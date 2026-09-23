from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)
SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(track|album|playlist)/|spotify:(track|album|playlist):)([A-Za-z0-9]+)")


class SpotifyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    title: str
    artists: tuple[str, ...]
    duration_ms: int
    url: str
    artwork: str | None = None
    isrc: str | None = None

    @property
    def search_query(self) -> str:
        return f"{self.title} {' '.join(self.artists)} audio"


class SpotifyClient:
    API = "https://api.spotify.com/v1"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, session: aiohttp.ClientSession | None = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._session = session
        self._owns_session = session is None
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def resolve(self, value: str, *, limit: int = 500) -> list[SpotifyTrack]:
        match = SPOTIFY_RE.search(value)
        if not match:
            raise SpotifyError("El enlace de Spotify no es válido")
        resource_type = match.group(1) or match.group(2)
        resource_id = match.group(3)
        if resource_type == "track":
            return [self._track(await self._get(f"/tracks/{resource_id}"))]
        if resource_type == "album":
            album = await self._get(f"/albums/{resource_id}")
            artwork = self._artwork(album)
            items = await self._collect(album["tracks"], limit)
            return [self._track(item, artwork=artwork) for item in items]
        playlist = await self._get(f"/playlists/{resource_id}")
        artwork = self._artwork(playlist)
        page = playlist.get("items") or playlist.get("tracks")
        if not page:
            raise SpotifyError("Spotify no devolvió canciones para esta playlist")
        items = await self._collect(page, limit)
        tracks = [item.get("track", item.get("item", item)) for item in items]
        return [self._track(item, artwork=artwork) for item in tracks if item and item.get("type") == "track"]

    async def _collect(self, page: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items = list(page.get("items", []))
        next_url = page.get("next")
        while next_url and len(items) < limit:
            page = await self._request("GET", next_url)
            items.extend(page.get("items", []))
            next_url = page.get("next")
        return items[:limit]

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", f"{self.API}{path}")

    async def _request(self, method: str, url: str) -> dict[str, Any]:
        session = await self._http()
        for attempt in range(3):
            token = await self._token()
            async with session.request(method, url, headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status == 401 and attempt == 0:
                    self._expires_at = 0
                    continue
                if response.status == 429 and attempt < 2:
                    await asyncio.sleep(min(int(response.headers.get("Retry-After", "1")), 5))
                    continue
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise SpotifyError(f"Spotify respondió {response.status}: {detail}")
                return await response.json()
        raise SpotifyError("Spotify no respondió después de varios intentos")

    async def _token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            session = await self._http()
            async with session.post(
                self.TOKEN_URL,
                headers={"Authorization": f"Basic {credentials}"},
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            ) as response:
                if response.status >= 400:
                    raise SpotifyError(f"No se pudo renovar OAuth de Spotify ({response.status})")
                payload = await response.json()
            self._access_token = payload["access_token"]
            self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
            return self._access_token

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    @staticmethod
    def _artwork(resource: dict[str, Any]) -> str | None:
        images = resource.get("images") or []
        return images[0].get("url") if images else None

    @classmethod
    def _track(cls, item: dict[str, Any], *, artwork: str | None = None) -> SpotifyTrack:
        album = item.get("album") or {}
        return SpotifyTrack(
            title=item.get("name", "Sin título"),
            artists=tuple(artist["name"] for artist in item.get("artists", []) if artist.get("name")),
            duration_ms=int(item.get("duration_ms", 0)),
            url=(item.get("external_urls") or {}).get("spotify", ""),
            artwork=artwork or cls._artwork(album),
            isrc=(item.get("external_ids") or {}).get("isrc"),
        )


def is_spotify_url(value: str) -> bool:
    return bool(SPOTIFY_RE.search(value))
