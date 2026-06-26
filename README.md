# trade-bot

A **capital-preservation-first** automated crypto trading bot for Binance (spot),
built in Python with [ccxt](https://github.com/ccxt/ccxt).

> ## ⚠️ Read this first — honest risk framing
>
> **No trading bot can guarantee a profit.** The base rate is brutal: most retail
> algorithmic strategies lose money after fees, and many "profitable" ones merely
> trail a simple buy-and-hold. The only thing genuinely within our control is
> **risk management** — not blowing up the account.
>
> This bot is engineered around that: realistic backtesting that refuses to fool
> us, strict per-trade loss limits, a hard drawdown kill-switch, and a deliberate
> **backtest → paper → tiny live** rollout where each stage must pass explicit
> go/no-go gates before the next.
>
> The realistic win condition is: *capture part of the trends, lose less than
> buy-and-hold in bear markets, net of fees, without blowing up.* If honest
> walk-forward testing shows no edge after costs, the correct, capital-preserving
> decision is **not to trade it live.** Only ever risk money you can afford to
> lose entirely.

## Design principle

The **same** strategy, risk, and portfolio-accounting code runs identically in
backtest, paper, and live. Only two things are swapped by dependency injection:

- the **DataFeed** — historical parquet (backtest) vs live ccxt (paper/live)
- the **ExecutionEngine** — simulated fills vs real ccxt orders

This structurally eliminates the classic "great in backtest, lost real money
live" divergence. See `tradebot/engine.py` (one shared `Engine.step`),
`tradebot/clock.py` (the time seam), and `tradebot/risk.py` (the loss-limit core).

## Setup (Windows / Python 3.13)

```powershell
# 1. install (editable)
pip install -e ".[dev]"

# 2. fetch historical data for backtests (one-time, ~1 min)
python -m scripts.fetch_data --symbols BTC/USDT ETH/USDT --timeframes 4h 1d --start 2020-01-01

# 3. START — paper-sim needs NO keys, NO account, NO real money:
python -m tradebot.main --once          # one cycle now (uses config.yaml: paper-sim, breakout)
python -m tradebot.main                 # run continuously (Ctrl-C to stop)
```

`config.yaml` ships ready for **paper-sim** (live Binance mainnet prices + simulated fills + virtual
money). No `.env` is required until you go to testnet or live. (Telegram alerts are optional but
recommended so you see kill-switch/error alerts — add keys to `.env`, see `.env.example`.)

### Run it unattended (optional)
Each `--once` run processes the latest closed candle and exits, resuming from SQLite — so a scheduler
can drive it. To register an hourly Windows task (you run this yourself; it runs code on a schedule):

```powershell
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\DELL\Desktop\Rai\trade-bot\scripts\run_paper.ps1"'
$t = New-ScheduledTaskTrigger -Once -At "00:00"; $t.RepetitionInterval = (New-TimeSpan -Hours 1); $t.RepetitionDuration = (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "TradeBot-Paper-BTCUSDT" -Action $a -Trigger $t -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew) -Force
# stop/remove later:  Disable-ScheduledTask -TaskName "TradeBot-Paper-BTCUSDT"   /   Unregister-ScheduledTask -TaskName "TradeBot-Paper-BTCUSDT" -Confirm:$false
```

### Monitor / operate
```powershell
python -m tradebot.admin status          # equity, drawdown, kill-switch, open position, last bar
python -m tradebot.report                # this week's reflection
python -m tradebot.report --logbook      # full trade logbook
python -m tradebot.admin clear-kill-switch   # re-arm after a 15% drawdown halt (manual, deliberate)
Get-Content logs\paper.log -Tail 20      # recent runs
```

## Workflow

| Stage | Command | Gate before advancing |
|---|---|---|
| Backtest | `python -m tradebot.main` (mode: backtest) | +expectancy after costs, profit factor ≥1.3, Calmar ≥0.5, MDD ≤~25%, ≥100 OOS trades |
| Paper (sim) | `mode: paper`, `paper_execution: sim` — **no keys needed** | live mainnet prices + simulated fills + virtual money; forward-test the loop |
| Paper (testnet) | `mode: paper`, `paper_execution: testnet` + testnet keys | 4–8 wks; results consistent with backtest; validates real order placement |
| Tiny live | set `mode: live` + `confirm_live: true` | tiny capital; MDD-breach kill-switch armed |

`python -m tradebot.main --mode paper --once` runs a single live cycle and exits — a quick smoke test of the loop with no account.

### Graduating to live (only when earned)
1. A strategy must **PASS the go/no-go gate** in walk-forward: `python -m tradebot.walkforward --strategy <name>`.
   (As of 2026-06-27, none does — daily `tsmom`/BTC is closest: positive expectancy, PF 1.58, and
   survives 2× costs, but narrowly misses Calmar/Sortino/trade-count. It is being **forward
   paper-tested** on daily bars; see `docs/strategy_gate_results.md`.)
2. Run **paper** for several weeks; weekly reflections should stay consistent with backtest.
3. Validate real order placement on **testnet**: add testnet keys to `.env`, set `paper_execution: testnet`.
4. Go live with **tiny** capital you can lose entirely: set `mode: live` + `confirm_live: true` + add live keys
   (spot-only, withdrawals disabled). The bot refuses to start live without that flag.

## Logbook & weekly reflection

Every trade is recorded to a SQLite journal (`state/<symbol>_<mode>.sqlite`) along with an equity
time series. While the bot runs, it auto-writes a **weekly reflection** to `reports/` each time a new
ISO week begins and alerts you. Generate them on demand too:

```powershell
python -m tradebot.report                 # latest week's reflection (paper journal)
python -m tradebot.report --all            # every week the bot ran
python -m tradebot.report --logbook        # full trade logbook -> reports/logbook.md
python -m tradebot.report --mode live      # read the live journal instead
```

The reflection is rule-based (no LLM): weekly P&L / win rate / profit factor, equity + intra-week
drawdown, largest single-trade loss vs the 2% hard cap, drawdown vs the 15% kill-switch, loss streaks,
and a plain-language capital-preservation summary.

## Risk controls (capital-preservation defaults)

Mandatory stop on every entry · 1% equity risk per trade (sized off stop
distance) · 3% daily-loss brake · 15% max-drawdown kill-switch (manual re-arm) ·
1 concurrent position · spot-only / no leverage · fees + slippage modelled from
the first backtest. Tunable in `config.yaml` → `risk:`.

## Layout

```
tradebot/
  config.py exchange.py data.py indicators.py     # plumbing
  clock.py types.py enums.py                       # core seams & types
  strategies/  risk.py portfolio.py execution.py   # trading logic (mode-agnostic)
  engine.py                                        # the ONE shared loop
  backtest.py metrics.py walkforward.py            # honest evaluation
  live.py persistence.py journal.py notify.py      # live runner + state + alerts
  main.py
scripts/   tests/   data/ state/ logs/ (gitignored)
```

**Not financial advice.** This is software for education and experimentation.
