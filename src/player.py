from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import discord
import wavelink

from src.config import Settings
from src.queue import QueueFullError, SessionQueue
from src.spotify import SpotifyClient, SpotifyError, SpotifyTrack, is_spotify_url
from src.utils import match_score
from src.youtube import YoutubeStreamResolver

LOGGER = logging.getLogger(__name__)


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

    async def ensure_player(self, guild: discord.Guild, member: discord.Member) -> wavelink.Player:
        if not member.voice or not member.voice.channel:
            raise MusicError("Debes entrar a un canal de voz primero.")
        player = guild.voice_client
        if player and not isinstance(player, wavelink.Player):
            raise MusicError("La conexión de voz actual no pertenece al reproductor musical.")
        if isinstance(player, wavelink.Player):
            if player.channel != member.voice.channel:
                humans = [user for user in player.channel.members if not user.bot]
                if humans:
                    raise MusicError(f"Ya estoy siendo usado en **{player.channel.name}**.")
                await player.move_to(member.voice.channel)
            return player
        return await member.voice.channel.connect(cls=wavelink.Player, self_deaf=True)

    @staticmethod
    def require_same_channel(guild: discord.Guild, member: discord.Member) -> wavelink.Player:
        player = guild.voice_client
        if not isinstance(player, wavelink.Player) or not player.connected:
            raise MusicError("No estoy conectado a un canal de voz.")
        if not member.voice or member.voice.channel != player.channel:
            raise MusicError("Debes estar en el mismo canal de voz que el bot.")
        return player

    async def resolve(self, query: str, requester: discord.abc.User) -> tuple[list[wavelink.Playable], int]:
        if is_spotify_url(query):
            if not self.spotify:
                raise MusicError("La integración de Spotify no está configurada.")
            try:
                spotify_tracks = await self.spotify.resolve(query, limit=self.settings.max_queue_size)
            except SpotifyError as exc:
                raise MusicError(str(exc)) from exc
            resolved: list[wavelink.Playable] = []
            misses = 0
            for spotify_track in spotify_tracks:
                track = await self._resolve_spotify_track(spotify_track, requester)
                if track:
                    resolved.append(track)
                else:
                    misses += 1
            return resolved, misses

        query = self.youtube.without_radio(query)
        source = None if query.startswith(("http://", "https://")) else wavelink.TrackSource.YouTube
        results = await wavelink.Playable.search(query, source=source)
        tracks = self._search_tracks(results)
        if not tracks:
            raise MusicError("No encontré resultados reproducibles.")
        if not isinstance(results, wavelink.Playlist):
            tracks = tracks[:1]
        for track in tracks[: self.settings.max_queue_size]:
            self._set_requester(track, requester)
        return tracks[: self.settings.max_queue_size], 0

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
    def _search_tracks(results: Any) -> list[wavelink.Playable]:
        if isinstance(results, wavelink.Playlist):
            return list(results.tracks)
        return list(results)

    @staticmethod
    def _set_requester(track: wavelink.Playable, requester: discord.abc.User) -> None:
        track.extras = {"requester_id": requester.id, "requester_name": requester.display_name}

    async def enqueue(
        self,
        guild: discord.Guild,
        member: discord.Member,
        text_channel_id: int,
        query: str,
        *,
        next_up: bool = False,
    ) -> tuple[int, int, wavelink.Playable]:
        player = await self.ensure_player(guild, member)
        tracks, misses = await self.resolve(query, member)
        session = self.session(guild.id)
        session.text_channel_id = text_channel_id
        self._cancel_idle(session)
        try:
            accepted = session.queue.add(tracks, next_up=next_up)
        except QueueFullError as exc:
            raise MusicError(str(exc)) from exc
        if session.queue.current is None and not player.playing:
            await self.play_next(player)
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
                return
            self._cancel_idle(session)
            try:
                await self.play_track(player, next_track)
            except Exception:
                LOGGER.exception("track_play_failed guild_id=%s title=%r", player.guild.id, next_track.title)
                session.queue.finish_current(failed=True)
                asyncio.create_task(self.play_next(player))

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
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _cancel_idle(session: GuildSession) -> None:
        if session.idle_task and not session.idle_task.done():
            session.idle_task.cancel()
        session.idle_task = None
