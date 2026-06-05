"""SQLite trade journal — an immutable record of every closed round-trip.

Shares the same DB file as the StateStore. Wire ``TradeJournal.record`` into
the engine's ``on_trade`` callback.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .enums import Side
from .types import Trade

_TRADE_COLS = ("symbol", "side", "qty", "entry_price", "exit_price", "entry_ms", "exit_ms",
               "pnl", "fees", "return_pct", "bars_held", "entry_reason", "exit_reason")


def _row_to_trade(r: tuple) -> Trade:
    return Trade(r[0], Side(r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12])


class TradeJournal:
    """Immutable record of every closed round-trip + an equity time series.

    This is the live logbook. ``record`` is wired to the engine's on_trade
    callback; ``record_equity`` to on_equity. The reporting module reads these
    to build weekly reflections.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")  # wait, don't error, if a run overlaps
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS trades ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL,"
            "entry_price REAL, exit_price REAL, entry_ms INTEGER, exit_ms INTEGER,"
            "pnl REAL, fees REAL, return_pct REAL, bars_held INTEGER,"
            "entry_reason TEXT, exit_reason TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS equity (ts_ms INTEGER PRIMARY KEY, equity REAL NOT NULL)"
        )
        self.conn.commit()

    # -- writes ---------------------------------------------------------------
    def record(self, t: Trade) -> None:
        self.conn.execute(
            f"INSERT INTO trades({','.join(_TRADE_COLS)}) VALUES({','.join('?' * len(_TRADE_COLS))})",
            (t.symbol, t.side.value, t.qty, t.entry_price, t.exit_price, t.entry_ms, t.exit_ms,
             t.pnl, t.fees, t.return_pct, t.bars_held, t.entry_reason, t.exit_reason),
        )
        self.conn.commit()

    def record_equity(self, ts_ms: int, equity: float) -> None:
        self.conn.execute(
            "INSERT INTO equity(ts_ms, equity) VALUES(?,?) "
            "ON CONFLICT(ts_ms) DO UPDATE SET equity=excluded.equity",
            (int(ts_ms), float(equity)),
        )
        self.conn.commit()

    # -- reads ----------------------------------------------------------------
    def all(self) -> list[Trade]:
        rows = self.conn.execute(
            f"SELECT {','.join(_TRADE_COLS)} FROM trades ORDER BY id"
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def trades_between(self, start_ms: int, end_ms: int) -> list[Trade]:
        """Trades CLOSED within [start_ms, end_ms)."""
        rows = self.conn.execute(
            f"SELECT {','.join(_TRADE_COLS)} FROM trades WHERE exit_ms >= ? AND exit_ms < ? ORDER BY exit_ms",
            (int(start_ms), int(end_ms)),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def equity_between(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        return self.conn.execute(
            "SELECT ts_ms, equity FROM equity WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
            (int(start_ms), int(end_ms)),
        ).fetchall()

    def equity_at_or_before(self, ms: int) -> float | None:
        row = self.conn.execute(
            "SELECT equity FROM equity WHERE ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1", (int(ms),)
        ).fetchone()
        return float(row[0]) if row else None

    def latest_equity(self) -> float | None:
        row = self.conn.execute("SELECT equity FROM equity ORDER BY ts_ms DESC LIMIT 1").fetchone()
        return float(row[0]) if row else None

    def export_markdown(self) -> str:
        """Human-readable logbook of every trade."""
        trades = self.all()
        lines = [f"# Trade logbook ({len(trades)} trades)", "",
                 "| # | exit (UTC) | symbol | entry | exit | qty | PnL | ret% | held | entry reason | exit reason |",
                 "|--:|---|---|--:|--:|--:|--:|--:|--:|---|---|"]
        for i, t in enumerate(trades, 1):
            dt = datetime.fromtimestamp(t.exit_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"| {i} | {dt} | {t.symbol} | {t.entry_price:.2f} | {t.exit_price:.2f} | {t.qty:.6f} | "
                f"{t.pnl:+.2f} | {t.return_pct:+.2%} | {t.bars_held} | {t.entry_reason} | {t.exit_reason} |"
            )
        return "\n".join(lines)

    def close(self) -> None:
        self.conn.close()
