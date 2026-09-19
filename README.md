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
python -m scripts.fetch_data --symbols BTC/USDT ETH/USDT SOL/USDT --timeframes 4h 1d --start 2020-01-01

# 3. START — paper-sim needs NO keys, NO account, NO real money:
python -m tradebot.main --once          # one cycle now (uses config.yaml: paper-sim, tsmom)
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
python -m scripts.paper_status         # one-page status dashboard
python -m scripts.equity_chart         # ASCII equity curve + drawdown chart
python -m scripts.validate_testnet     # validate testnet connectivity + order plumbing
python -m tradebot.admin status        # equity, drawdown, kill-switch, open position, last bar
python -m tradebot.report              # this week's reflection
python -m tradebot.report --logbook    # full trade logbook
python -m tradebot.admin clear-kill-switch   # re-arm after a 15% drawdown halt (manual, deliberate)
Get-Content logs\paper.log -Tail 20    # recent runs
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

## Multi-symbol support

The bot supports trading multiple symbols simultaneously (BTC, ETH, SOL) with correlation-aware
position sizing. When multiple symbols are configured, each gets its own strategy, risk manager,
execution engine, and portfolio — orchestrated by `MultiEngine`.

```yaml
# config.yaml — multi-symbol mode
symbols: ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
risk:
  max_concurrent_positions: 3
```

Correlation between symbols is computed from historical returns. When entering a position correlated
with an existing one, position size is scaled down proportionally (via `tradebot/correlation.py`).

## Alpha sources

TSMom can optionally use supplementary signal sources as confidence filters:

- **Funding rate** (`tradebot/alpha/funding_rate.py`) — high positive funding = crowded longs → skip entry
- **Open interest** (`tradebot/alpha/open_interest.py`) — rapid OI expansion = potential distribution → reduce confidence
- **Sentiment** (`tradebot/alpha/sentiment.py`) — extreme greed (>75) = contrarian caution → skip entry

Alpha sources are non-fatal: if the API is down, the bot trades without the filter.

## Risk controls (capital-preservation defaults)

Mandatory stop on every entry · 1% equity risk per trade (sized off stop
distance) · 3% daily-loss brake · 15% max-drawdown kill-switch (manual re-arm) ·
1 concurrent position (up to 3 in multi-symbol mode) · spot-only / no leverage ·
correlation-aware sizing · fees + slippage modelled from the first backtest.
Tunable in `config.yaml` → `risk:`.

## Layout

```
tradebot/
  config.py exchange.py data.py indicators.py     # plumbing
  clock.py types.py enums.py                       # core seams & types
  strategies/  risk.py portfolio.py execution.py   # trading logic (mode-agnostic)
  engine.py                                        # the ONE shared loop + MultiEngine
  backtest.py metrics.py walkforward.py            # honest evaluation
  live.py persistence.py journal.py notify.py      # live runner + state + alerts
  correlation.py                                   # pairwise correlation + sizing penalty
  alpha/                                           # supplementary signal sources
    __init__.py       # AlphaProvider protocol + AlphaSnapshot
    funding_rate.py   # Binance perpetual funding rate
    open_interest.py  # OI delta over 24h
    sentiment.py      # Fear & Greed Index
    composite.py      # merges all providers
  main.py
scripts/
  paper_status.py        # one-page status dashboard
  equity_chart.py        # ASCII equity curve + drawdown
  validate_testnet.py    # testnet connectivity + order validation
  fetch_data.py          # download historical OHLCV
  optimize_breakout.py   # parameter sweep (historical)
  check_connectivity.py  # basic exchange check
  run_paper.ps1          # Windows scheduled task wrapper
tests/   data/ state/ logs/ reports/ (gitignored)
```

## Key commands reference

| Command | What it does |
|---|---|
| `python -m tradebot.main` | Run bot (mode from config.yaml) |
| `python -m tradebot.main --once` | Single cycle, then exit |
| `python -m tradebot.main --mode backtest` | Run backtest |
| `python -m scripts.paper_status` | Show current paper/live status |
| `python -m scripts.equity_chart` | ASCII equity curve |
| `python -m scripts.validate_testnet` | Validate testnet setup |
| `python -m tradebot.admin status` | Inspect live state |
| `python -m tradebot.admin clear-kill-switch` | Re-arm after halt |
| `python -m tradebot.report` | Weekly reflection |
| `python -m tradebot.report --logbook` | Full trade logbook |
| `python -m tradebot.walkforward --strategy tsmom` | Walk-forward evaluation |

**Not financial advice.** This is software for education and experimentation.
