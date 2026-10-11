from __future__ import annotations

from src.storage import MusicStorage


def test_session_survives_reopening_database(tmp_path) -> None:
    path = tmp_path / "music.db"
    storage = MusicStorage(str(path))
    storage.save_session(
        7,
        {
            "volume": 82,
            "loop_mode": "queue",
            "autoplay": True,
            "text_channel_id": 10,
            "voice_channel_id": 11,
            "current_track": {"encoded": "current"},
            "queue_tracks": [{"encoded": "next"}],
            "history_tracks": [{"encoded": "before"}],
        },
    )
    storage.close()

    restored = MusicStorage(str(path)).load_session(7)
    assert restored is not None
    assert restored["volume"] == 82
    assert restored["loop_mode"] == "queue"
    assert restored["autoplay"] is True
    assert restored["current_track"] == {"encoded": "current"}
    assert restored["queue_tracks"] == [{"encoded": "next"}]


def test_history_statistics_and_favorites(tmp_path) -> None:
    storage = MusicStorage(str(tmp_path / "music.db"))
    track = {
        "title": "Creep",
        "author": "Radiohead",
        "duration": 238_000,
        "uri": "https://example.test/creep",
        "artwork": "https://example.test/cover.jpg",
        "source": "youtube",
        "requester": "A",
    }
    storage.record_play(1, track, 20)
    storage.record_play(1, track, 20)

    assert [item["title"] for item in storage.history(1)] == ["Creep", "Creep"]
    statistics = storage.statistics(1)
    assert statistics["plays"] == 2
    assert statistics["durationMs"] == 476_000
    assert statistics["topTracks"][0]["plays"] == 2

    assert storage.toggle_favorite(1, 20, track) is True
    assert storage.favorites(1, 20)[0]["title"] == "Creep"
    assert storage.toggle_favorite(1, 20, track) is False
    assert storage.favorites(1, 20) == []


def test_import_jobs_are_updated_and_listed(tmp_path) -> None:
    storage = MusicStorage(str(tmp_path / "music.db"))
    job = {
        "id": "abc",
        "guild_id": 1,
        "user_id": 2,
        "query": "playlist",
        "channel_id": 3,
        "next_up": False,
        "status": "queued",
        "source": "unknown",
        "found": 0,
        "resolved": 0,
        "omitted": 0,
        "added": 0,
        "error": None,
        "created_at": 100,
    }
    storage.save_import(job)
    job.update(status="complete", source="spotify", found=5, resolved=4, omitted=1, added=4)
    storage.save_import(job)

    saved = storage.recent_imports(1)
    assert len(saved) == 1
    assert saved[0]["status"] == "complete"
    assert saved[0]["added"] == 4


def test_saved_playlists_replace_tracks_and_enforce_owner(tmp_path) -> None:
    storage = MusicStorage(str(tmp_path / "music.db"))
    playlist_id = storage.create_playlist(1, 20, "Para trabajar")
    count = storage.replace_playlist_tracks(
        playlist_id,
        1,
        20,
        [
            {
                "title": "Creep", "author": "Radiohead", "duration": 238_000,
                "uri": "https://example.test/creep", "source": "youtube",
            }
        ],
    )
    assert count == 1
    assert storage.playlists(1, 20)[0]["tracks"] == 1
    assert storage.playlist_tracks(playlist_id, 1, 20)[0]["title"] == "Creep"

    import pytest

    with pytest.raises(KeyError):
        storage.playlist_tracks(playlist_id, 1, 21)
    storage.delete_playlist(playlist_id, 1, 20)
    assert storage.playlists(1, 20) == []
