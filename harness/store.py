"""Transactional state transitions and append-only execution events."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "session.sqlite", timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        """)

    def load(self) -> dict:
        row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, state: dict, kind: str, data: dict | None = None):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state),)
            )
            self.db.execute(
                "INSERT INTO events(at, kind, data) VALUES (?, ?, ?)",
                (time.time(), kind, json.dumps(data or {})),
            )

    def event(self, kind: str, data: dict):
        self.save(self.load(), kind, data)

    def events(self) -> list[dict]:
        return [
            {"seq": row[0], "at": row[1], "kind": row[2], "data": json.loads(row[3])}
            for row in self.db.execute(
                "SELECT seq,at,kind,data FROM events ORDER BY seq"
            )
        ]

    @contextlib.contextmanager
    def lock(self):
        # Kernel releases flock even on SIGKILL. Only one local runner mutates
        # this session at a time; read-only inspectors may still use SQLite.
        import fcntl

        with (self.directory / "runner.lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another runner owns this session") from None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
