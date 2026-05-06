# Rank-accuracy notes

## TL;DR

Investigating the rank-accuracy "drift" surfaced a real bug:
`compute_daily_means` (and the equivalent grouping in `_transform_forecast`)
sliced `valid_from[:10]` to bucket half-hourly rates by date. That's the
**UTC** date. During BST the first hour of each UK day (UTC 23:00–00:00)
belongs to the previous UTC date, so those slots were misallocated to the
wrong UK day. The bug is invisible in GMT and kicks in every March, which
matches when the rank metrics started cratering.

A/B test on the same raw 91-day window (region A, ending 2026-05-05):

| bucketing | pearson | spearman | top3/7 | R² |
| --------- | ------- | -------- | ------ | ---- |
| UTC (old) | 0.71    | 0.70     | 0.71   | 0.51 |
| UK (now)  | **0.97**| **0.94** | **0.89**| **0.95** |

Same data, single bucketing change. Most of what we previously called
"underlying drift" was an artefact. Fix landed in
`calibration.py`, `coordinator.py`, and `scripts/recalibrate.py` — convert
`valid_from` to `Europe/London` before extracting the date.

## Goals (priority order)

1. **Day-order accuracy.** Across the next ~7 days, identify which days are
   cheapest. Used to pick when to charge the EV. Metric: `rank_top3_of_7`
   (mean overlap between model's and actual's top-3 cheapest days across
   contiguous 7-day windows).
2. **Relative-magnitude accuracy.** Given two days, predict their *ratio* /
   *percentage* difference correctly so the calendar communicates "tomorrow
   is 25% cheaper" rather than "tomorrow is 5% cheaper" without being
   misleading. Currently **unmeasured**.
3. **Absolute price accuracy.** Predicted p/kWh close to actual p/kWh.
   Needed so calendar entries display believable numbers. Metric: MAE /
   RMSE.

The integration historically optimised (3) at the expense of (1) and (2).
With the bucketing fix, (1) is cheap to recover; (3) should also improve;
(2) still needs a metric before we can target it.

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

## Findings from the drift investigation

The diagnostic that surfaced the bug is `scripts/drift_diagnostic.py`. For
one region it fetches 90 days of Agile and Tracker rates and prints:

* Pearson at lags ±2 days (catches day-boundary alignment issues).
* First-half vs second-half regression (catches regime shifts).
* Rolling 30-day correlation (catches drift over the window).
* Day-of-week residual breakdown (catches weekly patterns).
* An A/B comparison of the old UTC-date bucketing vs the current UK-date
  bucketing.

Run it with `python scripts/drift_diagnostic.py --region A`.

### Before the fix

Same period (2026-02-04 → 2026-05-05, region A) showed:

* Lag −1 winning at r = 0.80, lag 0 only at 0.71 — i.e. Tracker[d] looked
  like it correlated more with Agile[d+1] than with Agile[d]. Smoking gun
  for a day-boundary bug.
* First half (Feb–Mar) R² = 0.95, second half (Mar–May) R² = 0.31. The
  "regime shift" lined up almost exactly with the BST clock change on
  2026-03-29.
* DOW residual mean range of 2.6 p/kWh.
* Spearman 0.95 → 0.49 between halves; top3/7 0.85 → 0.53.
* GMT-only window (Dec 20 → Mar 20): no anomalies at all. Lag 0 = 0.97,
  R² = 0.93, DOW range 0.4 p/kWh, top3/7 stable around 0.95.

The "BST-only" pattern was the giveaway.

### After the fix

Same period, same data, just UK-date bucketing:

* Lag 0 wins at r = 0.97. No alignment warning.
* R² = 0.95 across the full 91-day window.
* DOW residual mean range collapses (<0.5 p/kWh).
* GMT-only diagnostic is byte-identical between old and new bucketing —
  the fix is a no-op in winter.

## What this means for the older notes table

The previous version of this file had a table showing `rank_top3_of_7`
falling from 0.91 (04-01) to 0.78 (04-23) to 0.51 (04-24). All of those
numbers were computed against UTC-bucketed daily means.

Re-backfilling `calibration_history.json` with the corrected bucketing
(region A shown):

