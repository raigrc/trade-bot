# Tradebot Audit and Fixed-50-USDT Live Pilot

## Goal and Decisions

- Target real-money BTC/USDT spot trading on the user's existing always-on Linux VPS. The user rejected substituting a paper-only production deployment. Preparing this live pilot does not authorize activation: technical defects and failed strategy gates currently block launch.
- Use a permitted isolated Binance spot account/subaccount, initially funded with no more than 50 USDT and no pre-existing BTC, other assets or orders. Verify account eligibility; do not circumvent exchange/jurisdiction restrictions.
- User selected a fixed 50-USDT allocation ceiling, no automatic compounding above it or top-ups, one position, no leverage, 1% target trade risk, 2% hard sizing cap, 3% daily-loss pause and 15% drawdown shutdown with manual resume. Initial 50-USDT reference amounts are 0.50, 1.00, 1.50 and 7.50 respectively; none is a guaranteed realized-loss ceiling. USDT is not guaranteed to equal USD.
- Keep daily TSMom as the candidate, not a validated recommendation. No forced trades, higher-frequency switch, additional symbols, paid news/LLM dependency or gate weakening. News may be evaluated in shadow mode only.
- This session changes only this plan. Preserve the broad pre-existing dirty worktree, credentials, logs, SQLite/WAL and historical reports. Never delete evidence, adopt/sell unexplained holdings or clear a halt to make a check pass.

## Audit Findings

Local runtime evidence was inspected on September 14, 2026; public exchange/event information was checked September 15. No production account or VPS has been inspected.

| Priority | Verified issue | Evidence |
|---|---|---|
| Critical | Reconciliation checks free BTC, not locked BTC; may erase a protected position. The quoted warning only notifies, then permits startup trading. Foreign stop adoption can use unrelated inventory. | `tradebot/live.py:199-231,335-337`; `tradebot/execution.py:281-285` |
| Critical | Zero reported fill falls back to requested amount; missing prices fall back to a candle. Partial exits flatten execution state. Fee currency is ignored. | `tradebot/execution.py:224-245,331-365` |
| Critical | Ambiguous stop cancellation loses its ID; replacement/exit continues. Failed stop placement only logs; stop-limit can remain unfilled after a gap and monitoring depends on a new daily bar. | `tradebot/execution.py:250-277,304-337` |
| Critical | Order submission is blindly retried; no durable intent/single-writer guarantee; snapshots/journal commit separately. Kill-switch ignores synchronous exit fills and lacks durable unfinished-unwind recovery. | `tradebot/exchange.py:41-46,192-206`; `tradebot/persistence.py:34-40`; `tradebot/engine.py:95-101` |
| High | `initial_equity` is not a live cap: startup uses all free quote cash, then restores potentially stale cash. Multi-symbol initialization duplicates the same budget. Sim and testnet share one state filename. | `tradebot/live.py:60-67,90-96,118-138,184-196` |
| High | Live authorization checks a boolean, not evidence. Arbitrary testnet endpoints can cross environments. HTTPX INFO logging exposes Telegram token URLs; incoming status requests lack chat authorization. | `tradebot/config.py:128,135-160`; `tradebot/main.py:22-53`; `tradebot/exchange.py:71-96`; `tradebot/live.py:483-513` |
| High | Strategy evidence is failed/mismatched; walk-forward intervals overlap at inclusive endpoints; current live alpha inputs are absent from ordinary backtests. | `docs/strategy_gate_results.md:106-149`; `tradebot/walkforward.py:94-103,139-148`; `tradebot/data.py:222-233`; `tradebot/live.py:98-106`; `tradebot/backtest.py:101-107` |

**Warning provenance:** the hourly Windows task runs `scripts/run_paper.ps1` with `--once`. Logs pair the warning with `testnet.binance.vision`, including `logs/paper.log:3115-3118,5465-5470`. Repeated starts explain repeated alerts; seeded/pre-existing testnet BTC is consistent with the evidence, not independently proven transaction history or real BTC exposure. `config.yaml:8-16` actually selects paper/testnet with 1,000 initial equity despite its paper-sim comments.

**Evidence contamination:** the paper database has 82 equity rows on 78 distinct dates, zero closed trades, gaps and a temporary five-minute configuration. Equity jumps from 1,000 to 75,253.10 on August 11 without trading P&L; `reports/weekly_2026-W33.md:5-16` reports the resulting spurious gain. SQLite structural checks passed, not accounting validation. Neither clean qualifying forward evidence nor successful sustained testnet order/protection/recovery is established.

