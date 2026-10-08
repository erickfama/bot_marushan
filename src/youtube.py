from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be", "m.youtube.com"}
RADIO_QUERY_KEYS = {"start_radio", "index", "rv"}


class YoutubeStreamResolver:
    """Normalize YouTube links before Lavalink resolves and plays them."""

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