| date    | window | R²    | spearman | top3/7  | baseline |
| ------- | ------ | ----- | -------- | ------- | -------- |
| 04-01   | 1      | 0.85  | 0.98     | 0.92    | 0.92     |
| 04-06   | 1      | 0.67  | 0.98     | 0.91    | 0.91     |
| 04-13   | 1      | 0.58  | 0.98     | 0.92    | 0.92     |
| 04-23   | 1      | 0.56  | 0.97     | 0.91    | 0.91     |
| 04-24   | 7      | 0.35  | 0.65     | **0.58**| 0.91     |
| 04-27   | 7      | 0.34  | 0.65     | 0.58    | 0.91     |
| 05-04   | 7      | 0.30  | 0.61     | 0.57    | 0.89     |
| 05-06   | 1      | 0.95  | 0.94     | 0.89    | 0.89     |

Two things settle out:

1. **The "underlying drift" from 0.91 → 0.78 was the bug.** With correct
   bucketing, `baseline_top3_of_7` sits steady at 0.91–0.92 across the
   entire 3-month window and only nudges down to 0.89 on the most recent
   dates — well within natural variation. There never was a regime
   change in the Agile↔Tracker relationship.
2. **The window=7 model rows (04-24 → 05-04) show pure smoothing damage.**
   Same data, same UK-bucketing, but the model's `top3/7` collapses from
   0.91 to 0.58 because the slope/intercept were applied to a 7-day
   rolling Agile mean. Baseline rank for those same dates is still 0.91 —
   the Agile spot signal was always there, smoothing just hid it.

The 05-06 row is the post-fix recalibration: window=1 wins, R² jumps to
0.95, and rank is back to 0.89 (= baseline, since `slope·x + intercept`
is monotonic at window=1). The R² values for 04-01 → 04-23 are still
modest because their stored slope/intercept were fit to UTC-bucketed
data; we're applying them to UK-bucketed data so there's some
mismatch. Future entries will use post-fix slope/intercept and should
match 05-06's R².

## Candidate directions (revised)

In priority order. The bucketing fix and follow-up recalibration have
already been done; what remains is mostly about new metrics and
diagnostics.

1. ~~**Recalibrate.**~~ Done on 2026-05-06: window=1 wins for every
   region, R² ≈ 0.94, top3/7 = 0.89. Per-region defaults in `const.py`
   refreshed via `--update-const`.
2. ~~**Re-evaluate `rolling_window` selection.**~~ Window=1 added to
   `ROLLING_WINDOW_CANDIDATES`; grid-search now picks it naturally on
   R². No need to change the objective unless future data shifts the
   trade-off back.
3. **Add a magnitude metric** to recalibration output and backfill it.
   Concrete proposal: for every pair of days within each contiguous 7-day
   window, compute the predicted % difference and the actual % difference;
   regress predicted on actual and record the slope. 1.0 = perfect, < 1.0
   = predictions are too flat (the smoothing failure mode). Still
   outstanding.
4. ~~**Re-backfill `calibration_history.json`.**~~ Done. Older entries
   keep their original (pre-fix) slope/intercept but their rank metrics
   are now computed against UK-bucketed data, which gives clean
   `baseline_top3_of_7` numbers (~0.91 stable across the whole window —
   confirming there was no real drift). The model `top3/7` for the
   window=7 entries (04-24 → 05-04) is still ~0.58 since that's a
   property of those stored slopes applied to a smoothed input, not the
   bucketing.
5. **Investigate any remaining DOW pattern.** With the fix the residual
   range should be small, but worth double-checking against more regions.
6. **Defer:** band-weighting, shorter calibration windows. Both were
   speculative against an artefact-heavy signal. Re-evaluate if rank
   quality regresses again in future recalibrations.

The previously-listed "bypass the regression for the cheapest-day sensor"
direction is still rejected — it would break the calendar UX. With the
bucketing fix in place it's also no longer needed.

## Next concrete step

Add the magnitude metric described in (3). With (1), (2), (4) done, this
is the only remaining gap before we can claim all three goals (rank,
relative magnitude, absolute) are properly measured.
