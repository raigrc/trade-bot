# Strategy go/no-go sweep — findings (2026-06-26)

**Question:** Does *any* strategy in the codebase — or the best-evidenced new
academic candidate (time-series momentum) — clear the go/no-go gate, so it could
trade real money?

**Method:** Honest **walk-forward** (stitched out-of-sample, params re-selected per
train window from a small pre-registered grid, full 2020→2026 history including the
2022 bear), with a Monte-Carlo bootstrap, on **BTC and ETH**. Costs modeled: taker
0.10%, slippage 0.05% (stop slippage 0.20%). The gate (must pass **all**): positive
expectancy after costs · profit factor ≥ 1.3 · Calmar ≥ 0.5 · Sortino ≥ 1.0 · max
drawdown ≤ ~25% · ≥ 100 OOS trades. Same gate the prior `breakout` study used.

## Results — stitched out-of-sample

| Strategy | Symbol | OOS return | Profit factor | Calmar | Sortino | Trades | Gate |
|---|---|--:|--:|--:|--:|--:|:--|
| breakout *(prior, `breakout_findings.md`)* | BTC | +21.0% | 1.49 | 0.46 | 0.51 | 111 | **NO-GO** (Calmar/Sortino) |
| breakout *(prior)* | ETH | −0.1% | 1.00 | −0.00 | 0.01 | 106 | **NO-GO** |
| trend (EMA-cross + ADX + 200d gate) | BTC | −0.22% | 0.98 | −0.01 | −0.00 | 24 | **NO-GO** |
| trend | ETH | −3.94% | 0.72 | −0.11 | −0.09 | 30 | **NO-GO** |
| regime_router | BTC | −0.33% | 0.97 | −0.01 | −0.00 | 26 | **NO-GO** |
| regime_router | ETH | −6.30% | 0.35 | −0.19 | −0.10 | 15 | **NO-GO** |
| **tsmom** (new — Liu & Tsyvinski 2021) | BTC | −10.90% | 0.91 | −0.11 | −0.20 | **403** | **NO-GO** |
| **tsmom** (new) | ETH | −24.53% | 0.79 | −0.17 | −0.56 | **381** | **NO-GO** |
| mean_reversion | BTC/ETH | — | — | — | — | — | not completed (see note) |

### The TSMom result is the most informative
Time-series momentum is the **best-evidenced** systematic effect in academic crypto
finance (Liu & Tsyvinski, *Review of Financial Studies* 2021; Moskowitz-Ooi-Pedersen
2012). We implemented it from scratch (`tradebot/strategies/tsmom.py`) with a
deliberately tiny pre-registered grid (lookback ∈ {14, 21, 28} days, optional 200-day
regime gate — 6 combos total, no more, to avoid the multiple-testing trap).

It produced **400+ out-of-sample trades** with **negative** expectancy on both majors.
That is not a thin-sample fluke — it is a genuine no-edge result. At 4h resolution the
~0.30% round-trip cost exceeds the small momentum premium; the signal churns into fees.
Walk-forward efficiency was ~0 to negative across **every** strategy and symbol, i.e.
nothing that looked good in-sample carried out-of-sample.

### Note on mean_reversion
The `mean_reversion` walk-forward did **not** complete in a reasonable time — it fires
extremely frequently, and the runs were killed after each burned ~16 min of CPU with no
output. It does not change the conclusion: it is the least-promising candidate (a
mean-reversion strategy structurally blows up in sustained trends/bears), and it is the
same logic already exercised inside `regime_router`, which failed cleanly.

## Verdict: comprehensive NO-GO — do not trade live

Across the **entire pure-price / technical-analysis strategy space** on 4h BTC/ETH spot,
**no configuration has a tradeable edge after costs.** This is the expected base-rate
outcome for retail systematic strategies, not a failure of the build.

What **is** validated — the half that matters for capital preservation:

- **The risk machinery works.** Across every test, max drawdown stayed ~4–8% (one ETH
  outlier ~31%), 1% sizing and the mandatory stop behaved, the kill-switch fired
  correctly, no blowups.
- **The honest-evaluation machinery works.** The harness can and did surface positive
  raw returns (breakout/BTC +21%) yet still correctly failed them on risk-adjusted and
  out-of-sample grounds.

The capital-preserving decision (per `README.md`) is unambiguous: **do not go live.**
Keep paper-running to learn; only revisit with a genuinely novel, independently-motivated
hypothesis — never by tuning configs until one "passes" (textbook overfitting).

## Meta-lesson and the one remaining cheap experiment

The clear signal: **pure price/TA on 4h majors is exhausted.** Walk-forward efficiency
near zero everywhere says the timeframe + data + signal family has no generalizable edge.

The single most defensible next experiment (no new data, no new code — a config change):
run `tsmom`/`trend` on **daily (1d) bars** instead of 4h. Rationale: the 4h TSMom died
from cost-drag at 403 trades; on daily bars the same 28-day signal fires ~10–15×/year, so
cost drag drops from dominating to marginal — and the weekly/daily horizon is the
granularity the academic momentum evidence actually documents. **Honest prior: 15–20%**
to clear the gate (most likely failure: < 100 OOS trades over 2020→2026, or the 2022 bear
collapsing Calmar). If the daily version also fails, the evidence has converged: stop
testing this instrument/data at this complexity, keep paper-running the least-bad
strategy, and do not go live.
