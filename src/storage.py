from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class MusicStorage:
    """Small durable store for one personal Discord server.

    SQLite keeps deployment self-contained and every mutation is committed
    immediately so a container restart cannot lose the latest queue change.
    """

    def __init__(self, path: str) -> None:
        database: Path | str
        if path == ":memory:":
            database = path
            self.path = Path(path)
        else:
            database = Path(path)
            database.parent.mkdir(parents=True, exist_ok=True)
            self.path = database
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_state (
                    guild_id INTEGER PRIMARY KEY,
                    volume INTEGER NOT NULL,
                    loop_mode TEXT NOT NULL,
                    autoplay INTEGER NOT NULL,
                    text_channel_id INTEGER,
                    voice_channel_id INTEGER,
                    current_track TEXT,
                    queue_tracks TEXT NOT NULL,
                    history_tracks TEXT NOT NULL,
                    filter_preset TEXT NOT NULL DEFAULT 'off',
                    stay_247 INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    uri TEXT,
                    artwork TEXT,
                    source TEXT NOT NULL,
                    requester TEXT NOT NULL,
                    requester_id INTEGER,
                    outcome TEXT NOT NULL DEFAULT 'started',
                    listened_ms INTEGER NOT NULL DEFAULT 0,
                    played_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS plays_guild_time ON plays(guild_id, played_at DESC);
                CREATE TABLE IF NOT EXISTS favorites (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    uri TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    artwork TEXT,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(guild_id, user_id, uri)
                );
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(guild_id, owner_id, name)
                );
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    uri TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    artwork TEXT,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    next_up INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    found INTEGER NOT NULL,
                    resolved INTEGER NOT NULL,
                    omitted INTEGER NOT NULL,
                    added INTEGER NOT NULL,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            self._ensure_column("guild_state", "filter_preset", "TEXT NOT NULL DEFAULT 'off'")
            self._ensure_column("guild_state", "stay_247", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("plays", "listened_ms", "INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def save_session(self, guild_id: int, payload: dict[str, Any]) -> None:
        now = int(time.time())
        values = (
            guild_id,
            payload["volume"],
            payload["loop_mode"],
            int(payload["autoplay"]),
            payload.get("text_channel_id"),
            payload.get("voice_channel_id"),
            self._json(payload.get("current_track")),
            self._json(payload.get("queue_tracks", [])),
            self._json(payload.get("history_tracks", [])),
            payload.get("filter_preset", "off"),
            int(payload.get("stay_247", False)),
            now,
        )
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO guild_state
                (guild_id,volume,loop_mode,autoplay,text_channel_id,voice_channel_id,current_track,
                 queue_tracks,history_tracks,filter_preset,stay_247,updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  volume=excluded.volume, loop_mode=excluded.loop_mode,
                  autoplay=excluded.autoplay, text_channel_id=excluded.text_channel_id,
                  voice_channel_id=excluded.voice_channel_id,
                  current_track=excluded.current_track, queue_tracks=excluded.queue_tracks,
                  history_tracks=excluded.history_tracks, filter_preset=excluded.filter_preset,
                  stay_247=excluded.stay_247, updated_at=excluded.updated_at""",
                values,
            )

    def load_session(self, guild_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM guild_state WHERE guild_id=?", (guild_id,)).fetchone()
        if row is None:
            return None
        return {
            "volume": row["volume"],
            "loop_mode": row["loop_mode"],
            "autoplay": bool(row["autoplay"]),
            "text_channel_id": row["text_channel_id"],
            "voice_channel_id": row["voice_channel_id"],
            "current_track": self._loads(row["current_track"], None),
            "queue_tracks": self._loads(row["queue_tracks"], []),
            "history_tracks": self._loads(row["history_tracks"], []),
            "filter_preset": row["filter_preset"],
            "stay_247": bool(row["stay_247"]),
        }

    def record_play(
        self,
        guild_id: int,
        track: dict[str, Any],
        requester_id: int | None = None,
        *,
        listened_ms: int | None = None,
        outcome: str = "completed",
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO plays
                (guild_id,title,author,duration_ms,uri,artwork,source,requester,requester_id,outcome,listened_ms,played_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id, track["title"], track["author"], track["duration"], track.get("uri"),
                    track.get("artwork"), track.get("source", "youtube"), track.get("requester", "Desconocido"),
                    requester_id, outcome, max(0, int(listened_ms if listened_ms is not None else track["duration"])),
                    int(time.time()),
                ),
            )

    def history(self, guild_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM plays WHERE guild_id=? ORDER BY played_at DESC LIMIT ?", (guild_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def statistics(self, guild_id: int, days: int = 30) -> dict[str, Any]:
        since = int(time.time()) - days * 86_400
        with self._lock:
            summary = self._db.execute(
                """SELECT COUNT(*) plays, COALESCE(SUM(listened_ms),0) duration_ms,
                COUNT(DISTINCT author) artists FROM plays WHERE guild_id=? AND played_at>=?""",
                (guild_id, since),
            ).fetchone()
            tracks = self._db.execute(
                """SELECT title,author,artwork,COUNT(*) plays FROM plays
                WHERE guild_id=? AND played_at>=? GROUP BY title,author
                ORDER BY plays DESC, MAX(played_at) DESC LIMIT 10""", (guild_id, since)
            ).fetchall()
            artists = self._db.execute(
                """SELECT author,COUNT(*) plays FROM plays WHERE guild_id=? AND played_at>=?
                GROUP BY author ORDER BY plays DESC LIMIT 10""", (guild_id, since)
            ).fetchall()
            hours = self._db.execute(
                """SELECT CAST(strftime('%H', played_at, 'unixepoch', 'localtime') AS INTEGER) hour,
                COUNT(*) plays FROM plays WHERE guild_id=? AND played_at>=?
                GROUP BY hour ORDER BY hour""", (guild_id, since)
            ).fetchall()
        return {
            "days": days, "plays": summary["plays"], "durationMs": summary["duration_ms"],
            "artists": summary["artists"], "topTracks": [dict(row) for row in tracks],
            "topArtists": [dict(row) for row in artists], "byHour": [dict(row) for row in hours],
        }

    def toggle_favorite(self, guild_id: int, user_id: int, track: dict[str, Any]) -> bool:
        uri = str(track.get("uri") or f"{track['author']}::{track['title']}")
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT 1 FROM favorites WHERE guild_id=? AND user_id=? AND uri=?",
                (guild_id, user_id, uri),
            ).fetchone()
            if existing:
                self._db.execute(
                    "DELETE FROM favorites WHERE guild_id=? AND user_id=? AND uri=?", (guild_id, user_id, uri)
                )
                return False
            self._db.execute(
                """INSERT INTO favorites VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, uri, track["title"], track["author"], track["duration"],
                 track.get("artwork"), track.get("source", "youtube"), int(time.time())),
            )
            return True

    def favorites(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM favorites WHERE guild_id=? AND user_id=? ORDER BY created_at DESC",
                (guild_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_playlist(self, guild_id: int, owner_id: int, name: str) -> int:
        clean = " ".join(name.split())
        if not clean or len(clean) > 80:
            raise ValueError("El nombre debe tener entre 1 y 80 caracteres")
        try:
            with self._lock, self._db:
                cursor = self._db.execute(
                    "INSERT INTO playlists(guild_id,owner_id,name,created_at) VALUES(?,?,?,?)",
                    (guild_id, owner_id, clean, int(time.time())),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Ya tienes una playlist con ese nombre") from exc

    def playlists(self, guild_id: int, owner_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT p.*, COUNT(t.id) tracks FROM playlists p
                LEFT JOIN playlist_tracks t ON t.playlist_id=p.id
                WHERE p.guild_id=? AND p.owner_id=? GROUP BY p.id ORDER BY p.created_at DESC""",
                (guild_id, owner_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def playlist_tracks(self, playlist_id: int, guild_id: int, owner_id: int) -> list[dict[str, Any]]:
        with self._lock:
            owner = self._db.execute(
                "SELECT 1 FROM playlists WHERE id=? AND guild_id=? AND owner_id=?",
                (playlist_id, guild_id, owner_id),
            ).fetchone()
            if owner is None:
                raise KeyError("Playlist no encontrada")
            rows = self._db.execute(
                "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY position,id", (playlist_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_playlist_tracks(
        self, playlist_id: int, guild_id: int, owner_id: int, tracks: list[dict[str, Any]]
    ) -> int:
        # Validate ownership before the destructive replacement.
        self.playlist_tracks(playlist_id, guild_id, owner_id)
        values = []
        for position, track in enumerate(tracks[:500], 1):
            uri = str(track.get("uri") or "").strip()
            if not uri:
                continue
            values.append(
                (
                    playlist_id, position, uri, str(track.get("title") or "Sin título"),
                    str(track.get("author") or "Desconocido"), int(track.get("duration") or track.get("duration_ms") or 0),
                    track.get("artwork"), str(track.get("source") or "youtube"),
                )
            )
        with self._lock, self._db:
            self._db.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
            self._db.executemany(
                """INSERT INTO playlist_tracks
                (playlist_id,position,uri,title,author,duration_ms,artwork,source)
                VALUES(?,?,?,?,?,?,?,?)""", values
            )
        return len(values)

    def delete_playlist(self, playlist_id: int, guild_id: int, owner_id: int) -> None:
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM playlists WHERE id=? AND guild_id=? AND owner_id=?",
                (playlist_id, guild_id, owner_id),
            )
            if not cursor.rowcount:
                raise KeyError("Playlist no encontrada")

    def save_import(self, job: dict[str, Any]) -> None:
        now = int(time.time())
        values = (
            job["id"], job["guild_id"], job["user_id"], job["query"], job["channel_id"],
            int(job["next_up"]), job["status"], job["source"], job["found"], job["resolved"],
            job["omitted"], job["added"], job.get("error"), job.get("created_at", now), now,
        )
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,source=excluded.source,
                found=excluded.found,resolved=excluded.resolved,omitted=excluded.omitted,
                added=excluded.added,error=excluded.error,updated_at=excluded.updated_at""", values
            )

    def recent_imports(self, guild_id: int, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM imports WHERE guild_id=? ORDER BY created_at DESC LIMIT ?", (guild_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _json(value: Any) -> str | None:
        return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
