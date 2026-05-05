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
numbers were computed against UTC-bucketed daily means. They overstate
both:

* The "underlying drift" — most of it was the BST bucketing bug worsening
  as the window accumulated more BST days.
* The rolling-window damage — a chunk of the rank loss between 04-23 and
  04-24 came from BST entering the calibration window, not from
  `rolling_window` jumping from 1 to 7.

We should re-backfill `calibration_history.json` after the next live
recalibration so the historical metrics reflect the corrected bucketing.

## Candidate directions (revised)

In priority order. With the bucketing bug fixed, the previous priorities
shift.

1. **Recalibrate.** The current `DEFAULT_CALIBRATION` slope/intercept and
   `DEFAULT_ROLLING_WINDOW` were fit on UTC-bucketed data and inherit the
   bias. Run `python scripts/recalibrate.py` with the fixed code, inspect
   the new rank metrics, decide whether `--update-const` is appropriate.
2. **Re-evaluate `rolling_window` selection.** With BST noise removed,
   window=1 may now beat window=7 on R² as well as on rank — meaning the
   grid-search will pick the rank-friendly option naturally. If not,
   change the grid-search objective from R² to something rank-aware (e.g.
   `0.5 * R² + 0.5 * rank_top3_of_7`).
3. **Add a magnitude metric** to recalibration output and backfill it.
   Concrete proposal: for every pair of days within each contiguous 7-day
   window, compute the predicted % difference and the actual % difference;
   regress predicted on actual and record the slope. 1.0 = perfect, < 1.0
   = predictions are too flat (the smoothing failure mode).
4. **Re-backfill `calibration_history.json`.** Once recalibration has run
   with the fix, run `scripts/backfill_rank_metrics.py` to regenerate
   metrics across all entries. Old rows still have UTC-biased
   slope/intercept stored, so the regenerated metrics will still be
   imperfect for them — the value is mostly to track the *post-fix* trend
   going forward.
5. **Investigate any remaining DOW pattern.** With the fix the residual
   range should be small, but worth double-checking against more regions.
6. **Defer:** band-weighting, shorter calibration windows. Both were
   speculative against an artefact-heavy signal. Re-evaluate after (1)–(3).

The previously-listed "bypass the regression for the cheapest-day sensor"
direction is still rejected — it would break the calendar UX. With the
bucketing fix in place it's also no longer needed.

## Next concrete steps

1. Run `python scripts/recalibrate.py` (no `--update-const` first), inspect
   per-region rank metrics and the new `rolling_window` selections. If
   they look good, run with `--update-const` to refresh the per-region
   defaults baked into `const.py`.
2. Run `python scripts/backfill_rank_metrics.py` to refresh metrics in
   `calibration_history.json`.
3. Open a follow-up to add the magnitude metric described above.
