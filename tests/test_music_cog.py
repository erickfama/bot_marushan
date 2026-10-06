from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.music_cog import MusicCog, unwrap_command_error
from src.player import MusicError


def test_unwrap_command_error_reaches_root_exception() -> None:
    root = MusicError("No encontré resultados reproducibles.")
    inner = RuntimeError("invoke")
    inner.original = root  # type: ignore[attr-defined]
    outer = RuntimeError("hybrid")
    outer.original = inner  # type: ignore[attr-defined]

    assert unwrap_command_error(outer) is root


@pytest.mark.asyncio
async def test_cog_reports_nested_music_error_as_expected() -> None:
    root = MusicError("Ya estoy siendo usado en **Pitudos**.")
    inner = RuntimeError("invoke")
    inner.original = root  # type: ignore[attr-defined]
    outer = RuntimeError("hybrid")
    outer.original = inner  # type: ignore[attr-defined]
    sent: list[tuple[str, bool]] = []

    async def send(message: str, *, ephemeral: bool) -> None:
        sent.append((message, ephemeral))

    ctx = SimpleNamespace(send=send, interaction=object(), command="play")
    cog = MusicCog(SimpleNamespace(), SimpleNamespace())

    await cog.cog_command_error(ctx, outer)  # type: ignore[arg-type]

    assert sent == [("⚠️ Ya estoy siendo usado en **Pitudos**.", True)]
