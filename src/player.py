from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import discord
import wavelink

from src.config import Settings
from src.queue import LoopMode, QueueFullError, SessionQueue
from src.spotify import SpotifyClient, SpotifyError, SpotifyTrack, is_spotify_url
from src.utils import match_score
from src.youtube import YoutubeStreamResolver

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, int | str]], Awaitable[None] | None]
StateListener = Callable[[str], Awaitable[None] | None]


class MusicError(RuntimeError):
    pass


@dataclass(slots=True)
class GuildSession:
    queue: SessionQueue[wavelink.Playable]
    text_channel_id: int | None = None
    volume: int = 75
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    starting: bool = False
    idle_task: asyncio.Task[None] | None = None
    monitor_task: asyncio.Task[None] | None = None
    recovery_task: asyncio.Task[None] | None = None
    tracked_identifier: str | None = None
    last_position: int = 0
    last_progress_at: float = field(default_factory=time.monotonic)


class MusicManager:
    def __init__(self, bot: discord.Client, settings: Settings, spotify: SpotifyClient | None = None) -> None:
        self.bot = bot
        self.settings = settings
        self.spotify = spotify
        self.youtube = YoutubeStreamResolver()
        self.sessions: dict[int, GuildSession] = {}
        self._listeners: set[StateListener] = set()

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: StateListener) -> None:
        self._listeners.discard(listener)

    async def emit(self, event: str = "state") -> None:
        for listener in tuple(self._listeners):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                LOGGER.exception("music_listener_failed event=%s", event)

    def session(self, guild_id: int) -> GuildSession:
        if guild_id not in self.sessions:
            self.sessions[guild_id] = GuildSession(
                queue=SessionQueue(max_size=self.settings.max_queue_size),
                volume=self.settings.default_volume,
            )
        return self.sessions[guild_id]

    async def close(self) -> None:
        for session in self.sessions.values():
            if session.idle_task:
                session.idle_task.cancel()
            if session.monitor_task:
                session.monitor_task.cancel()
            if session.recovery_task:
                session.recovery_task.cancel()
        if self.spotify:
            await self.spotify.close()

    @staticmethod
    def member_voice_channel(member: discord.Member) -> discord.VoiceChannel | discord.StageChannel:
        if not member.voice or not member.voice.channel:
            raise MusicError("Debes entrar a un canal de voz primero.")
        return member.voice.channel

    async def ensure_player(
        self, guild: discord.Guild, member: discord.Member, channel_id: int | None = None
    ) -> wavelink.Player:
        member_channel = self.member_voice_channel(member)
        if channel_id is not None and member_channel.id != channel_id:
            raise MusicError("Debes estar dentro del canal de voz seleccionado.")
        bot_member = guild.me
        if bot_member is None:
            raise MusicError("Discord todavía no terminó de preparar al bot en este servidor.")
        permissions = member_channel.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.connect:
            raise MusicError(f"No tengo permiso para entrar a **{member_channel.name}**.")
        if not permissions.speak:
            raise MusicError(f"No tengo permiso para hablar en **{member_channel.name}**.")
        player = guild.voice_client
        if player and not isinstance(player, wavelink.Player):
            raise MusicError("La conexión de voz actual no pertenece al reproductor musical.")
        if isinstance(player, wavelink.Player):
            if player.channel != member_channel:
                humans = [user for user in player.channel.members if not user.bot]
                if humans:
                    raise MusicError(f"Ya estoy siendo usado en **{player.channel.name}**.")
                await player.move_to(member_channel)
                await self.emit("connection")
            self._ensure_monitor(player)
            if self.session(guild.id).queue.current is None:
                self._schedule_idle(player, self.session(guild.id))
            return player
        player = await member_channel.connect(cls=wavelink.Player, self_deaf=True)
        self._ensure_monitor(player)
        self._schedule_idle(player, self.session(guild.id))
        await self.emit("connection")
        return player

    @staticmethod
    def require_same_channel(guild: discord.Guild, member: discord.Member) -> wavelink.Player:
        player = guild.voice_client
        if not isinstance(player, wavelink.Player) or not player.connected:
            raise MusicError("No estoy conectado a un canal de voz.")
        if not member.voice or member.voice.channel != player.channel:
            raise MusicError("Debes estar en el mismo canal de voz que el bot.")
        return player

    async def resolve(
        self, query: str, requester: discord.abc.User, progress: ProgressCallback | None = None
    ) -> tuple[list[wavelink.Playable], int]:
        if is_spotify_url(query):
            if not self.spotify:
                raise MusicError("La integración de Spotify no está configurada.")
            try:
                spotify_tracks = await self.spotify.resolve(query, limit=self.settings.max_queue_size)
            except SpotifyError as exc:
                raise MusicError(str(exc)) from exc
            LOGGER.info(
                "spotify_import_started requester_id=%s found=%s",
                requester.id,
                len(spotify_tracks),
            )
            await self._progress(progress, source="spotify", found=len(spotify_tracks), resolved=0, omitted=0)
            semaphore = asyncio.Semaphore(4)
            completed = 0
            omitted = 0

            async def resolve_one(wanted: SpotifyTrack) -> wavelink.Playable | None:
                nonlocal completed, omitted
                async with semaphore:
                    try:
                        track = await self._resolve_spotify_track(wanted, requester)
                    except Exception:
                        LOGGER.exception("spotify_track_resolution_failed title=%r", wanted.title)
                        track = None
                completed += 1
                if track is None:
                    omitted += 1
                await self._progress(
                    progress,
                    source="spotify",
                    found=len(spotify_tracks),
                    resolved=completed - omitted,
                    omitted=omitted,
                )
                return track

            results = await asyncio.gather(*(resolve_one(track) for track in spotify_tracks))
            resolved_tracks = [track for track in results if track is not None]
            LOGGER.info(
                "spotify_import_resolved requester_id=%s found=%s resolved=%s omitted=%s",
                requester.id,
                len(spotify_tracks),
                len(resolved_tracks),
                omitted,
            )
            return resolved_tracks, omitted

        query = self.youtube.without_radio(query)
        is_url = query.startswith(("http://", "https://"))
        source = None if is_url else wavelink.TrackSource.YouTube
        try:
            results = await wavelink.Playable.search(query, source=source)
        except Exception as exc:
            raise MusicError("YouTube no pudo procesar esa búsqueda o playlist.") from exc
        tracks = self._search_tracks(results)
        if not tracks and not is_url:
            LOGGER.info("youtube_search_empty_falling_back_to_music query=%r", query)
            try:
                results = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTubeMusic)
            except Exception:
                LOGGER.exception("youtube_music_fallback_failed query=%r", query)
            else:
                tracks = self._search_tracks(results)
        if not tracks:
            raise MusicError("No encontré resultados reproducibles.")
        if not isinstance(results, wavelink.Playlist):
            tracks = tracks[:1]
        tracks = tracks[: self.settings.max_queue_size]
        for track in tracks:
            self._set_requester(track, requester)
        await self._progress(progress, source="youtube", found=len(tracks), resolved=len(tracks), omitted=0)
        return tracks, 0

    async def _resolve_spotify_track(self, wanted: SpotifyTrack, requester: discord.abc.User) -> wavelink.Playable | None:
        candidates: list[wavelink.Playable] = []
        seen: set[str] = set()

        async def search(query: str, source: wavelink.TrackSource) -> None:
            try:
                results = await wavelink.Playable.search(query, source=source)
            except Exception:
                # One unavailable YouTube client must not suppress the YouTube
                # Music fallback for the same Spotify item.
                LOGGER.exception(
                    "spotify_search_failed title=%r source=%s", wanted.title, source.value
                )
                return
            for item in self._search_tracks(results)[:10]:
                identifier = self._track_identifier(item) or str(id(item))
                if identifier not in seen:
                    seen.add(identifier)
                    candidates.append(item)

        def scored_candidates() -> list[tuple[float, wavelink.Playable]]:
            return sorted(
                (
                    (
                        match_score(
                            wanted.title,
                            " ".join(wanted.artists),
                            wanted.duration_ms,
                            item.title,
                            item.author,
                            item.length,
                        ),
                        item,
                    )
                    for item in candidates
                ),
                key=lambda pair: pair[0],
                reverse=True,
            )

        await search(wanted.search_query, wavelink.TrackSource.YouTube)
        score = 0.0
        chosen: wavelink.Playable | None = None
        if candidates:
            score, chosen = scored_candidates()[0]
        if score < 0.50:
            await search(wanted.search_query, wavelink.TrackSource.YouTubeMusic)
            if candidates:
                score, chosen = scored_candidates()[0]
        if score < 0.50 and wanted.artists:
            # Short titles such as "More" often need an artist-first query.
            await search(f"{wanted.artists[0]} - {wanted.title}", wavelink.TrackSource.YouTubeMusic)
            if candidates:
                score, chosen = scored_candidates()[0]
        if chosen is None:
            return None
        if score < 0.50:
            LOGGER.warning(
                "spotify_match_rejected title=%r artists=%r candidate=%r author=%r score=%.2f",
                wanted.title,
                wanted.artists,
                chosen.title,
                chosen.author,
                score,
            )
            return None
        LOGGER.debug(
            "spotify_match_selected title=%r candidate=%r author=%r score=%.2f",
            wanted.title,
            chosen.title,
            chosen.author,
            score,
        )
        chosen.extras = {
            "requester_id": requester.id,
            "requester_name": requester.display_name,
            "spotify_url": wanted.url,
            "spotify_artwork": wanted.artwork,
            "spotify_isrc": wanted.isrc,
            "spotify_title": wanted.title,
            "spotify_artists": ", ".join(wanted.artists),
            "match_score": round(score, 3),
        }
        return chosen

    @staticmethod
    async def _progress(callback: ProgressCallback | None, **payload: int | str) -> None:
        if callback is None:
            return
        result = callback(payload)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _search_tracks(results: Any) -> list[wavelink.Playable]:
        return list(results.tracks) if isinstance(results, wavelink.Playlist) else list(results)

    @staticmethod
    def _set_requester(track: wavelink.Playable, requester: discord.abc.User) -> None:
        track.extras = {"requester_id": requester.id, "requester_name": requester.display_name}

    async def enqueue(
        self,
        guild: discord.Guild,
        member: discord.Member,
        text_channel_id: int | None,
        query: str,
        *,
        next_up: bool = False,
        channel_id: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> tuple[int, int, wavelink.Playable]:
        started_at = time.monotonic()
        source = "spotify" if is_spotify_url(query) else "youtube"
        player = await self.ensure_player(guild, member, channel_id)
        try:
            tracks, misses = await self.resolve(query, member, progress)
        except Exception:
            self._schedule_idle(player, self.session(guild.id))
            raise
        # Resolution of a large Spotify playlist may take a while. Revalidate
        # that the requester is still in the selected voice channel before
        # mutating the shared queue.
        player = await self.ensure_player(guild, member, channel_id)
        if not tracks:
            raise MusicError("No pude resolver ninguna pista reproducible.")
        session = self.session(guild.id)
        async with session.lock:
            session.text_channel_id = text_channel_id
            self._cancel_idle(session)
            try:
                accepted = session.queue.add(tracks, next_up=next_up)
            except QueueFullError as exc:
                raise MusicError(str(exc)) from exc
            should_start = session.queue.current is None and not player.playing and not session.starting
            if should_start:
                # Claim the transition before releasing the lock. Two concurrent
                # Discord/web imports must never both consume the queue head.
                session.starting = True
        started_track: wavelink.Playable | None = None
        if should_start:
            started_track = await self.play_next(player)
            if started_track is None:
                raise MusicError("Encontré resultados, pero la fuente de audio no pudo iniciar ninguna pista.")
        await self.emit("queue")
        total_omitted = misses + max(0, len(tracks) - accepted)
        LOGGER.info(
            "enqueue_completed guild_id=%s source=%s resolved=%s accepted=%s omitted=%s duration_ms=%s",
            guild.id,
            source,
            len(tracks),
            accepted,
            total_omitted,
            round((time.monotonic() - started_at) * 1000),
        )
        return accepted, total_omitted, started_track or tracks[0]

    async def play_next(
        self,
        player: wavelink.Player,
        *,
        failed: bool = False,
        announce_current_failure: bool = False,
        expected_current: wavelink.Playable | None = None,
    ) -> wavelink.Playable | None:
        session = self.session(player.guild.id)
        failed_tracks: list[wavelink.Playable] = []
        started_track: wavelink.Playable | None = None
        async with session.lock:
            if expected_current is not None and not self._same_track(session.queue.current, expected_current):
                LOGGER.info(
                    "stale_track_transition_ignored guild_id=%s expected=%r current=%r",
                    player.guild.id,
                    getattr(expected_current, "title", None),
                    getattr(session.queue.current, "title", None),
                )
                return None
            session.starting = True
            try:
                if session.queue.current is not None:
                    if failed and announce_current_failure:
                        failed_tracks.append(session.queue.current)
                    session.queue.finish_current(failed=failed)
                autoplay_attempted = False
                while started_track is None:
                    next_track = session.queue.take_next()
                    if next_track is None and session.queue.autoplay and not autoplay_attempted:
                        autoplay_attempted = True
                        try:
                            next_track = await self._autoplay_track(player)
                        except Exception:
                            LOGGER.exception("autoplay_resolution_failed guild_id=%s", player.guild.id)
                        session.queue.current = next_track
                    if next_track is None:
                        self._schedule_idle(player, session)
                        break
                    self._cancel_idle(session)
                    try:
                        await self.play_track(player, next_track)
                    except Exception:
                        LOGGER.exception("track_play_failed guild_id=%s title=%r", player.guild.id, next_track.title)
                        failed_tracks.append(next_track)
                        session.queue.finish_current(failed=True)
                        continue
                    started_track = next_track
            finally:
                session.starting = False
        if failed_tracks:
            await self._announce_failed_tracks(session, failed_tracks)
        await self.emit("player")
        return started_track

    async def play_track(self, player: wavelink.Player, track: wavelink.Playable) -> None:
        session = self.session(player.guild.id)
        session.tracked_identifier = self._track_identifier(track)
        session.last_position = 0
        session.last_progress_at = time.monotonic()
        await player.play(track, volume=self.session(player.guild.id).volume)

    def record_player_update(self, player: wavelink.Player, position: int, connected: bool) -> None:
        session = self.session(player.guild.id)
        track = session.queue.current
        identifier = self._track_identifier(track)
        now = time.monotonic()
        if identifier != session.tracked_identifier:
            session.tracked_identifier = identifier
            session.last_position = position
            session.last_progress_at = now
            return
        if connected and position >= session.last_position + 500:
            session.last_progress_at = now
        session.last_position = position

    async def _announce_failed_tracks(
        self, session: GuildSession, tracks: list[wavelink.Playable]
    ) -> None:
        if session.text_channel_id is None:
            return
        get_channel = getattr(self.bot, "get_channel", None)
        channel = get_channel(session.text_channel_id) if callable(get_channel) else None
        if channel is None or not hasattr(channel, "send"):
            return
        if len(tracks) == 1:
            message = f"⚠️ Omití **{tracks[0].title}** porque la fuente de audio falló."
        else:
            message = f"⚠️ Omití **{len(tracks)}** pistas porque la fuente de audio falló."
        try:
            await channel.send(message)
        except discord.HTTPException:
            LOGGER.exception("track_failure_announcement_failed channel_id=%s", session.text_channel_id)

    async def _autoplay_track(self, player: wavelink.Player) -> wavelink.Playable | None:
        session = self.session(player.guild.id)
        seed = session.queue.history[-1] if session.queue.history else None
        if seed is None:
            return None
        results = await wavelink.Playable.search(f"{seed.title} {seed.author} mix", source=wavelink.TrackSource.YouTubeMusic)
        candidates = self._search_tracks(results)
        if not candidates:
            return None
        chosen = candidates[0]
        chosen.extras = {"requester_id": 0, "requester_name": "Autoplay"}
        return chosen

    async def on_track_end(
        self, player: wavelink.Player, reason: Any, ended_track: wavelink.Playable | None = None
    ) -> None:
        reason_text = str(reason).casefold()
        LOGGER.info(
            "track_end guild_id=%s reason=%s title=%r",
            player.guild.id,
            reason_text,
            getattr(ended_track, "title", None),
        )
        # player.play() emits an end event for the replaced track. That event
        # must not consume the newly selected queue item.
        if reason_text == "replaced":
            return
        source_failed = any(value in reason_text for value in ("exception", "stuck", "load_failed", "loadfailed"))
        await self.play_next(
            player,
            failed=reason_text == "stopped" or source_failed,
            announce_current_failure=source_failed,
            expected_current=ended_track or self.session(player.guild.id).queue.current,
        )

    async def recover_failed_track(
        self,
        player: wavelink.Player,
        track: wavelink.Playable,
        *,
        cause: str,
        delay: float = 1.0,
    ) -> None:
        """Advance when Lavalink reports failure but omits or delays TrackEnd."""
        session = self.session(player.guild.id)
        current_task = asyncio.current_task()
        if session.recovery_task and session.recovery_task is not current_task and not session.recovery_task.done():
            return
        if delay:
            await asyncio.sleep(delay)
        if not self._same_track(session.queue.current, track):
            return
        LOGGER.warning(
            "track_recovery guild_id=%s cause=%s title=%r position=%s duration=%s",
            player.guild.id,
            cause,
            track.title,
            player.position,
            track.length,
        )
        try:
            await player.skip(force=True)
        except Exception:
            LOGGER.exception("track_recovery_skip_failed guild_id=%s", player.guild.id)
        await asyncio.sleep(2)
        if self._same_track(session.queue.current, track):
            await self.play_next(
                player,
                failed=True,
                announce_current_failure=True,
                expected_current=track,
            )

    async def pause(self, guild: discord.Guild, member: discord.Member, paused: bool) -> None:
        player = self.require_same_channel(guild, member)
        await player.pause(paused)
        await self.emit("player")

    async def skip(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self.require_same_channel(guild, member)
        await player.skip(force=True)

    async def previous(self, guild: discord.Guild, member: discord.Member) -> wavelink.Playable:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            track = session.queue.previous()
            if not track:
                raise MusicError("No hay una canción anterior.")
            await self.play_track(player, track)
        await self.emit("player")
        return track

    async def stop(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            session.queue.reset()
            session.starting = False
            self._cancel_recovery(session)
            await player.skip(force=True)
            self._schedule_idle(player, session)
        await self.emit("player")

    async def disconnect(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            session.queue.reset()
            session.starting = False
            self._cancel_recovery(session)
            if session.monitor_task and not session.monitor_task.done():
                session.monitor_task.cancel()
            session.monitor_task = None
            await player.disconnect()
        await self.emit("connection")

    async def remove(self, guild_id: int, position: int) -> wavelink.Playable:
        session = self.session(guild_id)
        async with session.lock:
            track = session.queue.remove(position)
        await self.emit("queue")
        return track

    async def move(self, guild_id: int, origin: int, destination: int) -> None:
        session = self.session(guild_id)
        async with session.lock:
            session.queue.move(origin, destination)
        await self.emit("queue")

    async def clear(self, guild_id: int) -> int:
        session = self.session(guild_id)
        async with session.lock:
            count = session.queue.clear()
        await self.emit("queue")
        return count

    async def shuffle(self, guild_id: int) -> None:
        session = self.session(guild_id)
        async with session.lock:
            if len(session.queue.items) < 2:
                raise MusicError("Se necesitan al menos dos canciones pendientes.")
            session.queue.shuffle()
        await self.emit("queue")

    async def jump(self, guild: discord.Guild, member: discord.Member, position: int) -> None:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            session.queue.jump(position)
            await player.skip(force=True)

    async def seek(self, guild: discord.Guild, member: discord.Member, milliseconds: int) -> None:
        player = self.require_same_channel(guild, member)
        track = self.session(guild.id).queue.current
        if not track or track.is_stream:
            raise MusicError("No se puede buscar dentro de un stream en vivo.")
        if milliseconds < 0 or milliseconds >= track.length:
            raise MusicError("La posición está fuera de la duración de la canción.")
        await player.seek(milliseconds)
        await self.emit("player")

    async def set_volume(self, guild: discord.Guild, member: discord.Member, volume: int) -> None:
        if not 1 <= volume <= 150:
            raise MusicError("El volumen debe estar entre 1 y 150.")
        player = self.require_same_channel(guild, member)
        self.session(guild.id).volume = volume
        await player.set_volume(volume)
        await self.emit("settings")

    async def set_loop(self, guild_id: int, mode: LoopMode) -> None:
        session = self.session(guild_id)
        async with session.lock:
            session.queue.loop_mode = mode
        await self.emit("settings")

    async def set_autoplay(self, guild_id: int, enabled: bool) -> None:
        session = self.session(guild_id)
        async with session.lock:
            session.queue.autoplay = enabled
        await self.emit("settings")

    def state(self, guild: discord.Guild) -> dict[str, Any]:
        session = self.session(guild.id)
        player = guild.voice_client if isinstance(guild.voice_client, wavelink.Player) else None
        return {
            "connected": bool(player and player.connected),
            "channel": ({"id": str(player.channel.id), "name": player.channel.name} if player and player.channel else None),
            "playing": bool(player and player.playing),
            "paused": bool(player and player.paused),
            "position": player.position if player else 0,
            "volume": session.volume,
            "loop": session.queue.loop_mode.value,
            "autoplay": session.queue.autoplay,
            "current": self.track_data(session.queue.current),
            "queue": [self.track_data(track, index=index) for index, track in enumerate(session.queue.items, 1)],
            "historyCount": len(session.queue.history),
            "spotify": {"configured": bool(self.spotify and self.spotify.authenticated)},
        }

    @staticmethod
    def lavalink_connected() -> bool:
        return any(node.status is wavelink.NodeStatus.CONNECTED for node in wavelink.Pool.nodes.values())

    @staticmethod
    def track_data(track: wavelink.Playable | None, *, index: int | None = None) -> dict[str, Any] | None:
        if track is None:
            return None
        spotify_title = getattr(track.extras, "spotify_title", None)
        spotify_artists = getattr(track.extras, "spotify_artists", None)
        data: dict[str, Any] = {
            "title": spotify_title or track.title,
            "author": spotify_artists or track.author,
            "duration": track.length,
            "stream": track.is_stream,
            "uri": getattr(track.extras, "spotify_url", None) or track.uri,
            "artwork": getattr(track.extras, "spotify_artwork", None) or track.artwork,
            "requester": getattr(track.extras, "requester_name", "Desconocido"),
            "source": "spotify" if spotify_title else str(getattr(track, "source", "youtube")),
        }
        if index is not None:
            data["position"] = index
        return data

    def _schedule_idle(self, player: wavelink.Player, session: GuildSession) -> None:
        if session.idle_task and not session.idle_task.done():
            return
        session.idle_task = asyncio.create_task(self._idle_disconnect(player, session))

    async def _idle_disconnect(self, player: wavelink.Player, session: GuildSession) -> None:
        try:
            empty_since: float | None = None
            interval = min(30, max(1, self.settings.idle_timeout_seconds))
            while player.connected and session.queue.current is None:
                humans = any(not user.bot for user in player.channel.members)
                if humans:
                    empty_since = None
                elif empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since >= self.settings.idle_timeout_seconds:
                    await player.disconnect()
                    session.queue.reset()
                    await self.emit("connection")
                    return
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def _ensure_monitor(self, player: wavelink.Player) -> None:
        session = self.session(player.guild.id)
        if session.monitor_task and not session.monitor_task.done():
            return
        session.monitor_task = asyncio.create_task(
            self._monitor_player(player, session), name=f"music-monitor-{player.guild.id}"
        )

    async def _monitor_player(self, player: wavelink.Player, session: GuildSession) -> None:
        """Repair missing end events and streams that stop producing frames."""
        try:
            while player.connected:
                await asyncio.sleep(5)
                track = session.queue.current
                if track is None or session.starting:
                    session.last_progress_at = time.monotonic()
                    continue
                if player.paused:
                    session.last_progress_at = time.monotonic()
                    continue
                if player.current is None:
                    if time.monotonic() - session.last_progress_at >= 10:
                        self.schedule_recovery(player, track, cause="player_state_desynchronized", delay=0)
                    continue
                if time.monotonic() - session.last_progress_at >= 30:
                    self.schedule_recovery(player, track, cause="playback_without_progress", delay=0)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("player_monitor_failed guild_id=%s", player.guild.id)

    def schedule_recovery(
        self,
        player: wavelink.Player,
        track: wavelink.Playable,
        *,
        cause: str,
        delay: float = 1.0,
    ) -> None:
        session = self.session(player.guild.id)
        if session.recovery_task and not session.recovery_task.done():
            return
        task = asyncio.create_task(
            self.recover_failed_track(player, track, cause=cause, delay=delay),
            name=f"track-recovery-{player.guild.id}",
        )
        session.recovery_task = task

        def clear_recovery(done: asyncio.Task[None]) -> None:
            if session.recovery_task is done:
                session.recovery_task = None
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                LOGGER.exception("track_recovery_task_failed guild_id=%s", player.guild.id)

        task.add_done_callback(clear_recovery)

    @staticmethod
    def _track_identifier(track: wavelink.Playable | None) -> str | None:
        if track is None:
            return None
        return str(getattr(track, "identifier", None) or getattr(track, "encoded", None) or id(track))

    @classmethod
    def _same_track(
        cls, left: wavelink.Playable | None, right: wavelink.Playable | None
    ) -> bool:
        if left is None or right is None:
            return left is right
        return left is right or cls._track_identifier(left) == cls._track_identifier(right)

    @staticmethod
    def _cancel_recovery(session: GuildSession) -> None:
        if session.recovery_task and not session.recovery_task.done():
            session.recovery_task.cancel()
        session.recovery_task = None

    @staticmethod
    def _cancel_idle(session: GuildSession) -> None:
        if session.idle_task and not session.idle_task.done():
            session.idle_task.cancel()
        session.idle_task = None
