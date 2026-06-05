"""SQLite state store — survives restarts so the bot never double-trades or
loses its drawdown peak / kill-switch. Shares one DB file with the trade
journal so state and trades can't disagree.

Stores JSON blobs keyed by name: portfolio, risk, strategy, meta
(last_processed_bar_ms per symbol).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")  # wait, don't error, if a run overlaps
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS state ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ms INTEGER NOT NULL)"
        )
        self.conn.commit()

    def get(self, key: str) -> Optional[dict]:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict, now_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value,updated_ms) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (key, json.dumps(value), now_ms),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
