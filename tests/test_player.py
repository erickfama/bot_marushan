from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.player import MusicError, MusicManager
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


@pytest.mark.asyncio
async def test_youtube_search_falls_back_to_youtube_music(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    requester = SimpleNamespace(id=1, display_name="User")
    track = SimpleNamespace(title="Te quiero puta!", author="Rammstein", extras=None)
    calls: list[object] = []

    async def fake_search(_query: str, *, source):
        calls.append(source)
        return [] if source is not wavelink.TrackSource.YouTubeMusic else [track]

    import wavelink

    monkeypatch.setattr(wavelink.Playable, "search", fake_search)
    tracks, omitted = await manager.resolve("te quiero puta", requester)

    assert tracks == [track]
    assert omitted == 0
    assert calls == [wavelink.TrackSource.YouTube, wavelink.TrackSource.YouTubeMusic]
    assert track.extras["requester_id"] == requester.id


@pytest.mark.asyncio
async def test_direct_url_does_not_use_search_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    calls: list[object] = []

    async def fake_search(_query: str, *, source):
        calls.append(source)
        return []

    import wavelink

    monkeypatch.setattr(wavelink.Playable, "search", fake_search)
    with pytest.raises(MusicError, match="No encontré resultados reproducibles"):
        await manager.resolve("https://youtu.be/missing", SimpleNamespace(id=1, display_name="User"))

    assert calls == [None]


@pytest.mark.asyncio
async def test_play_track_sends_original_track_to_lavalink() -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    track = SimpleNamespace(title="Song")
    played: list[tuple[object, int]] = []

    class FakePlayer:
        guild = SimpleNamespace(id=1)

        async def play(self, item, *, volume: int):
            played.append((item, volume))

    await manager.play_track(FakePlayer(), track)  # type: ignore[arg-type]

    assert played == [(track, 75)]


@pytest.mark.asyncio
async def test_play_next_skips_failed_track_and_starts_following(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    first = SimpleNamespace(title="Broken")
    second = SimpleNamespace(title="Working")
    session = manager.session(1)
    session.queue.add([first, second])
    attempts: list[object] = []

    async def fake_play(_player, track):
        attempts.append(track)
        if track is first:
            raise RuntimeError("source failed")

    monkeypatch.setattr(manager, "play_track", fake_play)
    player = SimpleNamespace(guild=SimpleNamespace(id=1))

    started = await manager.play_next(player)  # type: ignore[arg-type]

    assert started is second
    assert attempts == [first, second]
    assert list(session.queue.history) == [first]
    assert session.queue.current is second


@pytest.mark.asyncio
async def test_enqueue_does_not_confirm_when_every_track_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    track = SimpleNamespace(title="Broken")
    player = SimpleNamespace(guild=SimpleNamespace(id=1), playing=False)

    async def fake_ensure_player(*_args, **_kwargs):
        return player

    async def fake_resolve(*_args, **_kwargs):
        return [track], 0

    async def fake_play(*_args, **_kwargs):
        raise RuntimeError("source failed")

    monkeypatch.setattr(manager, "ensure_player", fake_ensure_player)
    monkeypatch.setattr(manager, "resolve", fake_resolve)
    monkeypatch.setattr(manager, "play_track", fake_play)

    with pytest.raises(MusicError, match="no pudo iniciar ninguna pista"):
        await manager.enqueue(
            SimpleNamespace(id=1),
            SimpleNamespace(id=1, display_name="User"),
            10,
            "Broken",
        )
