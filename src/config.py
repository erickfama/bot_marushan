from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _secret(name: str, default: str | None = None) -> str | None:
    file_name = os.getenv(f"{name}_FILE")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.getenv(name, default)


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except ValueError as exc:
        raise RuntimeError(f"{name} debe ser un número entero") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    discord_guild_id: int
    lavalink_uri: str = "http://localhost:2333"
    lavalink_password: str = "youshallnotpass"
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    spotify_refresh_token: str | None = None
    default_volume: int = 75
    idle_timeout_seconds: int = 600
    max_queue_size: int = 500
    log_level: str = "INFO"
    bot_display_name: str = "Bot Nissin"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = _secret("DISCORD_TOKEN")
        guild_id = _secret("DISCORD_GUILD_ID")
        lavalink_password = _secret("LAVALINK_PASSWORD", "youshallnotpass")
        if not token:
            raise RuntimeError("Falta DISCORD_TOKEN o DISCORD_TOKEN_FILE")
        if not guild_id or not guild_id.isdigit():
            raise RuntimeError("DISCORD_GUILD_ID debe contener el ID numérico del servidor")
        volume = _integer("DEFAULT_VOLUME", 75)
        queue_size = _integer("MAX_QUEUE_SIZE", 500)
        if not 1 <= volume <= 150:
            raise RuntimeError("DEFAULT_VOLUME debe estar entre 1 y 150")
        if not 1 <= queue_size <= 500:
            raise RuntimeError("MAX_QUEUE_SIZE debe estar entre 1 y 500")
        return cls(
            discord_token=token,
            discord_guild_id=int(guild_id),
            lavalink_uri=os.getenv("LAVALINK_URI", "http://localhost:2333"),
            lavalink_password=lavalink_password or "youshallnotpass",
            spotify_client_id=_secret("SPOTIFY_CLIENT_ID"),
            spotify_client_secret=_secret("SPOTIFY_CLIENT_SECRET"),
            spotify_refresh_token=_secret("SPOTIFY_REFRESH_TOKEN"),
            default_volume=volume,
            idle_timeout_seconds=_integer("IDLE_TIMEOUT_SECONDS", 600),
            max_queue_size=queue_size,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            bot_display_name=os.getenv("BOT_DISPLAY_NAME", "Bot Nissin").strip() or "Bot Nissin",
        )

    @property
    def spotify_enabled(self) -> bool:
        return all((self.spotify_client_id, self.spotify_client_secret, self.spotify_refresh_token))
