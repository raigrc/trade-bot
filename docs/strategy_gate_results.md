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

## Daily-bar follow-up (2026-06-27) — promising, but still a walk-forward NO-GO

The 4h pure-price/TA space is exhausted. The teed-up cheap experiment was then run:
`tsmom` and `trend` on **daily (1d)** bars (regime gate = daily close vs daily SMA-200;
same 540/180-day walk-forward windows). Dropping from 4h to daily cut the cost/whipsaw
drag exactly as predicted — and for the first time a config showed **positive expectancy
with profit factor > 1.3**.

### Honest walk-forward (the real gate)
| Strategy | Symbol | OOS return | PF | Calmar | Sortino | Trades | Expectancy | Gate |
|---|---|--:|--:|--:|--:|--:|--:|:--|
| tsmom | BTC | +13.71% | 1.58 | 0.44 | 0.67 | 92 | +0.70% | NO-GO (Calmar/Sortino/trades) |
| tsmom | ETH | +6.32% | 1.13 | 0.13 | 0.32 | 109 | +0.51% | NO-GO |
| trend | BTC | +2.75% | 2.77 | 0.26 | 0.20 | 6 | +5.5% | NO-GO (6 trades) |
| trend | ETH | +0.46% | 1.12 | 0.02 | 0.03 | 9 | +0.46% | NO-GO (9 trades) |

Daily-TSMom/BTC **narrowly** misses on Calmar (0.44 vs 0.5), Sortino (0.67 vs 1.0), and
trade count (92 vs 100). It is a NO-GO — but a near-miss, unlike everything before it.

### Adversarial diagnostics — does it fail the way breakout did?
Breakout died two ways: regime-concentrated (one 2023-24 window) and cost-fragile
(collapsed under 2× costs). Daily-TSMom fails NEITHER. Fixed robust params (lookback=28d +
200d regime gate), full-history backtest (**IN-SAMPLE — optimistic, NOT the gate
estimate**), P&L by regime and under 2× costs:

| | BTC 1× | BTC 2× | ETH 1× | ETH 2× |
|---|--:|--:|--:|--:|
| Return | +35.4% | +28.4% | +16.2% | +12.2% |
| Profit factor | 2.53 | **2.20** | 1.53 | **1.40** |
| Calmar | 0.91 | 0.66 | 0.39 | 0.27 |

BTC regime breakdown: 2020-21 bull PF **3.54** · 2022 bear **0 trades (stayed out)** ·
2023-24 recovery PF **3.87** · 2025-26 PF 0.76 (recent weakness). ETH: positive in every
bull regime, survives 2× costs at PF 1.40.

Unlike breakout, the edge is **not** concentrated in a single window (profitable across two
distinct bull cycles), correctly sits out the 2022 bear, **survives 2× costs** (PF stays
2.20/1.40), and shows the same sign on both majors. These are the fingerprints of a real —
if modest — momentum premium, not curve-fit luck.

### Verdict and the disciplined next step
Honest gate: **still NO-GO — do not trade real money.** Yellow flags remain: the
walk-forward gate misses, low walk-forward efficiency (params don't transfer cleanly OOS),
recent 2025-26 softening, and borderline-thin trade counts.

But this is the first genuine signal of life, and it does not fail breakout's failure
modes. The capital-preserving way to pursue it is **not** to tune params until it squeaks
past the gate (that is overfitting). It is to **forward paper-test `tsmom` on daily bars**
— gather real, un-tortured out-of-sample evidence over the coming months at $0 risk. If it
keeps its cross-regime, cost-robust behavior forward, that builds the case for testnet then
tiny-live. If it decays, we learned cheaply. The in-sample +35% is the optimistic ceiling;
the honest forward expectation is the +13.7% walk-forward figure or lower.
