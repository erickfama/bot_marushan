from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

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
    ignore_end_events: int = 0
    force_advance: bool = False
    idle_task: asyncio.Task[None] | None = None


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
            return player
        player = await member_channel.connect(cls=wavelink.Player, self_deaf=True)
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
            return [track for track in results if track is not None], omitted

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
        results = await wavelink.Playable.search(wanted.search_query, source=wavelink.TrackSource.YouTube)
        candidates = self._search_tracks(results)[:5]
        if not candidates:
            return None
        scored = sorted(
            ((match_score(wanted.title, " ".join(wanted.artists), wanted.duration_ms, item.title, item.author, item.length), item) for item in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        score, chosen = scored[0]
        if score < 0.42:
            LOGGER.warning("spotify_match_rejected title=%r score=%.2f", wanted.title, score)
            return None
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
        player = await self.ensure_player(guild, member, channel_id)
        tracks, misses = await self.resolve(query, member, progress)
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
            should_start = session.queue.current is None and not player.playing
        if should_start:
            await self.play_next(player)
        await self.emit("queue")
        return accepted, misses + max(0, len(tracks) - accepted), tracks[0]

    async def play_next(self, player: wavelink.Player, *, failed: bool = False) -> None:
        session = self.session(player.guild.id)
        async with session.lock:
            if session.queue.current is not None:
                session.queue.finish_current(failed=failed)
            next_track = session.queue.take_next()
            if next_track is None and session.queue.autoplay:
                next_track = await self._autoplay_track(player)
                session.queue.current = next_track
            if next_track is None:
                self._schedule_idle(player, session)
                await self.emit("player")
                return
            self._cancel_idle(session)
            try:
                await self.play_track(player, next_track)
            except Exception:
                LOGGER.exception("track_play_failed guild_id=%s title=%r", player.guild.id, next_track.title)
                session.queue.finish_current(failed=True)
                asyncio.create_task(self.play_next(player))
        await self.emit("player")

    async def play_track(self, player: wavelink.Player, track: wavelink.Playable) -> None:
        playable = await self.youtube.playable(track)
        await player.play(playable, volume=self.session(player.guild.id).volume)

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

    async def on_track_end(self, player: wavelink.Player, reason: Any) -> None:
        session = self.session(player.guild.id)
        if session.ignore_end_events:
            session.ignore_end_events -= 1
            return
        reason_text = str(reason).lower()
        force_advance = session.force_advance
        session.force_advance = False
        await self.play_next(player, failed=force_advance or "exception" in reason_text or "stuck" in reason_text)

    async def pause(self, guild: discord.Guild, member: discord.Member, paused: bool) -> None:
        player = self.require_same_channel(guild, member)
        await player.pause(paused)
        await self.emit("player")

    async def skip(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self.require_same_channel(guild, member)
        self.session(guild.id).force_advance = True
        await player.skip(force=True)

    async def previous(self, guild: discord.Guild, member: discord.Member) -> wavelink.Playable:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            track = session.queue.previous()
            if not track:
                raise MusicError("No hay una canción anterior.")
            session.ignore_end_events += 1
            await self.play_track(player, track)
        await self.emit("player")
        return track

    async def stop(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            session.queue.reset()
            session.ignore_end_events += 1
            await player.stop(force=True)
        await self.emit("player")

    async def disconnect(self, guild: discord.Guild, member: discord.Member) -> None:
        player = self.require_same_channel(guild, member)
        session = self.session(guild.id)
        async with session.lock:
            session.queue.reset()
            session.ignore_end_events += 1
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
            session.force_advance = True
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
            await asyncio.sleep(self.settings.idle_timeout_seconds)
            if session.queue.current is None and player.connected and not any(not user.bot for user in player.channel.members):
                await player.disconnect()
                session.queue.reset()
                await self.emit("connection")
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _cancel_idle(session: GuildSession) -> None:
        if session.idle_task and not session.idle_task.done():
            session.idle_task.cancel()
        session.idle_task = None
