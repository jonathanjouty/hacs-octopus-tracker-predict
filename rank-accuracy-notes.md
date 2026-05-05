# Rank-accuracy notes (rework)

This is a deliberate rethink. The previous version of this file framed the
problem narrowly around rank and proposed bypassing the regression for the
cheapest-day sensor. That doesn't fit the actual UX — the calendar shows
predicted Tracker prices, so the prediction has to be both **correctly
ordered** *and* **correctly priced**. The notes below redo the problem
framing.

## Goals (priority order)

1. **Day-order accuracy.** Across the next ~7 days, identify which days are
   cheapest. Used to pick when to charge the EV. Metric: `rank_top3_of_7`
   (mean overlap between model's and actual's top-3 cheapest days across
   contiguous 7-day windows).
2. **Relative-magnitude accuracy.** Given two days, predict their *ratio* /
   *percentage* difference correctly so the calendar communicates "tomorrow
   is 25% cheaper" rather than "tomorrow is 5% cheaper" without being
   misleading. Currently **unmeasured**.
3. **Absolute price accuracy.** Predicted p/kWh close to actual p/kWh. Needed
   so the calendar entries display believable numbers. Metric: MAE / RMSE.
   Currently the **only** loss the calibration pipeline optimises.

The integration today optimises (3) at the expense of (1) and (2), and (1)
and (2) are what the user actually relies on.

## Metrics we currently track

Recorded per region in `calibration_history.json` for each calibration run:

- `rank_spearman` — Spearman ρ of model predictions vs actual Tracker daily
  means.
- `rank_top3_of_7` — mean overlap (0–1) between model's top-3 cheapest days
  and actual top-3 across every contiguous 7-day window.
- `baseline_top3_of_7` — same overlap metric but using **raw Agile spot
  daily means** as the predictor (no model). Tells us whether the linear
  regression layer adds rank value over ranking by Agile alone.

All three are computed in-sample on the same 90-day window the model is
fitted on. Helpers (`spearman_rho`, `top_n_window_overlap`,
`_average_ranks`) live in `scripts/recalibrate.py`. Backfill of older
entries via `scripts/backfill_rank_metrics.py`; new entries are written
directly by `scripts/recalibrate.py`.

We **do not yet** track a magnitude metric. Adding one is a prerequisite
for serious work on goal (2).

## What the data says today

| Date  | rolling_window | R²   | Spearman ρ | Model top3/7 | Baseline top3/7 |
| ----- | -------------- | ---- | ---------- | ------------ | --------------- |
| 04-01 | 1              | 0.85 | 0.92       | 0.91         | 0.91            |
| 04-06 | 1              | 0.67 | 0.89       | 0.89         | 0.89            |
| 04-13 | 1              | 0.58 | 0.86       | 0.85         | 0.85            |
| 04-23 | 1              | 0.56 | 0.80       | 0.78         | 0.78            |
| 04-24 | **7**          | 0.35 | 0.62       | **0.51**     | 0.78            |
| 04-27 | 7              | 0.34 | 0.61       | 0.50         | 0.76            |
| 05-04 | 7              | 0.30 | 0.55       | 0.47         | 0.71            |

(Region A shown; per-region variation is small. See JSON for the rest.)

Three things are happening at once:

1. **The rolling-mean change destroys rank.** From 04-24 onwards the
   recalibration grid-search picks `rolling_window=7`. Model `top3_of_7`
   collapses to ~0.50 while the Agile-spot baseline stays around 0.71–0.78.
   With `rolling_window=1`, the linear transform is monotonic so model and
   baseline ranks were identical (visible in the 04-01 → 04-23 rows). With
   `rolling_window=7` the input barely moves day-to-day, so the prediction
   barely moves day-to-day, so within any 7-day window everything looks
   equally cheap. Smoothing the input flattens the output and that flatness
   hurts both goals (1) and (2).
2. **An underlying drift independent of the model.** The Agile-only
   baseline drops from 0.91 → 0.71 across the period, with no model change
   between 04-01 and 04-23. Agile's day-ordering is becoming a less
   reliable proxy for Tracker's. Possible causes:
   * Real regime change in Tracker pricing (a tariff parameter change
     inside the rolling window that defines Tracker).
   * Agile forecast quality regression.
   * Timezone / day-boundary alignment bug in our code that worsens as the
     window shifts.
3. **R² also collapses (0.85 → 0.30).** This is *not* just about rank —
   the absolute Agile↔Tracker linear relationship has weakened too. So
   even if (1) were resolved, the absolute predictions on which the
   calendar relies have become noisier.

