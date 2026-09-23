from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import wavelink
import yt_dlp

LOGGER = logging.getLogger(__name__)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be", "m.youtube.com"}
RADIO_QUERY_KEYS = {"start_radio", "index", "rv"}


class YoutubeResolverError(RuntimeError):
    pass


class _YtdlpLogger:
    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            LOGGER.debug("yt_dlp %s", message)

    def warning(self, message: str) -> None:
        LOGGER.warning("yt_dlp_warning %s", message)

    def error(self, message: str) -> None:
        LOGGER.error("yt_dlp_error %s", message)


class YoutubeStreamResolver:
    """Resolve a YouTube page to a short-lived HTTP audio URL at playback time."""

    async def playable(self, track: wavelink.Playable) -> wavelink.Playable:
        if not self.is_youtube_url(track.uri):
            return track
        stream_url = await asyncio.to_thread(self._extract_stream_url, track.uri)
        results = await wavelink.Playable.search(stream_url)
        streams = list(results.tracks) if isinstance(results, wavelink.Playlist) else list(results)
        if not streams:
            raise YoutubeResolverError("Lavalink no pudo abrir el stream de audio resuelto.")
        return streams[0]

    @staticmethod
    def is_youtube_url(url: str | None) -> bool:
        if not url:
            return False
        return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS

    @staticmethod
    def without_radio(url: str) -> str:
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
            return url
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key == "start_radio" and value == "1" for key, value in query):
            return url
        cleaned = [
            (key, value)
            for key, value in query
            if key not in RADIO_QUERY_KEYS and not (key == "list" and value.startswith("RD"))
        ]
        return urlunparse(parsed._replace(query=urlencode(cleaned)))

    def _extract_stream_url(self, url: str) -> str:
        last_error: Exception | None = None
        for client in ("visionos", "tv_simply"):
            options = {
                "format": "bestaudio[protocol^=http]/bestaudio",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 2,
                "cachedir": "/tmp/yt-dlp",
                "logger": _YtdlpLogger(),
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                stream_url = str(info.get("url") or "") if info else ""
                if not stream_url:
                    continue
                response = requests.get(stream_url, headers={"Range": "bytes=0-0"}, stream=True, timeout=15)
                try:
                    if response.status_code < 400:
                        return stream_url
                    last_error = RuntimeError(f"YouTube devolvió HTTP {response.status_code} para {client}")
                finally:
                    response.close()
            except (yt_dlp.utils.DownloadError, requests.RequestException) as exc:
                last_error = exc
                LOGGER.warning("youtube_client_failed client=%s error=%s", client, type(exc).__name__)
        raise YoutubeResolverError("YouTube rechazó temporalmente la reproducción de esta pista.") from last_error
