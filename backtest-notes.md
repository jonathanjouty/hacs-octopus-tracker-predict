# Backtest notes

## TL;DR

* **The model** is a one-feature OLS regression, `tracker = slope × agile_daily_mean + intercept`. It is fitted on the last 60 days of *actual* Agile vs Tracker rates, then applied to *Agile Predict's* forecast daily means.
* **The existing metrics** (`calibration_history.json`, R² ≈ 0.95, top-3/7 ≈ 0.89) are in-sample and use actual Agile prices, so they measure the mapping and ignore forecast error. Live, users get **top-3/7 ≈ 0.75 and MAE ≈ 2.5 p/kWh** (all regions, 44 issue dates, 28 Jul – 18 Sep 2026).
* **Almost all of the error comes from upstream.** With perfect Agile forecasts, the same regression would give MAE 0.47 p and regret@1 0.02 p. Anything we do to the transform is fighting over the last ~0.5 p of MAE and cannot change the ranking.
* **Room for improvement:** regret@1 is 1.08 p/kWh (about 4% of the mean Tracker rate of 28 p). The perfect-forecast ceiling is about 0. None of the cheap transform tweaks tried so far beats production.

## Running it

```bash
pip install aiohttp
python scripts/eval_harness.py --regions all --out backtest-report.md
python scripts/eval_harness.py --offline --regions A    # re-score from cache, ~2 s per region
```

Options: `--horizon` (days ahead scored, default 7), `--issue-hour` (use the latest forecast issued before this UK hour, default 10), `--history-days`.

**Data retention matters.** Agile Predict's API only returns its ~206 most recent forecasts (about 2 months). The harness merges everything it sees into `.eval_cache/` (gitignored), so running it regularly (e.g. weekly) grows the backtest window. Without that, the window stays at about 45 issue dates and CIs stay wide. A scheduled job that commits the cache (or uploads it as an artifact) would fix this properly.

## How it works

For each issue date *I*:

1. Take the latest Agile Predict forecast created before `--issue-hour`.
2. Build each candidate using only data dated before *I* (walk-forward, no leakage; covered by a test).
3. Predict Tracker for *I*+1 … *I*+7 and score against published Tracker rates.

Metrics per issue, then averaged:

| metric | meaning |
| --- | --- |
| MAE | p/kWh absolute error |
| regret@1 / @3 | p/kWh lost by charging on the predicted cheapest 1 / 3 days rather than the actual cheapest. **Primary decision metric.** |
| top-3 overlap | share of actual 3 cheapest days that were predicted (random = 3/7 = 0.43) |
| Spearman ρ | rank correlation within the 7-day window |
| magnitude slope | predicted % diff between days regressed on actual % diff (1 = right size). Goal 2 in `rank-accuracy-notes.md`. |

Differences against `production` come with 95% moving-block bootstrap CIs (week-long blocks, because consecutive issues share target days). The all-regions summary averages regions per date before bootstrapping, because regions are almost perfectly correlated and 14 regions do not give 14 times the evidence.

## Results (2026-09-25, all regions)

| candidate | MAE | regret@1 | regret@3 | top-3 | ρ |
| --- | ---: | ---: | ---: | ---: | ---: |
| **production** | 2.53 | 1.08 | 0.80 | 0.750 | 0.65 |
| cal_30d / cal_120d | 2.52 | 1.08 | 0.80 | 0.750 | 0.65 |
| ex_peak | 2.54 | 1.29 | 0.82 | 0.735 | 0.65 |
| fit_on_forecasts¹ | 3.68 | 1.91 | 1.23 | 0.696 | 0.62 |
| stretch² | 2.68 | 0.99 | 0.78 | 0.752 | 0.69 |
| last_known (naive) | 3.74 | 5.40 | 2.86 | 0.470 | 0.00 |
| oracle_agile* (ceiling) | 0.47 | 0.02 | 0.06 | 0.902 | 0.93 |

¹ Only 23 issues: it needs about 3 weeks of forecast history to warm up. Paired with production on those same issues, it is no better.
² Scored on 39 issues. Paired with production, its rank metrics are identical by construction and its MAE is +0.06 p (worse).

Full per-region tables, per-horizon MAE and bias are in `backtest-report.md`.

### Observations

* **Calibration window (30/60/120 d) doesn't matter.** MAE differences are within ±0.03 p. Rank metrics are identical, because any increasing linear transform of a single feature ranks days the same way.
* **Ex-peak Agile mean (dropping the 16:00–19:00 uplift) is slightly worse.** regret@1 is +0.2 p, and the CI excludes 0 in the bad direction.
* **Fitting on forecasts** (per-horizon regression of Tracker on Agile Predict output) is worse. With about 2 months of forecast archive it is data-starved. Revisit once the cache is bigger.
* **Flat predictions are not cheaply fixable.** Agile Predict's within-week spread is about half the real spread (std 2.8 vs 5.4 p). As a result, predicted day-to-day differences are about ¼ of the real ones (magnitude slope 0.24 vs 0.90 for the oracle). The MSE-optimal stretch factor learnt from past issues is about 1.0, so the flatness is the correct response to forecast noise. Widening it to "look right" would raise MAE.
* **Error grows with horizon.** MAE goes from 1.7 p at h=1 to 2.9 p at h=7. Bias stays within ±0.3 p at every horizon, so the errors are noise, not systematic offset.
* **September was much harder than August.** Per-issue MAE rose from about 1–2 p to 3–5.7 p. This is again upstream: the oracle stays at about 0.5 p throughout.

## Where improvement could come from

All the gap between production and the oracle is **Agile Predict forecast error**. Transform-level ideas therefore have a ceiling of about ±0.05 p MAE and zero rank gain. Ideas that could actually move regret:

1. **Better or extra forecast inputs.** Blend Agile Predict with another day-ahead source (e.g. wholesale forward prices, wind/demand forecasts), or use Agile Predict's `agile_low`/`agile_high` spread as an uncertainty signal. Add these as candidates in `make_candidates()`.
2. **Decision-aware output.** Where forecast differences are smaller than the typical error at that horizon (≈2–3 p), say so. For example, mark near-tied days as "similar" rather than ranking them. This won't change regret, but it stops the calendar implying false precision.
3. **Grow the archive** (see above) so that learnt-from-forecast approaches (horizon-dependent shrinkage, bias correction) have enough data to be judged fairly.
