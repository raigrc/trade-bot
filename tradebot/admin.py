"""Operator admin: inspect live state and re-arm the kill-switch after a halt.

The drawdown kill-switch is intentionally manual to re-arm — a 15% drawdown
means the strategy's assumptions may be broken, so a human must decide to resume.

    python -m tradebot.admin status
    python -m tradebot.admin clear-kill-switch
    python -m tradebot.admin status --mode live
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .clock import LiveClock, to_utc
from .config import load_config
from .journal import TradeJournal
from .persistence import StateStore


def _db(cfg, mode: str) -> Path:
    return Path(cfg.state_dir) / f"{cfg.symbols[0].replace('/', '_')}_{mode}.sqlite"


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Inspect state / re-arm the kill-switch.")
    ap.add_argument("command", choices=["status", "clear-kill-switch"])
    ap.add_argument("--mode", default=None, help="paper | live (default: config mode)")
    args = ap.parse_args()

    mode = args.mode or cfg.mode.value
    db = _db(cfg, mode)
    if not db.exists():
        print(f"No state DB at {db}. The bot hasn't run in '{mode}' mode yet.")
        return 1

    store = StateStore(db)
    journal = TradeJournal(db)
    risk = store.get("risk") or {}
    pf = store.get("portfolio") or {}
    meta = store.get("meta") or {}

    if args.command == "status":
        eq = journal.latest_equity()
        peak = risk.get("peak_equity", 0.0)
        dd = ((peak - eq) / peak) if (eq is not None and peak) else 0.0
        lp = meta.get("last_processed_ms")
        print(f"  mode               : {mode}")
        print(f"  latest equity      : {eq}")
        print(f"  peak equity        : {peak}")
        print(f"  drawdown from peak : {dd:.2%}  (kill-switch at {cfg.risk.max_drawdown_pct:.0%})")
        print(f"  KILL-SWITCH        : {'ENGAGED -> ' + risk.get('kill_switch_reason', '') if risk.get('kill_switch_engaged') else 'clear'}")
        print(f"  open position      : {pf.get('position')}")
        print(f"  consecutive losses : {risk.get('consecutive_losses', 0)}")
        print(f"  last bar processed : {to_utc(lp) if lp else None}")
        print(f"  trades logged      : {len(journal.all())}")
    else:  # clear-kill-switch
        if not risk.get("kill_switch_engaged"):
            print("Kill-switch is not engaged — nothing to do.")
        else:
            print(f"Re-arming kill-switch (was: {risk.get('kill_switch_reason')}).")
            risk["kill_switch_engaged"] = False
            risk["kill_switch_reason"] = ""
            risk["peak_equity"] = 0.0  # re-anchors to current equity on next bar
            store.put("risk", risk, LiveClock().now_ms())
            print("Done. The bot will resume trading on its next run.")

    store.close()
    journal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
