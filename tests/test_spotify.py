from __future__ import annotations

import pytest

from src.spotify import SpotifyClient, SpotifyError, is_spotify_url


def test_detects_spotify_urls_and_uris() -> None:
    assert is_spotify_url("https://open.spotify.com/track/abc123")
    assert is_spotify_url("https://open.spotify.com/intl-es/track/abc123?si=test")
    assert is_spotify_url("https://spotify.link/short-code")
    assert is_spotify_url("spotify:playlist:abc123")
    assert not is_spotify_url("https://youtube.com/watch?v=abc")


@pytest.mark.asyncio
async def test_resolves_track_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SpotifyClient("id", "secret", "refresh")

    async def fake_get(_: str):
        return {
            "name": "Creep",
            "artists": [{"name": "Radiohead"}],
            "duration_ms": 238000,
            "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
            "external_ids": {"isrc": "GBAYE9200070"},
            "album": {"images": [{"url": "https://example.test/cover.jpg"}]},
        }

    monkeypatch.setattr(client, "_get", fake_get)
    tracks = await client.resolve("https://open.spotify.com/track/abc")
    assert tracks[0].title == "Creep"
    assert tracks[0].artists == ("Radiohead",)
    assert tracks[0].isrc == "GBAYE9200070"


@pytest.mark.asyncio
async def test_resolves_paginated_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SpotifyClient("id", "secret", "refresh")
    item = {"type": "track", "name": "One", "artists": [{"name": "Artist"}], "duration_ms": 1000}

    async def fake_get(_: str):
        return {"images": [], "items": {"items": [{"track": item}], "next": "next-page"}}

    async def fake_request(_method: str, _url: str):
        return {"items": [{"track": {**item, "name": "Two"}}], "next": None}

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "_request", fake_request)
    tracks = await client.resolve("spotify:playlist:abc")
    assert [track.title for track in tracks] == ["One", "Two"]


@pytest.mark.asyncio
async def test_rejects_invalid_spotify_url() -> None:
    client = SpotifyClient("id", "secret", "refresh")
    with pytest.raises(SpotifyError):
        await client.resolve("not spotify")


@pytest.mark.asyncio
async def test_public_track_does_not_require_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SpotifyClient()
    expected = client._track({"name": "Creep", "artists": [{"name": "Radiohead"}]})

    async def fake_public_track(url: str):
        assert url == "https://open.spotify.com/track/abc123"
        return expected

    monkeypatch.setattr(client, "_public_track", fake_public_track)
    tracks = await client.resolve("https://open.spotify.com/intl-es/track/abc123?si=test")
    assert tracks == [expected]


@pytest.mark.asyncio
async def test_public_playlist_explains_oauth_requirement() -> None:
    client = SpotifyClient()
    with pytest.raises(SpotifyError, match="OAuth"):
        await client.resolve("https://open.spotify.com/playlist/abc123")
