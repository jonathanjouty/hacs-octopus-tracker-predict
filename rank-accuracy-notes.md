# Rank-accuracy notes

The integration's stated goal is **relative rank** — picking the cheapest upcoming
days for EV charging — not minimising absolute price error. This file captures
what we've measured and the candidate next steps for improving rank accuracy.

## Metrics we now track

Recorded per-region in `calibration_history.json`:

- `rank_spearman` — Spearman ρ of model predictions vs actual Tracker daily means.
- `rank_top3_of_7` — mean overlap (0–1) between the model's "top-3 cheapest days"
  and the true top-3 across every contiguous 7-day window in the calibration period.
- `baseline_top3_of_7` — same metric but using **raw Agile spot daily means** as
  the predictor (no model). Reveals whether the linear regression adds rank value
  over ranking by Agile alone.

All three are computed in-sample on the same 90-day window the model is fitted on.
Backfilled across all historical entries by `scripts/backfill_rank_metrics.py`;
written for fresh entries by `scripts/recalibrate.py`.

Helpers (`spearman_rho`, `top_n_window_overlap`, `_average_ranks`) live in
`scripts/recalibrate.py`.

## Findings from the backfill

| Date    | Spearman ρ | Model top3/7 | Agile-only baseline top3/7 |
| ------- | ---------- | ------------ | -------------------------- |
| 04-01   | 0.92       | 0.91         | 0.91                       |
| 04-06   | 0.89       | 0.89         | 0.89                       |
| 04-13   | 0.86       | 0.85         | 0.85                       |
| 04-23   | 0.80       | 0.78         | 0.78                       |
| 04-24   | 0.62       | **0.51**     | **0.78**                   |

Two clear signals:

1. **The rolling-mean change made rank accuracy worse.** Until 04-24 the rolling
   window was 1 (spot daily mean), and `slope·x + intercept` is monotonic, so
   model and baseline produced identical ranks. After the change to window=7 on
   04-24, model top-3/7 dropped to 0.51 while the Agile-only baseline held at
   0.78. Smoothing destroys exactly the day-to-day variation that rank-picking
   depends on.
2. **There is a separate underlying degradation** independent of the model — the
   Agile-only baseline drifts from 0.91 → 0.78 between 04-01 and 04-23. Agile
   itself is becoming a less reliable rank-proxy for Tracker.

## Candidate next steps

In rough priority order. Each should be evaluated against `rank_top3_of_7` and
the Agile-only baseline, not RMSE/MAE.

1. **Revert the rolling-mean change (or pin window=1).** The grid-search picks
   the window that maximises R², but R² rewards smoothing and rank does not.
   Either drop the search and force window=1, or change the objective to
   `rank_top3_of_7`.
2. **Bypass the regression for the cheapest-day sensor.** Rank by Agile daily
   mean directly. The linear transform adds zero rank value at window=1 and
   net-negative rank value at window>1. Model output would still drive the
   absolute-price sensors; the cheapest-day sensor would use Agile rank.
3. **Investigate the 0.91 → 0.78 baseline drift.** Possible causes: Agile
   forecast quality regression, a Tracker tariff regime change inside the
   window, or a bug in how we align timezones / day boundaries. Start by
   plotting daily Agile vs daily Tracker for the full 90 days of the worst
   region.
4. **Per-day-of-week or per-month bias term.** If Tracker has a weekly or
   seasonal pattern that Agile doesn't, a small categorical offset on top of
   Agile rank could help. Cheap to test.
5. **Use Agile high/low bands, not just `agile_pred`.** The Predict API
   returns confidence intervals; days where the band is wide may rank
   unreliably. Worth checking whether weighting by band width improves ranking.
6. **Shorter calibration window.** 90 days may include stale regime data. Try
   30 / 45 / 60 and grid-search by `rank_top3_of_7`.
