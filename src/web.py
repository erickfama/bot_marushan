from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
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
        self.imports: dict[str, dict[str, Any]] = {}
        self.import_tasks: set[asyncio.Task[None]] = set()
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.rate_limits: dict[int, deque[float]] = defaultdict(deque)
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
        for task in tuple(self.import_tasks):
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
            payload["imports"] = list(self.imports.values())[-10:]
            return payload

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
                "status": "queued",
                "source": "unknown",
                "found": 0,
                "resolved": 0,
                "omitted": 0,
                "added": 0,
                "error": None,
            }
            self.imports[job_id] = job
            task = asyncio.create_task(self._run_import(job, member, payload), name=f"import-{job_id}")
            self.import_tasks.add(task)
            task.add_done_callback(self.import_tasks.discard)
            return {"jobId": job_id}

        @app.get("/api/imports/{job_id}")
        async def import_status(job_id: str, request: Request) -> dict[str, Any]:
            self._require_session(request)
            if job_id not in self.imports:
                raise HTTPException(404, "Importación no encontrada")
            return self.imports[job_id]

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
        except Exception as exc:
            LOGGER.exception("web_import_failed job_id=%s", job["id"])
            message = str(exc) if isinstance(exc, (MusicError, HTTPException)) else "Error interno al importar"
            job.update(status="failed", error=message)
        await self.broadcast("import")

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
