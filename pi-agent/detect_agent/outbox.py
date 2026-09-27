"""
Durable store-and-forward outbox (SQLite, WAL).

Every event gets a device sequence number that is persisted and strictly
increasing across restarts. The backend enforces UNIQUE(device_id, seq), so a
batch that is re-sent after a lost response is accepted once and ignored after
(no duplicate alerts).

Events stay in the outbox until the backend acknowledges them, so a network
outage delays alerts but does not lose them. The outbox is capped: if it ever
fills (weeks offline), the OLDEST PASS counts are dropped first; alarms and
faults are never dropped to make room.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

MAX_ROWS = 50_000


class Outbox:
    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self._path = os.path.join(state_dir, "outbox.sqlite3")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")   # survive power loss
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            " seq INTEGER PRIMARY KEY,"
            " event_type TEXT NOT NULL,"
            " body TEXT NOT NULL,"
            " created_ms INTEGER NOT NULL)"
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL)")
        self._db.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('last_seq', 0)")

    @property
    def path(self) -> str:
        return self._path

    def put(self, event: dict[str, Any], created_ms: int) -> int:
        """Persist one event; returns its sequence number."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                seq = self._db.execute("SELECT v FROM meta WHERE k='last_seq'").fetchone()[0] + 1
                self._db.execute("UPDATE meta SET v=? WHERE k='last_seq'", (seq,))
                event = dict(event, seq=seq)
                self._db.execute(
                    "INSERT INTO outbox (seq, event_type, body, created_ms) VALUES (?,?,?,?)",
                    (seq, event["type"], json.dumps(event, separators=(",", ":")), created_ms),
                )
                self._enforce_cap()
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return seq

    def _enforce_cap(self) -> None:
        n = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        excess = n - MAX_ROWS
        if excess > 0:
            self._db.execute(
                "DELETE FROM outbox WHERE seq IN ("
                " SELECT seq FROM outbox WHERE event_type='PASS' ORDER BY seq LIMIT ?)",
                (excess,),
            )

    def peek(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM outbox ORDER BY seq LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def ack(self, seqs: list[int]) -> None:
        if not seqs:
            return
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE seq=?", [(s,) for s in seqs])

    def depth(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def last_seq(self) -> int:
        with self._lock:
            return self._db.execute("SELECT v FROM meta WHERE k='last_seq'").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