**Strategy gate:** the published adaptive daily BTC walk-forward reports PF 1.58 and expectancy +0.70%, but Calmar 0.44 < 0.5, Sortino 0.67 < 1.0 and 92 < 100 OOS trades. Exact maximum OOS drawdown needs verification. Fixed-28-day diagnostics are explicitly in-sample, and later RSI/volume/alpha changes make the deployed variant different. These numbers are not approval of the proposed release.

## Implementation Sequence

### 1. Establish Identity and Preserve Evidence

Owners: senior-engineer for state/config; devops for authorized backups and cutover.

- Introduce an explicit pilot profile and versioned identity: venue, execution environment/backend, non-secret verified account identity, strategy instance/configuration, symbols/timeframes, allocation and release fingerprint. Pin credential sources and attest the resulting account; a dotenv filename does not override inherited environment variables.
- Give simulation, testnet and live separate state identities/directories. Archive legacy state with lineage using consistent SQLite backup procedures, including committed WAL data. Do not seed live from the mixed paper store or silently reset risk/cursors.
- Acquire an account-scoped lifetime lock before actionable restore/reconciliation. For the first pilot, require one authorized VPS/key and one service, with all launch paths taking the same lock; do not build distributed trading or multi-symbol live support.
- Reconcile free plus locked balances, open orders and confirmed trades against durable bot records and client IDs. Exchange balances prove availability, not ownership. Quarantine unknown holdings/orders/transfers; query failures or mismatches block new entries. Never adopt the first stop-shaped order, erase locked inventory or replace cash from wallet totals.
- Replace "Trusting exchange" with an accurate blocked-state alert containing environment, attributed/unattributed quantities and recovery status. Persist mismatch identity and notify on transitions plus bounded reminders, not every scheduled restart. Testnet seed inventory requires reviewed classification outside the bot allocation, not a fabricated entry.

Affected: `tradebot/config.py`, `tradebot/live.py`, `tradebot/persistence.py`, `tradebot/portfolio.py`.

### 2. Make Execution Durable and Accounting Atomic

Owner: senior-engineer. Implement before any live admission logic relies on a budget or position snapshot.

- Extend existing SQLite storage with durable intents/reservations, exchange-order state and uniquely identified executions/commissions. Store exact monetary/quantity amounts using Decimal-compatible serialization; keep strategy indicators separate from money arithmetic. Preserve existing WAL/busy-timeout behavior.
- Persist each entry, exit and stop-revision intent with a stable client ID before network submission. Bind entry intents to the strategy-instance/signal identity. Enforce uniqueness on intent, order and exchange execution identifiers.
- Represent `INTENT -> SUBMITTING -> OPEN/PARTIAL -> FILLED/CANCELED/EXPIRED/REJECTED`; uncertainty is `UNKNOWN`, not failure/flat. Query by retained IDs and reconcile trades before any resubmission. Never blindly retry an ambiguous create, reuse a completed signal to submit again or release an unknown reservation.
- Apply only confirmed fill deltas, with actual price, quantity and fee assets. Never substitute requested amount for zero/missing fills or historical close for execution price. Preserve residual positions and pending orders through partial results.
- In one transaction, deduplicate executions and update cash, reservations, cost basis, inventory, risk, execution state, journal and relevant processing cursor. Commit intents separately before network calls. Disk/commit failure freezes new risk and invokes recovery; it must not fabricate a successful fill or advance a cursor past an unrecorded action.
- Preserve the dependency-injected engine design: shared event application and `Engine.step()` remain the strategy path. Add an execution-service event seam for live reconciliation between bars without duplicating strategy decisions or bar metrics.

Affected: `tradebot/types.py`, `tradebot/enums.py`, `tradebot/exchange.py`, `tradebot/execution.py`, `tradebot/engine.py`, `tradebot/persistence.py`, `tradebot/journal.py`, `tradebot/portfolio.py`.

### 3. Enforce the Fixed Allocation and Loss Policy

Owner: senior-engineer; security-reviewer verifies the admission boundary.

