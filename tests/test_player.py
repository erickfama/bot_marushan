from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.player import MusicManager
from src.spotify import SpotifyTrack


class FakeSpotify:
    authenticated = True

    async def resolve(self, _query: str, *, limit: int):
        return [
            SpotifyTrack(title=f"Track {index}", artists=("Artist",), duration_ms=1000, url=str(index))
            for index in range(min(6, limit))
        ]

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_spotify_resolution_is_concurrent_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(discord_token="token", discord_guild_id=1)
    manager = MusicManager(SimpleNamespace(), settings, FakeSpotify())
    active = 0
    peak = 0
    progress: list[dict[str, int | str]] = []

    async def fake_resolve(track: SpotifyTrack, _requester):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep((6 - int(track.url)) * 0.002)
        active -= 1
        return track.title

    monkeypatch.setattr(manager, "_resolve_spotify_track", fake_resolve)
    tracks, omitted = await manager.resolve(
        "https://open.spotify.com/playlist/abc",
        SimpleNamespace(id=1, display_name="User"),
        lambda value: progress.append(dict(value)),
    )

    assert tracks == [f"Track {index}" for index in range(6)]
    assert omitted == 0
    assert peak == 4
    assert progress[-1]["resolved"] == 6


@pytest.mark.asyncio
async def test_spotify_resolution_counts_omitted_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1), FakeSpotify())

    async def fake_resolve(track: SpotifyTrack, _requester):
        return None if track.url in {"1", "4"} else track.title

    monkeypatch.setattr(manager, "_resolve_spotify_track", fake_resolve)
    tracks, omitted = await manager.resolve(
        "spotify:playlist:abc", SimpleNamespace(id=1, display_name="User")
    )
    assert len(tracks) == 4
    assert omitted == 2
