from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import secrets
import time
import uuid
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
import discord
import uvicorn
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from src.config import Settings
from src.player import MusicError, MusicManager
from src.queue import LoopMode
from src.storage import MusicStorage

LOGGER = logging.getLogger(__name__)
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_ME = "https://discord.com/api/users/@me"


class PlayRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    channel_id: int
    next_up: bool = False


class ChannelRequest(BaseModel):
    channel_id: int


class MoveRequest(BaseModel):
    origin: int = Field(ge=1)
    destination: int = Field(ge=1)


class ValueRequest(BaseModel):
    value: int | bool | str


class RetryImportRequest(BaseModel):
    channel_id: int


class PlaylistNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PlaylistPlayRequest(BaseModel):
    channel_id: int
    next_up: bool = False


@dataclass(slots=True)
class WebSession:
    user_id: int
    username: str
    avatar: str | None
    csrf: str
    expires_at: float


class WebServer:
    def __init__(self, bot: discord.Client, manager: MusicManager, settings: Settings) -> None:
        self.bot = bot
        self.manager = manager
        self.settings = settings
        self.app = FastAPI(title="Bot Marushan", docs_url=None, redoc_url=None, openapi_url=None)
        self.sessions: dict[str, WebSession] = {}
        self.oauth_states: dict[str, float] = {}
        self.storage = getattr(manager, "storage", None) or MusicStorage(settings.database_path)
        persisted_imports = self.storage.recent_imports(settings.discord_guild_id, 25)
        self.imports: dict[str, dict[str, Any]] = {}
        for stored in reversed(persisted_imports):
            stored["next_up"] = bool(stored["next_up"])
            if stored["status"] in {"queued", "resolving"}:
                stored.update(status="failed", error="La importación fue interrumpida por un reinicio.")
                self.storage.save_import(stored)
            self.imports[stored["id"]] = stored
        self.import_tasks: dict[str, asyncio.Task[None]] = {}
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.rate_limits: dict[int, deque[float]] = defaultdict(deque)
        self.lyrics_cache: dict[str, dict[str, Any]] = {}
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._state_secret = (settings.web_session_secret or secrets.token_urlsafe(48)).encode()
        self._static = Path(__file__).resolve().parent.parent / "web_dist"
        self._routes()
        manager.add_listener(self.broadcast)

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host=self.settings.web_host,
            port=self.settings.web_port,
            # Application events keep the configured log level. Uvicorn's INFO
            # websocket lifecycle messages are extremely noisy for stale tabs.
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(), name="bot-marushan-web")

    async def close(self) -> None:
        self.manager.remove_listener(self.broadcast)
        for task in tuple(self.import_tasks.values()):
            task.cancel()
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def broadcast(self, event: str = "state") -> None:
        message = json.dumps({"type": event, "at": int(time.time() * 1000)})
        for queue in tuple(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def _routes(self) -> None:
        app = self.app

        @app.middleware("http")
        async def security_headers(request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self' wss: ws:; frame-ancestors 'none'"
            )
            if self.settings.public_base_url.startswith("https://"):
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response

        @app.get("/health")
        async def health() -> JSONResponse:
            discord_ready = self.bot.is_ready()
            lavalink_ready = self.manager.lavalink_connected()
            healthy = discord_ready and lavalink_ready
            return JSONResponse(
                {
                    "status": "ok" if healthy else "degraded",
                    "discord": discord_ready,
                    "lavalink": lavalink_ready,
                    "oauthConfigured": self.settings.discord_oauth_enabled,
                    "spotifyConfigured": self.settings.spotify_enabled,
                },
                status_code=200 if healthy else 503,
            )

        @app.get("/auth/discord")
        async def discord_login() -> Response:
            if not self.settings.discord_oauth_enabled:
                return HTMLResponse("Discord OAuth aún no está configurado.", status_code=503)
            self._prune_runtime_state()
            nonce = secrets.token_urlsafe(32)
            signature = hmac.new(self._state_secret, nonce.encode(), hashlib.sha256).hexdigest()
            state = f"{nonce}.{signature}"
            self.oauth_states[state] = time.monotonic() + 600
            query = urlencode(
                {
                    "client_id": self.settings.discord_oauth_client_id,
                    "response_type": "code",
                    "redirect_uri": f"{self.settings.public_base_url}/auth/discord/callback",
                    "scope": "identify",
                    "state": state,
                }
            )
            return RedirectResponse(f"{DISCORD_AUTHORIZE}?{query}")

        @app.get("/auth/discord/callback")
        async def discord_callback(code: str = "", state: str = "") -> Response:
            expires = self.oauth_states.pop(state, 0)
            if not code or expires < time.monotonic() or not self._valid_state(state):
                return HTMLResponse("La autorización expiró o no es válida.", status_code=400)
            data = {
                "client_id": self.settings.discord_oauth_client_id or "",
                "client_secret": self.settings.discord_oauth_client_secret or "",
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.settings.public_base_url}/auth/discord/callback",
            }
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                    async with session.post(DISCORD_TOKEN, data=data) as token_response:
                        token_data = await token_response.json()
                        if token_response.status >= 400:
                            raise RuntimeError("Discord rechazó el código OAuth")
                    async with session.get(
                        DISCORD_ME, headers={"Authorization": f"Bearer {token_data['access_token']}"}
                    ) as user_response:
                        user_data = await user_response.json()
                        if user_response.status >= 400:
                            raise RuntimeError("Discord no devolvió el usuario")
            except (TimeoutError, aiohttp.ClientError, RuntimeError, KeyError):
                LOGGER.exception("discord_oauth_failed")
                return HTMLResponse("No se pudo completar el login con Discord.", status_code=502)

            try:
                user_id = int(user_data["id"])
            except (KeyError, TypeError, ValueError):
                return HTMLResponse("Discord devolvió un usuario inválido.", status_code=502)
            guild = self._guild()
            try:
                await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return HTMLResponse("Tu cuenta no pertenece al servidor autorizado.", status_code=403)
            session_id = secrets.token_urlsafe(48)
            avatar = None
            if user_data.get("avatar"):
                avatar = f"https://cdn.discordapp.com/avatars/{user_id}/{user_data['avatar']}.png?size=128"
            self.sessions[session_id] = WebSession(
                user_id=user_id,
                username=user_data.get("global_name") or user_data.get("username") or "Usuario",
                avatar=avatar,
                csrf=secrets.token_urlsafe(32),
                expires_at=time.monotonic() + 43_200,
            )
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                "marushan_session",
                session_id,
                max_age=43_200,
                secure=self.settings.public_base_url.startswith("https://"),
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        @app.post("/auth/logout")
        async def logout(request: Request, x_csrf_token: str = Header(default="")) -> Response:
            session_id, session = self._require_session(request)
            self._verify_mutation(request, session, x_csrf_token)
            self.sessions.pop(session_id, None)
            response = JSONResponse({"ok": True})
            response.delete_cookie("marushan_session", path="/")
            return response

        @app.get("/api/me")
        async def me(request: Request) -> dict[str, Any]:
            _, session = self._require_session(request)
            member = await self._member(session.user_id)
            return {
                "id": str(session.user_id),
                "name": member.display_name or session.username,
                "avatar": session.avatar,
                "csrf": session.csrf,
            }

        @app.get("/api/state")
        async def state(request: Request) -> dict[str, Any]:
            _, session = self._require_session(request)
            member = await self._member(session.user_id)
            payload = self.manager.state(self._guild())
            payload["canControl"] = self._can_control(member)
            payload["oauthConfigured"] = self.settings.discord_oauth_enabled
            payload["imports"] = [self._public_import(job) for job in list(self.imports.values())[-10:]]
            current = payload.get("current")
            favorites = self.storage.favorites(self.settings.discord_guild_id, session.user_id)
            payload["currentFavorite"] = bool(
                current and any(item["uri"] == current.get("uri") for item in favorites)
            )
            return payload

        @app.get("/api/search")
        async def search(query: str, request: Request) -> list[dict[str, Any]]:
            _, session = self._require_session(request)
            member = await self._member(session.user_id)
            clean = query.strip()
            if not clean:
                raise HTTPException(422, "Escribe una búsqueda")
            if len(clean) > 500:
                raise HTTPException(422, "La búsqueda es demasiado larga")
            return await self.manager.search_candidates(clean, member)

        @app.get("/api/history")
        async def history(request: Request, limit: int = 100) -> list[dict[str, Any]]:
            self._require_session(request)
            return self.storage.history(self.settings.discord_guild_id, max(1, min(limit, 250)))

        @app.get("/api/statistics")
        async def statistics(request: Request, days: int = 30) -> dict[str, Any]:
            self._require_session(request)
            return self.storage.statistics(self.settings.discord_guild_id, max(1, min(days, 365)))

        @app.get("/api/favorites")
        async def favorites(request: Request) -> list[dict[str, Any]]:
            _, session = self._require_session(request)
            return self.storage.favorites(self.settings.discord_guild_id, session.user_id)

        @app.post("/api/favorites/current")
        async def favorite_current(
            request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            _, session = self._require_session(request)
            self._verify_mutation(request, session, x_csrf_token)
            current = self.manager.state(self._guild()).get("current")
            if not current:
                raise HTTPException(400, "No hay una canción reproduciéndose")
            favorite = self.storage.toggle_favorite(
                self.settings.discord_guild_id, session.user_id, current
            )
            await self.broadcast("library")
            return {"favorite": favorite}

        @app.get("/api/playlists")
        async def playlists(request: Request) -> list[dict[str, Any]]:
            _, session = self._require_session(request)
            return self.storage.playlists(self.settings.discord_guild_id, session.user_id)

        @app.post("/api/playlists")
        async def create_playlist(
            payload: PlaylistNameRequest, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, int]:
            _, session = self._require_session(request)
            self._verify_mutation(request, session, x_csrf_token)
            try:
                playlist_id = self.storage.create_playlist(
                    self.settings.discord_guild_id, session.user_id, payload.name
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            await self.broadcast("library")
            return {"id": playlist_id}

        @app.post("/api/playlists/{playlist_id}/save-queue")
        async def save_queue_playlist(
            playlist_id: int, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, int]:
            _, session = self._require_session(request)
            self._verify_mutation(request, session, x_csrf_token)
            state = self.manager.state(self._guild())
            tracks = ([state["current"]] if state.get("current") else []) + state["queue"]
            try:
                count = self.storage.replace_playlist_tracks(
                    playlist_id, self.settings.discord_guild_id, session.user_id, tracks
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            await self.broadcast("library")
            return {"tracks": count}

        @app.delete("/api/playlists/{playlist_id}")
        async def delete_playlist(
            playlist_id: int, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            _, session = self._require_session(request)
            self._verify_mutation(request, session, x_csrf_token)
            try:
                self.storage.delete_playlist(
                    playlist_id, self.settings.discord_guild_id, session.user_id
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            await self.broadcast("library")
            return {"ok": True}

        @app.post("/api/playlists/{playlist_id}/play", status_code=202)
        async def play_playlist(
            playlist_id: int,
            payload: PlaylistPlayRequest,
            request: Request,
            x_csrf_token: str = Header(default=""),
        ) -> dict[str, str]:
            member = await self._mutation_member(request, x_csrf_token)
            _, session = self._require_session(request)
            if not member.voice or not member.voice.channel or member.voice.channel.id != payload.channel_id:
                raise HTTPException(403, "Debes estar dentro del canal seleccionado.")
            try:
                tracks = self.storage.playlist_tracks(
                    playlist_id, self.settings.discord_guild_id, session.user_id
                )
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            if not tracks:
                raise HTTPException(400, "La playlist está vacía")
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id, "guild_id": self.settings.discord_guild_id, "user_id": member.id,
                "query": f"Playlist guardada ({len(tracks)} canciones)", "channel_id": payload.channel_id,
                "next_up": payload.next_up, "status": "queued", "source": "library",
                "found": len(tracks), "resolved": 0, "omitted": 0, "added": 0,
                "error": None, "created_at": int(time.time()),
            }
            self.imports[job_id] = job
            self.storage.save_import(job)
            task = asyncio.create_task(
                self._run_saved_playlist(job, member, payload, tracks), name=f"playlist-{job_id}"
            )
            self.import_tasks[job_id] = task
            task.add_done_callback(lambda _: self.import_tasks.pop(job_id, None))
            return {"jobId": job_id}

        @app.post("/api/radio/favorites", status_code=202)
        async def favorite_radio(
            payload: PlaylistPlayRequest,
            request: Request,
            x_csrf_token: str = Header(default=""),
        ) -> dict[str, str]:
            member = await self._mutation_member(request, x_csrf_token)
            _, session = self._require_session(request)
            tracks = self.storage.favorites(self.settings.discord_guild_id, session.user_id)
            if not tracks:
                raise HTTPException(400, "Necesitas al menos un favorito para iniciar la radio")
            random.shuffle(tracks)
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id, "guild_id": self.settings.discord_guild_id, "user_id": member.id,
                "query": "Radio de favoritos", "channel_id": payload.channel_id,
                "next_up": payload.next_up, "status": "queued", "source": "library",
                "found": len(tracks), "resolved": 0, "omitted": 0, "added": 0,
                "error": None, "created_at": int(time.time()),
            }
            self.imports[job_id] = job
            self.storage.save_import(job)
            task = asyncio.create_task(
                self._run_saved_playlist(job, member, payload, tracks), name=f"radio-{job_id}"
            )
            self.import_tasks[job_id] = task
            task.add_done_callback(lambda _: self.import_tasks.pop(job_id, None))
            await self.manager.set_autoplay(self.settings.discord_guild_id, True)
            return {"jobId": job_id}

        @app.get("/api/diagnostics")
        async def diagnostics(request: Request) -> dict[str, Any]:
            self._require_session(request)
            guild = self._guild()
            return {
                "discord": self.bot.is_ready(),
                "discordLatencyMs": round(getattr(self.bot, "latency", 0) * 1000),
                "lavalink": self.manager.lavalink_connected(),
                "spotify": self.settings.spotify_enabled,
                "connected": bool(guild.voice_client and getattr(guild.voice_client, "connected", False)),
                "database": True,
                "queueSize": len(self.manager.session(guild.id).queue.items),
                "importsRunning": sum(1 for job in self.imports.values() if job["status"] in {"queued", "resolving"}),
            }

        @app.get("/api/lyrics")
        async def lyrics(request: Request) -> dict[str, Any]:
            self._require_session(request)
            current = self.manager.state(self._guild()).get("current")
            if not current:
                raise HTTPException(404, "No hay una canción reproduciéndose")
            cache_key = f"{current['author']}::{current['title']}::{current['duration']}"
            if cache_key in self.lyrics_cache:
                return self.lyrics_cache[cache_key]
            params = {
                "artist_name": current["author"],
                "track_name": current["title"],
                "duration": max(1, round(current["duration"] / 1000)),
            }
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.get("https://lrclib.net/api/get", params=params) as response:
                        if response.status == 404:
                            result = {"title": current["title"], "artist": current["author"], "lines": [], "plain": None, "provider": "LRCLIB"}
                        elif response.status >= 400:
                            raise RuntimeError(f"LRCLIB respondió {response.status}")
                        else:
                            payload = await response.json()
                            result = {
                                "title": current["title"], "artist": current["author"],
                                "lines": self._parse_synced_lyrics(payload.get("syncedLyrics") or ""),
                                "plain": payload.get("plainLyrics"), "provider": "LRCLIB",
                            }
            except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
                LOGGER.warning("lyrics_lookup_failed title=%r error=%s", current["title"], exc)
                raise HTTPException(502, "No se pudieron consultar las letras en este momento") from exc
            if len(self.lyrics_cache) >= 100:
                self.lyrics_cache.pop(next(iter(self.lyrics_cache)))
            self.lyrics_cache[cache_key] = result
            return result

        @app.get("/api/voice-channels")
        async def voice_channels(request: Request) -> list[dict[str, Any]]:
            _, session = self._require_session(request)
            member = await self._member(session.user_id)
            return [
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "members": len([item for item in channel.members if not item.bot]),
                    "userHere": bool(member.voice and member.voice.channel == channel),
                }
                for channel in self._guild().voice_channels
                if channel.permissions_for(member).connect
            ]

        @app.post("/api/connect")
        async def connect(
            payload: ChannelRequest, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            member = await self._mutation_member(request, x_csrf_token)
            await self.manager.ensure_player(self._guild(), member, payload.channel_id)
            return {"ok": True}

        @app.post("/api/play", status_code=202)
        async def play(
            payload: PlayRequest, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, str]:
            member = await self._mutation_member(request, x_csrf_token)
            if not member.voice or not member.voice.channel or member.voice.channel.id != payload.channel_id:
                raise HTTPException(403, "Debes estar dentro del canal seleccionado.")
            self._prune_runtime_state()
            while len(self.imports) >= 100:
                self.imports.pop(next(iter(self.imports)))
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "guild_id": self.settings.discord_guild_id,
                "user_id": member.id,
                "query": payload.query,
                "channel_id": payload.channel_id,
                "next_up": payload.next_up,
                "status": "queued",
                "source": "unknown",
                "found": 0,
                "resolved": 0,
                "omitted": 0,
                "added": 0,
                "error": None,
                "created_at": int(time.time()),
            }
            self.imports[job_id] = job
            self.storage.save_import(job)
            task = asyncio.create_task(self._run_import(job, member, payload), name=f"import-{job_id}")
            self.import_tasks[job_id] = task
            task.add_done_callback(lambda _: self.import_tasks.pop(job_id, None))
            return {"jobId": job_id}

        @app.get("/api/imports/{job_id}")
        async def import_status(job_id: str, request: Request) -> dict[str, Any]:
            self._require_session(request)
            if job_id not in self.imports:
                raise HTTPException(404, "Importación no encontrada")
            return self._public_import(self.imports[job_id])

        @app.post("/api/imports/{job_id}/cancel")
        async def cancel_import(
            job_id: str, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            _, session = self._require_session(request)
            self._verify_mutation(request, session, x_csrf_token)
            job = self.imports.get(job_id)
            if not job:
                raise HTTPException(404, "Importación no encontrada")
            task = self.import_tasks.get(job_id)
            if task and not task.done():
                task.cancel()
            job.update(status="cancelled", error="Cancelada por el usuario")
            self.storage.save_import(job)
            await self.broadcast("import")
            return {"ok": True}

        @app.post("/api/imports/{job_id}/retry", status_code=202)
        async def retry_import(
            job_id: str,
            payload: RetryImportRequest,
            request: Request,
            x_csrf_token: str = Header(default=""),
        ) -> dict[str, str]:
            member = await self._mutation_member(request, x_csrf_token)
            original = self.imports.get(job_id)
            if not original:
                raise HTTPException(404, "Importación no encontrada")
            replay = PlayRequest(
                query=original["query"], channel_id=payload.channel_id, next_up=bool(original["next_up"])
            )
            new_id = uuid.uuid4().hex
            job = {
                **original,
                "id": new_id,
                "user_id": member.id,
                "channel_id": payload.channel_id,
                "status": "queued",
                "source": "unknown",
                "found": 0,
                "resolved": 0,
                "omitted": 0,
                "added": 0,
                "error": None,
                "created_at": int(time.time()),
            }
            self.imports[new_id] = job
            self.storage.save_import(job)
            task = asyncio.create_task(self._run_import(job, member, replay), name=f"import-{new_id}")
            self.import_tasks[new_id] = task
            task.add_done_callback(lambda _: self.import_tasks.pop(new_id, None))
            return {"jobId": new_id}

        @app.post("/api/player/{action}")
        async def player_action(
            action: str, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            member = await self._mutation_member(request, x_csrf_token, control=True)
            guild = self._guild()
            if action == "pause":
                await self.manager.pause(guild, member, True)
            elif action == "resume":
                await self.manager.pause(guild, member, False)
            elif action == "skip":
                await self.manager.skip(guild, member)
            elif action == "previous":
                await self.manager.previous(guild, member)
            elif action == "stop":
                await self.manager.stop(guild, member)
            elif action == "disconnect":
                await self.manager.disconnect(guild, member)
            else:
                raise HTTPException(404, "Control desconocido")
            return {"ok": True}

        @app.delete("/api/queue/{position}")
        async def remove_queue(
            position: int, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            await self._mutation_member(request, x_csrf_token, control=True)
            await self.manager.remove(self.settings.discord_guild_id, position)
            return {"ok": True}

        @app.post("/api/queue/move")
        async def move_queue(
            payload: MoveRequest, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            await self._mutation_member(request, x_csrf_token, control=True)
            await self.manager.move(self.settings.discord_guild_id, payload.origin, payload.destination)
            return {"ok": True}

        @app.post("/api/queue/{action}")
        async def queue_action(
            action: str, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            member = await self._mutation_member(request, x_csrf_token, control=True)
            if action == "clear":
                await self.manager.clear(self.settings.discord_guild_id)
            elif action == "shuffle":
                await self.manager.shuffle(self.settings.discord_guild_id)
            else:
                raise HTTPException(404, "Acción desconocida")
            self.manager.require_same_channel(self._guild(), member)
            return {"ok": True}

        @app.post("/api/queue/jump/{position}")
        async def jump_queue(
            position: int, request: Request, x_csrf_token: str = Header(default="")
        ) -> dict[str, bool]:
            member = await self._mutation_member(request, x_csrf_token, control=True)
            await self.manager.jump(self._guild(), member, position)
            return {"ok": True}

        @app.post("/api/settings/{setting}")
        async def settings_action(
            setting: str,
            payload: ValueRequest,
            request: Request,
            x_csrf_token: str = Header(default=""),
        ) -> dict[str, bool]:
            member = await self._mutation_member(request, x_csrf_token, control=True)
            guild = self._guild()
            try:
                if setting == "volume":
                    await self.manager.set_volume(guild, member, int(payload.value))
                elif setting == "seek":
                    await self.manager.seek(guild, member, int(payload.value))
                elif setting == "loop":
                    await self.manager.set_loop(guild.id, LoopMode(str(payload.value)))
                elif setting == "autoplay":
                    await self.manager.set_autoplay(guild.id, bool(payload.value))
                elif setting == "filter":
                    await self.manager.set_filter(guild, member, str(payload.value))
                elif setting == "stay247":
                    await self.manager.set_247(guild, member, bool(payload.value))
                else:
                    raise HTTPException(404, "Ajuste desconocido")
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, "Valor inválido") from exc
            return {"ok": True}

        @app.websocket("/api/events")
        async def events(websocket: WebSocket) -> None:
            origin = websocket.headers.get("origin", "").rstrip("/")
            if origin != self.settings.public_base_url:
                await websocket.accept()
                await websocket.close(code=4403)
                return
            session_id = websocket.cookies.get("marushan_session", "")
            session = self.sessions.get(session_id)
            if not session or session.expires_at < time.monotonic():
                await websocket.accept()
                await websocket.close(code=4401)
                return
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
            self.subscribers.add(queue)
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "connected"}))
            try:
                while True:
                    await websocket.send_text(await queue.get())
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                self.subscribers.discard(queue)

        @app.exception_handler(MusicError)
        async def music_error(_: Request, exc: MusicError) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        @app.exception_handler(IndexError)
        async def index_error(_: Request, exc: IndexError) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        @app.get("/{path:path}")
        async def frontend(path: str) -> Response:
            requested = (self._static / path).resolve()
            if path and self._static in requested.parents and requested.is_file():
                return FileResponse(requested)
            index = self._static / "index.html"
            if index.is_file():
                return FileResponse(index)
            return HTMLResponse("El frontend aún no fue compilado.", status_code=503)

    async def _run_import(self, job: dict[str, Any], member: discord.Member, payload: PlayRequest) -> None:
        async def progress(update: dict[str, int | str]) -> None:
            job.update(update)
            job["status"] = "resolving"
            self.storage.save_import(job)
            await self.broadcast("import")

        try:
            accepted, omitted, _ = await self.manager.enqueue(
                self._guild(),
                member,
                None,
                payload.query,
                next_up=payload.next_up,
                channel_id=payload.channel_id,
                progress=progress,
            )
            job.update(status="complete", added=accepted, omitted=omitted)
        except asyncio.CancelledError:
            job.update(status="cancelled", error="Cancelada por el usuario")
        except Exception as exc:
            LOGGER.exception("web_import_failed job_id=%s", job["id"])
            message = str(exc) if isinstance(exc, (MusicError, HTTPException)) else "Error interno al importar"
            job.update(status="failed", error=message)
        self.storage.save_import(job)
        await self.broadcast("import")

    async def _run_saved_playlist(
        self,
        job: dict[str, Any],
        member: discord.Member,
        payload: PlaylistPlayRequest,
        tracks: list[dict[str, Any]],
    ) -> None:
        ordered = list(reversed(tracks)) if payload.next_up else tracks
        try:
            job["status"] = "resolving"
            for stored in ordered:
                try:
                    accepted, omitted, _ = await self.manager.enqueue(
                        self._guild(), member, None, stored["uri"], next_up=payload.next_up,
                        channel_id=payload.channel_id,
                    )
                    job["added"] += accepted
                    job["resolved"] += accepted
                    job["omitted"] += omitted
                except (MusicError, HTTPException):
                    job["omitted"] += 1
                self.storage.save_import(job)
                await self.broadcast("import")
            job["status"] = "complete" if job["added"] else "failed"
            if not job["added"]:
                job["error"] = "No se pudo resolver ninguna canción guardada"
        except asyncio.CancelledError:
            job.update(status="cancelled", error="Cancelada por el usuario")
        except Exception:
            LOGGER.exception("saved_playlist_import_failed job_id=%s", job["id"])
            job.update(status="failed", error="Error interno al reproducir la playlist")
        self.storage.save_import(job)
        await self.broadcast("import")

    @staticmethod
    def _public_import(job: dict[str, Any]) -> dict[str, Any]:
        return {
            key: job.get(key)
            for key in (
                "id", "query", "next_up", "status", "source", "found", "resolved",
                "omitted", "added", "error", "created_at",
            )
        }

    @staticmethod
    def _parse_synced_lyrics(value: str) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        pattern = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
        for raw_line in value.splitlines():
            match = pattern.match(raw_line)
            if not match:
                continue
            milliseconds = int((int(match.group(1)) * 60 + float(match.group(2))) * 1000)
            text = match.group(3).strip()
            if text:
                lines.append({"time": milliseconds, "text": text})
        return lines

    def _valid_state(self, state: str) -> bool:
        try:
            nonce, signature = state.rsplit(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self._state_secret, nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _guild(self) -> discord.Guild:
        guild = self.bot.get_guild(self.settings.discord_guild_id)
        if not guild:
            raise HTTPException(503, "Discord todavía no está listo")
        return guild

    async def _member(self, user_id: int) -> discord.Member:
        guild = self._guild()
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            raise HTTPException(403, "Ya no perteneces al servidor autorizado") from exc

    def _require_session(self, request: Request) -> tuple[str, WebSession]:
        self._prune_runtime_state()
        session_id = request.cookies.get("marushan_session", "")
        session = self.sessions.get(session_id)
        if not session or session.expires_at < time.monotonic():
            self.sessions.pop(session_id, None)
            raise HTTPException(401, "Inicia sesión con Discord")
        return session_id, session

    def _verify_mutation(self, request: Request, session: WebSession, csrf: str) -> None:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin != self.settings.public_base_url:
            raise HTTPException(403, "Origen no permitido")
        if not csrf or not hmac.compare_digest(csrf, session.csrf):
            raise HTTPException(403, "Token CSRF inválido")
        now = time.monotonic()
        bucket = self.rate_limits[session.user_id]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= 60:
            raise HTTPException(429, "Demasiadas acciones; espera un momento")
        bucket.append(now)

    def _prune_runtime_state(self) -> None:
        now = time.monotonic()
        self.oauth_states = {key: expiry for key, expiry in self.oauth_states.items() if expiry >= now}
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires_at >= now}
        active_users = {session.user_id for session in self.sessions.values()}
        for user_id, bucket in tuple(self.rate_limits.items()):
            while bucket and bucket[0] < now - 60:
                bucket.popleft()
            if not bucket and user_id not in active_users:
                self.rate_limits.pop(user_id, None)

    async def _mutation_member(self, request: Request, csrf: str, *, control: bool = False) -> discord.Member:
        _, session = self._require_session(request)
        self._verify_mutation(request, session, csrf)
        member = await self._member(session.user_id)
        if not member.voice or not member.voice.channel:
            raise HTTPException(403, "Debes estar dentro de un canal de voz")
        if control:
            try:
                self.manager.require_same_channel(self._guild(), member)
            except MusicError as exc:
                raise HTTPException(403, str(exc)) from exc
        return member

    def _can_control(self, member: discord.Member) -> bool:
        if not member.voice or not member.voice.channel:
            return False
        player = self._guild().voice_client
        return not player or getattr(player, "channel", None) == member.voice.channel
