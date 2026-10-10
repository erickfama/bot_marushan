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
async def test_spotify_track_falls_back_to_youtube_music_when_youtube_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    requester = SimpleNamespace(id=1, display_name="User")
    wanted = SpotifyTrack(
        title="Te lo agradezco, pero no",
        artists=("Alejandro Sanz", "Shakira"),
        duration_ms=273_000,
        url="https://open.spotify.com/track/example",
    )
    candidate = SimpleNamespace(
        title="Te Lo Agradezco, Pero No",
        author="Alejandro Sanz, Shakira",
        length=273_000,
        extras=None,
    )
    calls: list[object] = []

    async def fake_search(_query: str, *, source):
        calls.append(source)
        return [] if source is wavelink.TrackSource.YouTube else [candidate]

    import wavelink

    monkeypatch.setattr(wavelink.Playable, "search", fake_search)

    result = await manager._resolve_spotify_track(wanted, requester)

    assert result is candidate
    assert calls == [wavelink.TrackSource.YouTube, wavelink.TrackSource.YouTubeMusic]
    assert candidate.extras["spotify_url"] == wanted.url


@pytest.mark.asyncio
async def test_spotify_track_retries_weak_youtube_match_on_youtube_music(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    requester = SimpleNamespace(id=1, display_name="User")
    wanted = SpotifyTrack(title="Creep", artists=("Radiohead",), duration_ms=238_000, url="spotify")
    weak = SimpleNamespace(title="Unrelated song", author="Someone", length=100_000, extras=None)
    strong = SimpleNamespace(title="Creep", author="Radiohead", length=238_000, extras=None)

    async def fake_search(_query: str, *, source):
        return [weak] if source is wavelink.TrackSource.YouTube else [strong]

    import wavelink

    monkeypatch.setattr(wavelink.Playable, "search", fake_search)

    result = await manager._resolve_spotify_track(wanted, requester)

    assert result is strong


@pytest.mark.asyncio
async def test_spotify_track_falls_back_when_youtube_search_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    requester = SimpleNamespace(id=1, display_name="User")
    wanted = SpotifyTrack(title="More", artists=("The Warning",), duration_ms=203_000, url="spotify")
    candidate = SimpleNamespace(
        title="MORE (Official Music Video)",
        author="The Warning",
        length=204_000,
        identifier="more",
        extras=None,
    )

    async def fake_search(_query: str, *, source):
        if source is wavelink.TrackSource.YouTube:
            raise RuntimeError("client unavailable")
        return [candidate]

    import wavelink

    monkeypatch.setattr(wavelink.Playable, "search", fake_search)

    result = await manager._resolve_spotify_track(wanted, requester)

    assert result is candidate
    assert candidate.extras["spotify_title"] == "More"


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


@pytest.mark.asyncio
async def test_concurrent_enqueues_start_only_one_track(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    player = SimpleNamespace(guild=SimpleNamespace(id=1), playing=False)
    played: list[object] = []

    async def fake_ensure_player(*_args, **_kwargs):
        return player

    async def fake_resolve(query, *_args, **_kwargs):
        return [SimpleNamespace(title=query, identifier=query)], 0

    async def fake_play(_player, track):
        played.append(track)
        await asyncio.sleep(0.01)
        player.playing = True

    monkeypatch.setattr(manager, "ensure_player", fake_ensure_player)
    monkeypatch.setattr(manager, "resolve", fake_resolve)
    monkeypatch.setattr(manager, "play_track", fake_play)

    await asyncio.gather(
        manager.enqueue(SimpleNamespace(id=1), SimpleNamespace(id=1), 10, "first"),
        manager.enqueue(SimpleNamespace(id=1), SimpleNamespace(id=2), 10, "second"),
    )

    assert [track.title for track in played] == ["first"]
    assert manager.session(1).queue.current.title == "first"
    assert [track.title for track in manager.session(1).queue.items] == ["second"]


@pytest.mark.asyncio
async def test_replaced_end_event_does_not_advance_queue() -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    current = SimpleNamespace(title="Current", identifier="current")
    following = SimpleNamespace(title="Following", identifier="following")
    session = manager.session(1)
    session.queue.current = current
    session.queue.add([following])
    player = SimpleNamespace(guild=SimpleNamespace(id=1))

    await manager.on_track_end(player, "replaced", current)  # type: ignore[arg-type]

    assert session.queue.current is current
    assert list(session.queue.items) == [following]


@pytest.mark.asyncio
async def test_stale_end_event_does_not_skip_new_current() -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    stale = SimpleNamespace(title="Stale", identifier="stale")
    current = SimpleNamespace(title="Current", identifier="current")
    following = SimpleNamespace(title="Following", identifier="following")
    session = manager.session(1)
    session.queue.current = current
    session.queue.add([following])
    player = SimpleNamespace(guild=SimpleNamespace(id=1))

    await manager.on_track_end(player, "finished", stale)  # type: ignore[arg-type]

    assert session.queue.current is current
    assert list(session.queue.items) == [following]


@pytest.mark.asyncio
async def test_failure_recovery_advances_without_track_end(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MusicManager(SimpleNamespace(), Settings(discord_token="token", discord_guild_id=1))
    broken = SimpleNamespace(title="Broken", identifier="broken", length=1000)
    following = SimpleNamespace(title="Following", identifier="following", length=1000)
    session = manager.session(1)
    session.queue.current = broken
    session.queue.add([following])
    played: list[object] = []

    class FakePlayer:
        guild = SimpleNamespace(id=1)
        position = 1000

        async def skip(self, *, force: bool):
            return broken

    async def no_wait(_seconds):
        return None

    async def fake_play(_player, track):
        played.append(track)

    monkeypatch.setattr("src.player.asyncio.sleep", no_wait)
    monkeypatch.setattr(manager, "play_track", fake_play)

    await manager.recover_failed_track(FakePlayer(), broken, cause="test", delay=0)  # type: ignore[arg-type]

    assert session.queue.current is following
    assert played == [following]
    assert list(session.queue.history) == [broken]