- Let approved ceiling `B=50`, verified initial funding `0<F<=B`, and fixed sizing anchor `S=F`. Track spendable quote `C`, fee-inclusive owned inventory cost `K`, unspent commitments `R`, and excluded realized-profit reserve `P`; require `A=C+K+R<=B` without double-counting exchange locks.
- Reservations transfer `C -> R`; confirmed purchases convert the appropriate reservation into owned cost/inventory, actual fees and unused cash. On settled disposal release the sold cost basis and apply net P&L `g`: `A'=min(B,A+g)`, `P'=P+max(0,A+g-B)`. Gains can recover allocation below the ceiling; overflow is excluded. Never replenish allocation from old `P`, deposits or unrelated holdings. Preserve remaining dust cost in `K`.
- Track base fees as reduced net inventory, quote fees as actual cash movements and any third-asset commission in its own currency. No extra fee-token funding. Unknown material commissions/valuation block entries until reconciled; never label a fee estimate an actual ledger debit.
- Trade target/hard risk is `1%/2% * min(S, allocated marked equity)`, checked after precision, fees and every sizing adjustment and again at final admission. Reject unknown/obsolete config fields, non-finite/invalid values and target risk above the hard cap. Never increase size to satisfy exchange minimums.
- Define cashflow-neutral pilot NAV `N` including excluded profits, but not external deposits; internal transfers to `P` must not fake a drawdown. Persist UTC day-start NAV `N0`, high-water `H` and daily budget `D=3%*min(S, day-start allocated marked equity)`. Pause entries at `N0-N>=D`; latch drawdown at `(H-N)/H>=15%`. Evaluate with fresh marked prices between bars. Restarts, deposits, reserve transfers and manual resume must not silently reset these baselines.

### 4. Use Bounded Entries and Continuous Protection

Owner: senior-engineer; devops supplies continuous service operation.

- Prefer a marketable, price-bounded `LIMIT` buy with `FOK`, after proving installed CCXT serialization and actual account support. Set a fresh-quote maximum entry price and size against that worst price, stop distance and conservative costs. Reserve `quantity * limit + maximum applicable quote fees`; require both bot allocation and exchange-free funds. Expired/unfilled signals are not chased, widened or switched to unrestricted market buys. FOK is not a maker-fee assumption or a substitute for unknown/partial-result handling.
- Refresh and enforce all applicable symbol/account filters, price/reference bounds, quantities and commissions at admission. Validate the post-fee sellable amount and protective-exit notional at the applicable stop/reference price. Skip stale, invalid or knowingly unprotectable orders. Public September 15 BTCUSDT rules advertise a 5-USDT minimum, 0.00001-BTC lot and 0.01-USDT tick; never hard-code that snapshot as permanent/account-specific truth.
- Prefer exchange-native `STOP_LOSS` market protection, capability-tested on the installed adapter and intended environment. Do not silently substitute stop-limit or daily software checks. Market stops can still gap, partially fill or expire under exchange execution bounds; they do not guarantee the modeled loss.
- Service orders, fills, protection and marked risk independently of daily candles: initial operational cadence two seconds, safety quote/order snapshot expiry ten seconds, rate-limit-aware backoff. Stale safety state blocks entries but retains known protection and continues recovery. Daily signals remain closed-bar-only; slow news/Telegram work must not block protective servicing.
- Keep stop IDs until cancellation/fill is resolved. Account for intervening fills before replacement or exit; never sell already-filled/locked quantities twice. Protection creation/coverage failure raises an immediate alert, blocks entries and initiates an idempotent unwind of attributable sellable inventory only.
- Route synchronous kill-switch fills through the same event reducer and persist `halted/unwind_pending`. Continue management through restart, partial exit, failed cancel and exchange outage. A risk halt or expired entry approval must not abandon an owned open position or cancel its valid protection.
- Unsellable residual BTC remains owned, marked and reserved, not flat. Alert and block new entries until reviewed executable resolution; never erase dust, buy more to clear it, auto-convert it or touch foreign orders. This conservative first-pilot policy can prevent further trading.

### 5. Make Launch Authorization Fail Closed

Owners: senior-engineer and security-reviewer.

