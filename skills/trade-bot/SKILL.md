# Trade Bot — Operator Quick Reference

Capital-preservation-first automated crypto trading bot for Binance spot.

> **Warning:** No trading bot can guarantee profit. Only risk money you can afford to lose entirely.

---

## 1. Quick Start Commands

### Paper Mode (No Keys, No Risk)

```powershell
# Single cycle (smoke test)
python -m tradebot.main --once

# Continuous run (Ctrl-C to stop)
python -m tradebot.main
```

### Testnet Mode (Virtual Money, Real API Calls)

```powershell
# 1. Add testnet keys to .env
# BINANCE_TESTNET_API_KEY=...
# BINANCE_TESTNET_API_SECRET=...

# 2. Set config.yaml
# mode: paper
# paper_execution: testnet

# 3. Validate testnet setup
python -m scripts.validate_testnet

# 4. Run
python -m tradebot.main
```

### Live Mode (Real Money)

```powershell
# 1. Add live keys to .env (spot-only, NO withdrawals enabled)
# BINANCE_API_KEY=...
# BINANCE_API_SECRET=...

# 2. Set config.yaml
# mode: live
# confirm_live: true    <-- REQUIRED safety gate

# 3. Run
python -m tradebot.main
```

### Scheduled Runs (Windows Task Scheduler)

```powershell
# Register hourly task (processes latest candle, exits, resumes from SQLite)
Register-ScheduledTask -TaskName "TradeBot-Paper-BTCUSDT" `
  -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\DELL\Desktop\Rai\trade-bot\scripts\run_paper.ps1"') `
  -Trigger (New-ScheduledTaskTrigger -Once -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew) `
  -Force

# Stop:  Disable-ScheduledTask -TaskName "TradeBot-Paper-BTCUSDT"
# Remove: Unregister-ScheduledTask -TaskName "TradeBot-Paper-BTCUSDT" -Confirm:$false
```

---

## 2. Common Operations

### Status & Monitoring

| Command | Description |
|---------|-------------|
| `python -m scripts.paper_status` | One-page status dashboard (equity, drawdown, kill-switch, trades) |
| `python -m scripts.equity_chart` | ASCII equity curve + drawdown chart |
| `python -m tradebot.admin status` | Inspect live state (equity, drawdown, open position) |
| `Get-Content logs\paper.log -Tail 20` | Recent log entries |

### Reports

| Command | Description |
|---------|-------------|
| `python -m tradebot.report` | Latest week's reflection |
| `python -m tradebot.report --all` | Every week with activity |
| `python -m tradebot.report --logbook` | Full trade logbook → `reports/logbook.md` |
| `python -m tradebot.report --mode live` | Read live journal instead of paper |

### Kill-Switch Management

| Command | Description |
|---------|-------------|
| `python -m tradebot.admin clear-kill-switch` | Re-arm after 15% drawdown halt |

> The kill-switch is **intentionally manual** — a 15% drawdown means assumptions may be broken. A human must decide to resume.

### Data Management

| Command | Description |
|---------|-------------|
| `python -m scripts.fetch_data --symbols BTC/USDT ETH/USDT SOL/USDT --timeframes 4h 1d --start 2020-01-01` | Fetch historical data |
| `python -m scripts.check_connectivity` | Test exchange connection |

---

## 3. Configuration Reference

### `config.yaml` Key Fields

| Field | Default | Description |
|-------|---------|-------------|
| `mode` | `backtest` | `backtest`, `paper`, or `live` |
| `confirm_live` | `false` | Must be `true` to start live mode (safety gate) |
| `paper_execution` | `sim` | `sim` (no keys needed) or `testnet` (real API calls) |
| `symbols` | `["BTC/USDT"]` | Trading pairs. Multi-symbol: `["BTC/USDT", "ETH/USDT", "SOL/USDT"]` |
| `timeframe` | `4h` | Signal bar timeframe (`1d`, `4h`) |
| `htf_timeframe` | `1d` | Higher-timeframe trend filter |
| `initial_equity` | `10000` | Starting equity (virtual for paper) |
| `heartbeat_hours` | `4` | Telegram heartbeat interval (0 = disabled) |
| `strategy.name` | `trend` | `tsmom`, `trend`, `mean_reversion`, `breakout` |
| `strategy.params` | `{}` | Strategy-specific parameters |

### Risk Controls

| Field | Default | Description |
|-------|---------|-------------|
| `risk.risk_fraction_per_trade` | `0.01` | 1% equity risk per trade |
| `risk.risk_hard_cap_per_trade` | `0.02` | Absolute ceiling per trade |
| `risk.require_stop` | `true` | Mandatory stop-loss on every entry |
| `risk.stop_atr_mult` | `1.5` | Stop-loss = entry ± ATR × this |
| `risk.daily_loss_limit_pct` | `0.03` | Halts new entries until UTC midnight |
| `risk.max_drawdown_pct` | `0.15` | 15% drawdown → flatten + halt (kill-switch) |
| `risk.max_concurrent_positions` | `1` | 3 for multi-symbol mode |
| `risk.spot_only` | `true` | No margin/futures |
| `risk.taker_fee_pct` | `0.001` | 0.10% taker fee (Binance VIP0) |
| `risk.slippage_pct` | `0.0005` | 5 bps per side for liquid majors |

