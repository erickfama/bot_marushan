from __future__ import annotations

import pytest

from src.config import Settings


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("default_volume", 151, "DEFAULT_VOLUME"),
        ("max_queue_size", 0, "MAX_QUEUE_SIZE"),
        ("idle_timeout_seconds", 0, "IDLE_TIMEOUT_SECONDS"),
        ("web_port", 70_000, "WEB_PORT"),
        ("public_base_url", "music.orza.mx", "PUBLIC_BASE_URL"),
        ("log_level", "VERBOSE", "LOG_LEVEL"),
    ],
)
def test_settings_reject_invalid_runtime_values(field: str, value: object, message: str) -> None:
    values = {"discord_token": "token", "discord_guild_id": 1, field: value}
    with pytest.raises(RuntimeError, match=message):
        Settings(**values)