- Extend the CLI with a true nontrading preflight that uses read-only exchange methods and read-only state access, reports checks without secrets, and cannot instantiate a trading runner, create/cancel orders, migrate state or clear latches. `--once` is not a dry run; reject it for the live pilot.
- Require explicit live consent plus a root-owned, service-read-only approval manifest binding account/environment, ceiling, effective profile, reviewed release-content/dependency hashes, evidence hashes and validity. No custom signing infrastructure is needed. CLI changes, missing/mismatched evidence or changed identity invalidate entry permission.
- Verify all unchanged gates: after-cost expectancy > 0, PF >= 1.3, Calmar >= 0.5, Sortino >= 1.0, maximum drawdown <= 25%, >=100 OOS trades, >=12 weeks valid forward paper evidence and >=2 weeks qualifying testnet execution validation. Duration alone or a successful connection test is insufficient. Preserve read-only/recovery behavior where ownership is proven when new-entry authorization fails.
- Restrict paper/testnet private endpoints to approved HTTPS testnet origins before authenticated calls; reject production/arbitrary hosts in paper mode, including validation scripts. Pin the paper wrapper to paper mode, rather than relying on its filename.
- Redact credential-bearing URLs/headers/exceptions before logging; suppress unsafe HTTPX request logging. Rotate the Telegram token exposed to the old logging path through an authorized secret-maintenance step, assess old logs/backups and retain sanitized operational evidence. This plan has not rotated anything.
- Authorize Telegram chat/sender before exact command parsing; rate-limit and persist update deduplication. Keep commands read-only, with no trading or remote re-arm capability. Status reports must distinguish data/execution environments, allocation/reserve, freshness, ownership mismatches, pending/UNKNOWN orders, stop coverage, dust and halt state.

Affected: `tradebot/main.py`, `tradebot/config.py`, `tradebot/exchange.py`, `tradebot/live.py`, `tradebot/notify.py`, `scripts/run_paper.ps1`, validation scripts; add a small `tradebot/preflight.py` only if needed for an isolated read-only boundary.

### 6. Rebuild Trustworthy Strategy Evidence

Owner: senior-engineer for evaluation/data correctness; researcher for evidence interpretation.

- Correct train/test and adjacent OOS overlap using explicit disjoint half-open intervals while preserving valid prior-history warmup. Align historical/live HTF windows to the signal close; never let a stale lower-timeframe response observe later HTF data. Require the latest expected completed signal candle after the publication grace period.
- Freeze the exact candidate/profile before evaluation, including RSI/volume settings, stop behavior, fixed-allocation sizing, FOK execution assumptions and realistic commissions/slippage. Separate adaptive walk-forward selection from claims about a fixed deployed configuration. Model nonfills/precision/protection feasibility rather than assuming every bounded order fills.
- Separate mainnet-price strategy evidence, simulation and testnet execution evidence by identity and reports. Testnet OHLCV can materially differ from mainnet and is not interchangeable edge evidence. Reclassify contaminated history without rewriting it; only proven homogeneous observations qualify.
- Keep external funding/OI/sentiment/news out of entry decisions until their inputs are replayable and their exact policy has matched evidence. Existing confidence reductions do not scale sizing, TSMom does not supply an ATR take-profit, and missing news currently removes a veto. Make profile/status semantics explicit; do not enable new confidence sizing or take-profit behavior as an untested launch fix.
- Limit optional improvement work to two versioned experiments: deterministic executable-baseline/freshness recording, and a free news/event veto in shadow mode. Log source/publication/observation timestamps, availability and would-veto outcomes; pre-register the veto and compare costs, avoided losses, missed gains, drawdown and trade count on identical signals. Never backfill current sentiment into past bars or let a fetched headline place an order.
- The current news endpoint returned HTTP 402 on September 14 and fails open. Replace/test its data source only within the optional shadow experiment, with explicit missing/stale status. FOMC is scheduled September 15-16, with September 16 release/press conference at 14:00/14:30 Eastern; this is volatility context, not a forecast or buy/sell signal.

### 7. Validate Before Deployment

Owners: implementation engineer runs tests; an independent engineer/security-reviewer verifies financial boundaries. Testnet order experiments require separate authorization and must never select mainnet implicitly.

