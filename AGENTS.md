# AGENTS.md — Project State & Conventions

## Project: trade-bot

Capital-preservation-first automated crypto trading bot for Binance spot (Python, ccxt).
Currently in **paper-sim mode** forward-testing TSMom on daily bars.

## Current State (as of 2026-08-09)

- Paper-sim mode running on daily bars (Binance mainnet prices, simulated fills)
- Testnet validated (5/5 checks pass)
- **125 tests pass**, ruff clean

### Strategy Status
- **TSMom (daily, 28d lookback + 200d SMA gate)** — closest to passing go/no-go gate
  - Walk-forward: BTC PF=1.58, +0.70% expectancy, 92 OOS trades
  - Narrowly misses: Calmar (0.44<0.5), Sortino (0.67<1.0), trade count (92<100)
  - Status: **forward paper-testing** on daily bars, 0 trades so far (staying flat correctly)
- All other strategies (breakout, trend, regime_router, mean_reversion, tsmom on 4h) are **NO-GO**

### Go/No-Go Gate Thresholds
- Expectancy > 0 after costs
- Profit factor >= 1.3
- Calmar >= 0.5
- Sortino >= 1.0
- Max drawdown <= 25%
- >= 100 OOS trades

### Configuration
- `config.yaml`: `mode: paper`, `paper_execution: testnet`, `confirm_live: false`
- `timeframe: "1d"`, `htf_timeframe: "1d"`, `symbols: ["BTC/USDT"]`
- Testnet keys in `.env`, live keys in `.env.live` (both gitignored)
- Heartbeat: 4 hours (Telegram)
- Risk: 1% per trade, 2% hard cap, 3% daily loss limit, 15% kill-switch

### Data
- `data/BTC_USDT_1d.parquet` — 2,347 rows (2020-01-01 to 2026-06-04)
- `data/BTC_USDT_4h.parquet` — 14,082 rows
- `data/ETH_USDT_1d.parquet` — 2,347 rows
- `data/ETH_USDT_4h.parquet` — 14,082 rows
- `data/SOL_USDT_1d.parquet` — 2,187 rows (2020-08-11 to 2026-08-06)
- `data/SOL_USDT_4h.parquet` — 13,123 rows

### Tests
- **125 tests pass**, ruff clean
- Test files: metrics, risk, execution, reporting, persistence, correlation, alpha, portfolio_multi, strategies, no_lookahead, parity, engine

## Architecture

### Core Pattern
Same code runs in backtest/paper/live via dependency injection:
- `Engine.step()` is the ONE shared event loop
- `SimClock` (backtest) vs `LiveClock` (live) — only seam for time
- `SimulatedExecution` (backtest) vs `CcxtExecution` (live) — only seam for fills
- Strategies are PURE: see only closed bars, never touch wall-clock

### Key Files
- `tradebot/engine.py` — `Engine` (single-symbol) + `MultiEngine` (multi-symbol orchestrator)
- `tradebot/risk.py` — `RiskManager` with correlation-aware sizing
- `tradebot/portfolio.py` — `Portfolio` with `positions: dict[str, Position]` (multi-symbol)
- `tradebot/strategies/tsmom.py` — current strategy with alpha filters
- `tradebot/alpha/` — funding rate, OI, sentiment, news sentiment providers
- `tradebot/correlation.py` — rolling pairwise correlation + penalty
- `tradebot/live.py` — `LiveRunner` (single + multi-symbol modes)
- `tradebot/persistence.py` — SQLite state store
- `tradebot/journal.py` — trade journal

### Multi-Symbol Mode
When `config.symbols` has >1 entry:
- Each symbol gets its own `Engine` (strategy, risk, execution, portfolio)
- `MultiEngine` orchestrates them, aggregating equity curve
- Correlation penalty scales position size down for correlated entries
- State keyed by symbol in SQLite

## Conventions

### Code Style
- Python 3.13+, type hints everywhere
- `from __future__ import annotations` in all files
- ruff for linting (line-length=100, target-version=py313)
- No comments unless asked
- `log = logging.getLogger(__name__)` for logging

### Testing
- pytest with `-q` flag
- `tests/conftest.py` has shared fixtures (`market`, `risk_cfg`, `account_flat`, `make_ohlcv_df`)
- Run specific test files: `python -m pytest tests/test_risk.py -q`
- Some tests depend on data files (skip if not present)

### Risk Philosophy
- Capital preservation first, always
- Walk-forward gate must pass before live trading
- Never weaken the gate to make a strategy "pass"
- Forward paper evidence > backtest results
- The bot should NOT trade if no edge is proven

### State Persistence
- SQLite at `state/{symbol}_{mode}.sqlite`
- WAL mode, busy_timeout=5000ms
- State keys: risk, portfolio, execution, strategy, meta
- Multi-symbol uses `multi_engine` key + per-symbol meta

## Completed Work

### Security Hardening
- Live keys separated to `.env.live` (gitignored)
- `.env.local` and `.env.live` added to `.gitignore`
- `RiskState.load_state` uses explicit `_STATE_KEYS` allowlist (no setattr injection)
- `Portfolio.load_state` has type validation on position restore
- `SimulatedExecution.load_state` has error handling for corrupted state
- Dead exposure cap config removed from RiskConfig (6 fields)
- Circuit breaker in live.py (alerts after 5 consecutive exchange failures)
- Tenacity retry on HistoricalDataFetcher and Telegram notifier
- Native stop failure alerts operator
- Telegram `/status` needs chat ID auth

### QA Fixes
- BUG-2: Oversized sell uses `qty_closed` (capped to actual position)
- BUG-3: Zero-qty fill guarded (no-op on zero quantity)
- EDGE-5: Independent `RiskState` per symbol in multi backtest
- Engine kill-switch integration tests
- MultiEngine unit tests

### Monitoring
- Telegram heartbeat (configurable interval, default 4h)
- `/status` Telegram command (chat ID auth required)
- `scripts/paper_status.py` — one-page CLI dashboard
- `scripts/equity_chart.py` — ASCII equity curve + drawdown
- Enhanced weekly reports with per-symbol breakdown

### Multi-Symbol
- Portfolio refactored for `positions: dict[str, Position]`
- `MultiEngine` orchestrator for multiple single-symbol engines
- `correlation.py` — rolling correlation + sizing penalty
- `RiskManager` integrates correlation penalty
- `LiveRunner` supports multi-symbol mode
- ETH/SOL data fetched and cached

### Alpha Sources
- `tradebot/alpha/` package with 4 providers + composite
  - Funding rate, open interest, sentiment, news sentiment
- TSMom uses alpha as confidence filters (skip on crowded longs / extreme greed)
- Non-fatal: API failures default to no filter

### New Files Created
- `tradebot/alpha/news_sentiment.py` — news sentiment alpha provider
- `skills/trade-bot/SKILL.md` — project-specific agent skill
- `.env.live`, `.env.live.example` — live key separation
- `tests/test_engine.py` — engine + kill-switch + MultiEngine tests

## Remaining Work

### What's Next
- Let paper accumulate >=12 weeks evidence before go-live decision
- Walk-forward on ETH/SOL (optional, lower priority)
- Testnet for >=2 weeks to validate live execution path
- Gate must pass before any live deployment

### Phase 5: Go Live (needs gate pass + paper evidence)
- Verify walk-forward gate passes
- Verify >=12 weeks paper evidence
- Set `mode: live`, `confirm_live: true`
- Deploy with tiny capital (50 USDT)
- Pre-flight checks before each run

### Future Enhancements
- Portfolio-level metrics in reporting
- Background thread for Telegram polling (avoid blocking main loop)