### Environment Variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `BINANCE_TESTNET_API_KEY` | For testnet | Testnet API key (generate at testnet.binance.vision) |
| `BINANCE_TESTNET_API_SECRET` | For testnet | Testnet API secret |
| `BINANCE_API_KEY` | For live | Live API key (spot-only, NO withdrawals) |
| `BINANCE_API_SECRET` | For live | Live API secret |
| `TELEGRAM_BOT_TOKEN` | Optional | Telegram bot token (recommended) |
| `TELEGRAM_CHAT_ID` | Optional | Telegram chat ID for alerts |

---

## 4. Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `[BLOCKED] Refusing to start LIVE mode` | `confirm_live: false` | Set `confirm_live: true` in config.yaml |
| `Missing BINANCE_API_KEY/SECRET in .env` | Live keys not configured | Add keys to `.env` (spot-only, NO withdrawals) |
| `Missing BINANCE_TESTNET_API_KEY/SECRET in .env` | Testnet keys not configured | Generate at testnet.binance.vision, add to `.env` |
| `No state file found` | Bot hasn't run yet | Run `python -m tradebot.main --once` first |
| `No equity data yet` | No trades recorded | Bot is flat (staying out of market) — normal behavior |
| `Kill-switch: ENGAGED` | 15% drawdown breached | Review strategy, then: `python -m tradebot.admin clear-kill-switch` |
| `Exchange auth failure` | API key/secret wrong or expired | Regenerate keys, update `.env` |
| `Clock drift > 2000 ms` | System clock out of sync | Sync system clock (Windows Time Service) |
| `Testnet balance is zero` | Testnet resets ~monthly | Reset at testnet.binance.vision |
| `Telegram not sending` | Bot token/chat ID wrong | Verify with @BotFather and @userinfobot |
| Log file not updating | Scheduled task not running | Check Task Scheduler history, verify `run_paper.ps1` path |

### Log Locations

| Log | Path |
|-----|------|
| Bot output | `logs/paper.log` |
| Previous rotation | `logs/paper.prev.log` |
| Last success timestamp | `logs/last_success.txt` |
| State database | `state/<symbol>_<mode>.sqlite` |
| Reports | `reports/` |

---

## 5. Safety Checklist — Before Going Live

### Gate Requirements (All Must Pass)

- [ ] Strategy passes walk-forward: `python -m tradebot.walkforward --strategy tsmom`
- [ ] Expectancy > 0 after costs
- [ ] Profit factor ≥ 1.3
- [ ] Calmar ratio ≥ 0.5
- [ ] Sortino ratio ≥ 1.0
- [ ] Max drawdown ≤ 25%
- [ ] ≥ 100 out-of-sample trades

### Pre-Live Checklist

- [ ] **Backtest gate passed** — see above
- [ ] **Paper forward-tested ≥ 12 weeks** — results consistent with backtest
- [ ] **Testnet validated** — `python -m scripts.validate_testnet` (5/5 PASS)
- [ ] **Testnet run ≥ 2 weeks** — real order placement works
- [ ] **Live keys configured** — spot-only, **withdrawals DISABLED**
- [ ] **`confirm_live: true`** set in config.yaml
- [ ] **Telegram alerts enabled** — receive kill-switch/error notifications
- [ ] **Tiny capital** — start with amount you can lose entirely (e.g., 50 USDT)
- [ ] **Kill-switch armed** — 15% max drawdown halt is active
- [ ] **Daily loss limit** — 3% halt is active
- [ ] **Stop-loss mandatory** — `require_stop: true`
- [ ] **Spot-only** — `spot_only: true`, `max_leverage: 1.0`
- [ ] **Monitoring schedule** — check status daily for first 2 weeks

### Red Flags — Do NOT Go Live If

- Strategy fails any gate metric
- Paper results diverge from backtest
- Testnet order placement fails
- You haven't run testnet for ≥ 2 weeks
- You can't afford to lose the entire capital
- Live keys have withdrawal enabled
- Telegram alerts not working

---

## Data Files

| File | Description |
|------|-------------|
| `data/BTC_USDT_1d.parquet` | BTC daily OHLCV (2020-01-01 to 2026-06-04) |
| `data/BTC_USDT_4h.parquet` | BTC 4h OHLCV |
| `data/ETH_USDT_1d.parquet` | ETH daily OHLCV |
| `data/ETH_USDT_4h.parquet` | ETH 4h OHLCV |
| `data/SOL_USDT_1d.parquet` | SOL daily OHLCV (2020-08-11 to 2026-08-06) |
| `data/SOL_USDT_4h.parquet` | SOL 4h OHLCV |

---

**Not financial advice.** This is software for education and experimentation.