| Regression | Required result |
|---|---|
| Seeded/foreign BTC, fully locked BTC, wrong quantities, failed queries | No false-flat/adoption, no foreign cancel/sell, entries blocked with accurate diagnostics. |
| Zero/missing/partial fills; base/quote/third-asset fees; dust | Only confirmed net inventory/cash changes; residuals tracked; no fabricated price or successful protection. |
| Timeout after acceptance; duplicate/out-of-order executions; crash at each boundary | Stable intents, exactly-once ledger effects, retained reservations, no duplicate orders or skipped recovery. |
| Stop creation/cancel ambiguity, fill/cancel race, expired/partial stop, missing candle | Continuous management, no double sell or lost stop ID; unresolved protection blocks entries. |
| Kill switch, failed exit, restart while halted | Fills update cash/P&L/journal; pending unwind survives; protection/management continues; no auto-resume. |
| Huge wallet, old 1,000-USDT state, competing processes, reserved funds, profit/loss cycles | Allocation never exceeds approval; foreign funds/profits cannot silently raise it; second writer blocked. |
| FOK expiry, min notional after fees, precision/reference rules, stale quotes | No chasing/rounding up; invalid plans skipped; actual adapter payloads proven. |
| Mixed identity, invalid/unknown config, every failed gate, CLI changes, paper mainnet URL | Admission denied before any order-capable action; read-only preflight has zero order/state-mutation calls. |
| Unauthorized Telegram, retries/exceptions, alert flood | No unauthorized response and no secret-bearing logs; protection loop remains responsive. |
| Fold edges, warmup, latest candle/HTF boundaries, alpha unavailable, restart replay | Disjoint OOS evidence, no lookahead, deterministic decisions and no duplicate strategy bars. |

Run targeted new live-runner/CCXT/exchange tests and the full `python -m pytest -q`; run `python -m ruff check tradebot tests scripts`. Add to existing persistence/risk/engine/parity tests where suitable; new live-path tests are necessary because simulation tests do not establish live safety. Prove real testnet entry/protection/exit and restart recovery, not merely `order/test` or a connection check. Archive results tied to the reviewed release. No full test suite or deployment was run during this audit; the security audit used offline in-memory reproductions.

### 8. Cut Over to the Existing VPS

Owner: devops, after technical validation and a separate successful release-gate decision.

- Verify VPS OS/runtime, disk durability, time sync, egress IP, dependencies and actual account/API permissions. Use one unprivileged continuously running systemd service, explicit absolute paths and persistent local SQLite storage outside release directories. Keep service disabled until authorized; no hourly `--once` trading.
- Provision a dedicated spot-only key with withdrawals/transfers/borrowing/futures and automatic funding disabled wherever supported, IP-restricted to the verified VPS. Store secrets in a protected root-owned environment file, not the release or command line. Use bounded restart/backoff; restarts never re-arm risk.
- During separately authorized cutover, disable the old Windows schedule, confirm no active writer and exclude other launchers/hosts. Preserve its evidence; bootstrap a fresh verified live identity rather than copying its book. Do not remotely connect, stop jobs, upload credentials, fund accounts or enable orders as part of planning.
- Require passing preflight, resolved ownership, the approved manifest and explicit operator activation of the exact release/account/budget. Observe the first naturally eligible order through confirmed fills, native protection and recovery; never manufacture a signal for a production smoke test.
- Add an independent watchdog for missing service/health, stale data/reconciliation, missing protection, UNKNOWN orders, halts and backup failures. Test alert delivery. Use consistent encrypted off-host SQLite backups and an isolated restore check; a process exit code is not a trading-health check.
- Rollback first disables entries while preserving protective management. Reconcile against current exchange events before restoring a compatible application version. Never overwrite newer fills with an old database, delete WAL, blanket-cancel stops or silently clear latches. Uncertain downgrade compatibility requires operator takeover with entries blocked.

## Release Status and Boundaries

Implementation planning is complete; live activation remains blocked. Required deployment inputs/evidence, not assumed facts: actual VPS access/OS/static IP, permitted account isolation/identity, effective keys/permissions, fee asset/rates and current filters, installed adapter capabilities, corrected passing OOS results, qualifying forward/testnet history and explicit final activation consent. Obtain secrets through protected tooling, not chat.

Shared-wallet support, multi-symbol live, multi-host ownership, automatic compounding/top-ups/dust conversion, paid/LLM trading signals, new TP/confidence strategies and overriding the strategy gate are out of scope. Technical remediation may be implemented independently, but cannot manufacture missing time-series evidence or guarantee a successful trade.

## Public References

- Binance symbol filters, checked September 15: https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT&showPermissionSets=false
- Binance order types, payloads and uncertain-outcome rules: https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- Filters and commission semantics: https://github.com/binance/binance-spot-api-docs/blob/master/filters.md and https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md
- Market execution bounds: https://github.com/binance/binance-spot-api-docs/blob/master/faqs/price_range_execution_rules.md
- Federal Reserve September calendar, checked September 15: https://www.federalreserve.gov/newsevents/2026-september.htm
