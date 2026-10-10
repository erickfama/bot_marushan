from __future__ import annotations

import pytest

from src.spotify import SpotifyClient, SpotifyError, is_spotify_url


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status = status
        self.payload = payload or {}
        self._text = text
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload

    async def text(self):
        return self._text


class FakeSession:
    closed = False

    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def request(self, *_args, **_kwargs):
        return self.response


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
async def test_playlist_items_endpoint_is_used_when_metadata_has_no_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpotifyClient("id", "secret", "refresh")
    item = {"type": "track", "name": "One", "artists": [{"name": "Artist"}], "duration_ms": 1000}
    calls: list[str] = []

    async def fake_get(path: str):
        calls.append(path)
        if path == "/playlists/abc":
            return {"images": []}
        return {"items": [{"item": item}], "next": None}

    monkeypatch.setattr(client, "_get", fake_get)

    tracks = await client.resolve("spotify:playlist:abc")

    assert [track.title for track in tracks] == ["One"]
    assert calls == ["/playlists/abc", "/playlists/abc/items"]


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


@pytest.mark.asyncio
async def test_owned_playlist_error_is_explained_on_403() -> None:
    client = SpotifyClient("id", "secret", "refresh", session=FakeSession(FakeResponse(403)))
    client._access_token = "token"
    client._expires_at = float("inf")
    with pytest.raises(SpotifyError, match="propias o colaborativas"):
        await client._request("GET", "https://api.spotify.com/v1/playlists/abc/items")


@pytest.mark.asyncio
async def test_quota_exceeded_is_not_retried() -> None:
    response = FakeResponse(429, {"reason": "QUOTA_EXCEEDED"})
    client = SpotifyClient("id", "secret", "refresh", session=FakeSession(response))
    client._access_token = "token"
    client._expires_at = float("inf")
    with pytest.raises(SpotifyError, match="cuota"):
        await client._request("GET", "https://api.spotify.com/v1/tracks/abc")


@pytest.mark.asyncio
async def test_nested_quota_exceeded_is_not_retried() -> None:
    response = FakeResponse(429, {"error": {"reason": "QUOTA_EXCEEDED"}})
    client = SpotifyClient("id", "secret", "refresh", session=FakeSession(response))
    client._access_token = "token"
    client._expires_at = float("inf")
    with pytest.raises(SpotifyError, match="cuota"):
        await client._request("GET", "https://api.spotify.com/v1/tracks/abc")
