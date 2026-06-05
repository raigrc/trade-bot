"""Generate the trade logbook + weekly reflections on demand from the journal.

    python -m tradebot.report                 # most recent week with activity
    python -m tradebot.report --all            # every week the bot ran
    python -m tradebot.report --logbook        # full trade logbook (markdown)
    python -m tradebot.report --mode live      # read the live DB instead of paper
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .journal import TradeJournal
from .reporting import _WEEK_MS, build_weekly_report, iso_week_bounds, write_report


def _db_path(cfg, mode: str) -> Path:
    return Path(cfg.state_dir) / f"{cfg.symbols[0].replace('/', '_')}_{mode}.sqlite"


def _active_week_starts(journal: TradeJournal) -> list[int]:
    rows = journal.equity_between(0, 2**62)
    stamps = [ts for ts, _ in rows] + [t.exit_ms for t in journal.all()]
    if not stamps:
        return []
    lo = iso_week_bounds(min(stamps))[0]
    hi = iso_week_bounds(max(stamps))[0]
    return list(range(lo, hi + 1, _WEEK_MS))


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Trade logbook + weekly reflections.")
    ap.add_argument("--mode", default="paper", help="which journal DB: paper | live")
    ap.add_argument("--all", action="store_true", help="report every week with activity")
    ap.add_argument("--logbook", action="store_true", help="export the full trade logbook")
    args = ap.parse_args()

    db = _db_path(cfg, args.mode)
    if not db.exists():
        print(f"No journal at {db}. Run the bot (paper/live) first.")
        return 1
    journal = TradeJournal(db)

    if args.logbook:
        text = journal.export_markdown()
        out = Path(cfg.reports_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "logbook.md").write_text(text, encoding="utf-8")
        print(text)
        print(f"\n-> {out / 'logbook.md'}")
        return 0

    weeks = _active_week_starts(journal)
    if not weeks:
        print("No data yet — the bot hasn't recorded any bars.")
        return 0
    targets = weeks if args.all else [weeks[-1]]
    for ws in targets:
        label, md = build_weekly_report(journal, cfg, ws)
        path = write_report(md, label, cfg.reports_dir)
        print(md)
        print(f"\n-> {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
