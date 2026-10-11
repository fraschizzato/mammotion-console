import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backend.config import sanitize


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, root: Path):
        self._preferences_cache = None
        self.root = root.resolve()
        for folder in ("db", "maps", "previews", "backups", "logs"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.database = self.root / "db/mammotion-console.sqlite3"
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone():
                versions = [row[0] for row in db.execute("SELECT version FROM schema_version")]
                if versions != [1]:
                    raise RuntimeError(
                        "Unsupported database schema; use the matching application version or restore a compatible backup"
                    )
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS snapshots(
                    id TEXT PRIMARY KEY, device TEXT NOT NULL, set_id TEXT NOT NULL,
                    version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    name TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
                    manifest TEXT NOT NULL, UNIQUE(set_id,version));
                CREATE INDEX IF NOT EXISTS fingerprint_idx ON snapshots(device,fingerprint);
                CREATE TABLE IF NOT EXISTS documents(
                    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    updated TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL,
                    message TEXT NOT NULL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_kind_id ON events(kind,id DESC);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, kind: str, message: str, details=None):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(created,kind,message,details) VALUES(?,?,?,?)",
                (now(), kind, message, json.dumps(sanitize(details or {}))),
            )

    def event_coalesced(self, kind: str, message: str, details=None, seconds: int = 21600):
        """Persist an event unless the latest event is the same and still recent."""
        created = now()
        with self.connect() as db:
            previous = db.execute(
                "SELECT created,message FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if previous and previous["message"] == message:
                age = datetime.fromisoformat(created) - datetime.fromisoformat(previous["created"])
                if age.total_seconds() < seconds:
                    return False
            db.execute(
                "INSERT INTO events(created,kind,message,details) VALUES(?,?,?,?)",
                (created, kind, message, json.dumps(sanitize(details or {}))),
            )
        return True

    def events(self, kind: str | None = None):
        with self.connect() as db:
            if kind is None:
                rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT 100", (kind,)
                ).fetchall()
        return [dict(r) | {"details": json.loads(r["details"])} for r in rows]

    def documents(self, kind: str):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM documents WHERE kind=? ORDER BY updated DESC", (kind,)
            ).fetchall()
        return [json.loads(r["body"]) | {"id": r["id"], "updated": r["updated"]} for r in rows]

    def put(self, kind: str, body: dict, key: str | None = None):
        if kind == "settings":
            self._preferences_cache = None
        key = key or str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated=excluded.updated",
                (kind, key, json.dumps(sanitize(body)), now()),
            )
        if kind == "settings":
            self._preferences_cache = None
        return body | {"id": key}

    def delete(self, kind: str, key: str):
        if kind == "settings":
            self._preferences_cache = None
        with self.connect() as db:
            db.execute("DELETE FROM documents WHERE kind=? AND id=?", (kind, key))
        if kind == "settings":
            self._preferences_cache = None

    def preferences(self):
        if self._preferences_cache is None:
            items = self.documents("settings")
            self._preferences_cache = items[0] if items else {}
        return dict(self._preferences_cache)