The grid-search optimises R², which prefers a smoothed input because
smoothing reduces variance on the line of best fit. That objective is
explicitly misaligned with goals (1) and (2).

## Constraints any solution has to satisfy

- The calendar entries display the **predicted Tracker price**, so we can't
  separate "rank picker" from "price displayer" without showing
  contradictory numbers next to each other on the same UI surface.
- The integration must stay dependency-free for HACS — pure Python, no
  numpy / scipy.
- Calibration must remain runnable both from CI (the quarterly
  `recalibrate.yml` workflow) and from the live coordinator on first start.

## Candidate directions

No priority ordering yet. Each should be evaluated against the *combined*
{rank, magnitude, MAE} criteria, not just one of them in isolation.

- **Drop the rolling-mean smoothing, accept the extreme-day bias.** The
  rolling-mean change (commit `0f47011`, 2026-04-12) was introduced to
  reduce systematic over-prediction on high-Agile days and
  under-prediction on low-Agile days. If that bias is small in p/kWh
  terms, reverting buys back rank quality cheaply. We need a pre/post MAE
  comparison to know the cost. Quickest experiment.
- **Decompose level vs day-effect.** Use the rolling mean as the *level*
  feature and `(spot − rolling_mean)` as a *day deviation* feature.
  Tracker is itself a smoothed wholesale, so this split has a real
  physical basis. Two-feature linear fit is still pure-Python and avoids
  the smoothing-flattens-output failure mode.
- **Non-linear (piecewise / quantile) transform on the spot input.** Keep
  the spot daily mean (preserves rank), correct the extreme-day bias with
  a non-linear shape rather than smoothing. Slightly harder to fit
  without numpy but doable.
- **Change the recalibration objective.** The grid-search currently picks
  `rolling_window` on R². Switch the search objective to
  `rank_top3_of_7` — or a weighted blend of rank and MAE — so it can no
  longer choose a window that wins on R² while losing on rank. Cheap to
  try; works whether or not the underlying parametrisation also changes.
- **Investigate the underlying drift first.** Before changing the model,
  plot daily Agile spot vs daily Tracker for the worst-baseline region
  across the 90-day window. If there's a timezone-alignment or
  day-boundary bug, it'll be visible. Zero-cost diagnostic that may
  invalidate parts of the parametrisation work above.
- **Per-DOW or per-month bias term.** If Tracker has a weekly pattern
  Agile doesn't, a small categorical offset on top of Agile rank could
  help. Cheap to test; mostly relevant if rank is still bad after the
  smoothing question is resolved.
- **Use Agile high/low band width as a confidence weight.** The Predict
  API returns confidence intervals. Days with wide bands may rank
  unreliably; we could de-weight or flag them. Defer until rank is
  otherwise good.
- **Shorter calibration window.** 90 days may include stale regime data.
  Try 30 / 45 / 60 once a rank-aware objective is in.

## Metrics worth adding

- **Magnitude metric.** For every pair of days within a contiguous 7-day
  window, what fraction has the predicted % difference within ±X% of the
  actual? Or: regress predicted day-pair % differences against actual and
  report the slope (1.0 = perfect, < 1.0 = predictions are too flat).
  Without this we can't tell whether smoothing is also collapsing
  meaningful day-to-day spreads.
- **Out-of-sample rank.** Fit on the first 60 days of the 90, evaluate
  rank on the remaining 30. Removes the in-sample optimism baked into
  every current row of the table.

## Recommended next moves

1. **Investigate the underlying drift first.** It's free, may surface a
   data bug that changes everything below. A small ad-hoc plotting script
   over `agile_daily` and `tracker_daily` for one region's 90-day window.
2. **Change the recalibration objective from R² to a rank-aware blend.**
   This is a one-function change in `scripts/recalibrate.py`. With it in
   place, the grid-search is no longer actively choosing the
   rank-destroying window; further parametrisation experiments become
   safer.
3. **Add a magnitude metric** to recalibrate output and backfill it. We
   don't currently know whether goal (2) is met or not; we can't improve
   what we can't see.
4. **Then experiment with parametrisation.** Compare on the new combined
   {rank, magnitude, MAE} basis: window=1, level+deviation decomposition,
   piecewise/quantile transform. Pick the best.
5. **Defer per-DOW, band-weighting, shorter calibration window.** They're
   second-order; only attempt if (1)–(4) leave a measurable gap.

The "bypass the regression for the cheapest-day sensor" idea from the
previous version of this file is *not* on this list. It would either
display Agile prices in the calendar (wrong magnitude) or display the
model's smoothed Tracker estimate next to a rank that doesn't match it
(internally inconsistent UI). Neither is acceptable for the
calendar-driven charging workflow.
