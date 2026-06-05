# Breakout edge investigation — findings (2026-06-05)

**Question:** Can theory-grounded false-breakout filters get the `breakout` strategy
through the go/no-go gate (so it could trade real money)?

**Method:** 7 variants — baseline + volume-confirmation, ADX trend-strength, HTF-slope
filters and their combinations — each evaluated by **walk-forward** (stitched
out-of-sample, fixed params, full 2020→2026 history including the 2022 bear) on **both
BTC and ETH**. The best variant was then stress-tested by market regime and 2× costs.

## Walk-forward results (out-of-sample)

| variant | BTC ret / Calmar / PF / trades | ETH ret / Calmar / PF / trades | gate |
|---|---|---|---|
| baseline | +21.0% / 0.46 / 1.49 / 111 | −0.1% / −0.00 / 1.00 / 106 | no-go |
| +volume | +18.0% / 0.40 / 1.31 / 115 | +1.5% / 0.03 / 1.04 / 102 | no-go |
| +adx | +3.9% / 0.09 / 1.10 / 80 | +3.3% / 0.07 / 1.13 / 61 | no-go |
| **+htf_slope** | **+26.2% / 0.58 / 1.45 / 113** | −2.8% / −0.04 / 0.93 / 104 | no-go |
| +vol+adx | +6.4% / 0.17 / 1.20 / 70 | +1.7% / 0.04 / 1.08 / 53 | no-go |
| +vol+slope | +21.6% / 0.48 / 1.40 / 104 | +0.5% / 0.01 / 1.01 / 91 | no-go |
| +all | +6.9% / 0.19 / 1.23 / 64 | −0.1% / −0.00 / 0.99 / 45 | no-go |

## Adversarial verification of the best variant (htf_slope, BTC)

**Regime breakdown** — the entire edge comes from one period:

| period | return | PF | trades |
|---|---|---|---|
| 2020–21 bull | +0.1% | 1.00 | 52 |
| 2022 bear | 0.0% | — | 0 (correctly stayed out) |
| **2023–24 recovery** | **+25.1%** | **1.88** | 69 |
| 2025–26 | +0.3% | 1.02 | 31 |

**Cost-stress (2× fees + slippage):** +26.9% / PF 1.38 → **+1.4% / PF 1.02**. The edge
is barely above transaction costs.

## Verdict: NO durable edge — do NOT trade this live.

Three independent red flags converge:
1. **Doesn't generalize** — positive on BTC, flat/negative on ETH. A real edge shows on
   correlated majors; a symbol-specific one is usually regime/luck.
2. **Regime-concentrated** — ~all of BTC's return is from the 2023–24 recovery; flat
   (PF ≈ 1.0) in every other period. That's fitting one window, not an edge.
3. **Cost-fragile** — doubling costs collapses it to break-even.

No filter fixed this. `htf_slope` marginally improved BTC's risk (kept it out of the
2022 bear, nudged Calmar 0.46→0.58) but hurt ETH; `adx` cut good trades (ADX lags
breakouts); `volume` was marginal.

## What this means
- **The capital-preservation machinery works** (max drawdown ~8% throughout, no blowups,
  kill-switch never needed). The *risk* side of the bot is validated.
- This simple strategy set has **no profitable edge on BTC/ETH after costs** — which is
  the expected base-rate outcome for retail technical strategies, not a failure of the build.
- **Do not keep tuning configs until one "passes" full-history** — that is textbook
  overfitting (multiple testing). The disciplined, capital-preserving decision is to NOT
  trade this, keep paper-running to learn, and only revisit with a genuinely different,
  independently-motivated hypothesis (and the same honest walk-forward bar).
