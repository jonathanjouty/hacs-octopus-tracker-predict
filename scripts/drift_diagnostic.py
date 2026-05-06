#!/usr/bin/env python3
"""Diagnostic for the Agile↔Tracker baseline drift documented in
``rank-accuracy-notes.md``.

For one region (default ``A``), fetches the last 90 days of Agile and Tracker
half-hourly rates and prints a series of analyses designed to surface:

* timezone / day-boundary alignment bugs (lag-correlation test);
* regime changes inside the window (first-half vs second-half fits);
* day-of-week effects (residual breakdown by weekday);
* a date-aligned table to scan visually.

This is intentionally a printout rather than a saved-figure plot — the
integration runs without numpy / matplotlib and we want a tool that's runnable
anywhere `aiohttp` is available. Output is plain text.

Usage:
    python scripts/drift_diagnostic.py            # region A, ending now
    python scripts/drift_diagnostic.py --region D
    python scripts/drift_diagnostic.py --region A --period-to 2026-04-23
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import statistics
import sys
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import aiohttp

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.recalibrate import (  # noqa: E402
    DEFAULT_AGILE_PRODUCT,
    DEFAULT_TRACKER_PRODUCT,
    compute_daily_means,
    discover_product_code,
    fetch_rates,
    fit_linear_model,
    spearman_rho,
    top_n_window_overlap,
)


def compute_daily_means_utc(rates: list[dict]) -> dict[str, float]:
    """Reference implementation of the *old* (buggy) UTC-date bucketing.

    Kept here purely so the A/B section of this diagnostic can show the
    delta against today's UK-bucketed implementation.
    """
    daily: dict[str, list[float]] = {}
    for rate in rates:
        valid_from = rate.get("valid_from", "")
        value = rate.get("value_inc_vat")
        if not valid_from or value is None:
            continue
        date_str = valid_from[:10]
        daily.setdefault(date_str, []).append(float(value))
    return {date: statistics.mean(vals) for date, vals in daily.items() if vals}

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

CALIBRATION_DAYS = 90
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def lag_correlation(
    agile_daily: dict[str, float], tracker_daily: dict[str, float], lag: int
) -> tuple[float, int]:
    """Pearson correlation of Agile shifted by ``lag`` days vs Tracker.

    A positive lag means Agile is shifted forward in time (Agile[d-lag] paired
    with Tracker[d]) — i.e. testing whether Tracker tracks past Agile. A
    negative lag is the opposite. Returns (correlation, n_pairs_used).
    """
    pairs: list[tuple[float, float]] = []
    for d_str, t_val in tracker_daily.items():
        d = date_cls.fromisoformat(d_str)
        a_d = (d - timedelta(days=lag)).isoformat()
        a_val = agile_daily.get(a_d)
        if a_val is None:
            continue
        pairs.append((a_val, t_val))
    if len(pairs) < 7:
        return (0.0, len(pairs))
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return (pearson(xs, ys), len(pairs))


def rolling_correlation(
    agile_vals: list[float], tracker_vals: list[float], window: int = 30
) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for start in range(0, len(agile_vals) - window + 1):
        a = agile_vals[start : start + window]
        t = tracker_vals[start : start + window]
        out.append((start, pearson(a, t)))
    return out


async def run(region: str, period_to: datetime | None) -> int:
    async with aiohttp.ClientSession() as session:
        agile_product = (
            await discover_product_code(session, "AGILE") or DEFAULT_AGILE_PRODUCT
        )
        tracker_product = (
            await discover_product_code(session, "SILVER") or DEFAULT_TRACKER_PRODUCT
        )
        print(f"# Drift diagnostic — region {region}")
        print(f"# Agile product:   {agile_product}")
        print(f"# Tracker product: {tracker_product}")
        if period_to:
            print(f"# Period ends:     {period_to.isoformat()}")
        else:
            print("# Period ends:     now")
        print()

        agile_rates = await fetch_rates(
            session, agile_product, region, CALIBRATION_DAYS, period_to=period_to
        )
        tracker_rates = await fetch_rates(
            session, tracker_product, region, CALIBRATION_DAYS, period_to=period_to
        )

    if not agile_rates or not tracker_rates:
        print("ERROR: empty API response")
        return 1

    agile_daily = compute_daily_means(agile_rates)
    tracker_daily = compute_daily_means(tracker_rates)
    common = sorted(set(agile_daily) & set(tracker_daily))
    if len(common) < 30:
        print(f"ERROR: only {len(common)} common dates")
        return 1

    agile_vals = [agile_daily[d] for d in common]
    tracker_vals = [tracker_daily[d] for d in common]

    # Old UTC-bucketed parallel series for the A/B comparison.
    agile_daily_utc = compute_daily_means_utc(agile_rates)
    tracker_daily_utc = compute_daily_means_utc(tracker_rates)
    common_utc = sorted(set(agile_daily_utc) & set(tracker_daily_utc))
    agile_vals_utc = [agile_daily_utc[d] for d in common_utc]
    tracker_vals_utc = [tracker_daily_utc[d] for d in common_utc]

    print(f"## Window: {common[0]} → {common[-1]}  ({len(common)} days)")
    print()

    # ── 0. UTC vs UK bucketing A/B ───────────────────────────────────────
    print("## A/B: old UTC-date bucketing vs current UK-date bucketing")
    fit_utc = fit_linear_model(agile_vals_utc, tracker_vals_utc)
    fit_uk = fit_linear_model(agile_vals, tracker_vals)
    print(f"{'bucketing':>9}  {'days':>5}  {'pearson':>8}  {'spearman':>9}  {'top3/7':>7}  {'R²':>6}")
    for label, a, t, fit in (
        ("UTC (old)", agile_vals_utc, tracker_vals_utc, fit_utc),
        ("UK (now)", agile_vals, tracker_vals, fit_uk),
    ):
        r = pearson(a, t)
        sp = spearman_rho(a, t)
        top = top_n_window_overlap(a, t, 3, 7)
        r2 = fit[2] if fit else 0.0
        print(f"{label:>9}  {len(a):>5}  {r:>8.4f}  {sp:>9.4f}  {top:>7.4f}  {r2:>6.3f}")
    print()

    # ── 1. Lag-correlation test (timezone / day-boundary) ────────────────
    print("## Lag correlation (Pearson, Agile shifted by k days vs Tracker)")
    print(f"{'lag':>5}  {'r':>8}  {'n':>4}")
    best_lag = 0
    best_r = -2.0
    for lag in range(-2, 3):
        r, n = lag_correlation(agile_daily, tracker_daily, lag)
        marker = "  <-- best" if False else ""
        if r > best_r:
            best_r = r
            best_lag = lag
        print(f"{lag:>5}  {r:>8.4f}  {n:>4}{marker}")
    print(f"  best lag = {best_lag} (r = {best_r:.4f})")
    if best_lag == 0:
        print("  → no obvious timezone/day-boundary alignment issue.")
    else:
        print(f"  → ALIGNMENT WARNING: lag {best_lag} beats lag 0; investigate.")
    print()

    # ── 2. First-half vs second-half regression ──────────────────────────
    half = len(common) // 2
    a1, t1 = agile_vals[:half], tracker_vals[:half]
    a2, t2 = agile_vals[half:], tracker_vals[half:]
    fit_full = fit_linear_model(agile_vals, tracker_vals)
    fit1 = fit_linear_model(a1, t1)
    fit2 = fit_linear_model(a2, t2)
    print("## First-half vs second-half linear fit")
    print(f"{'segment':>14}  {'days':>5}  {'slope':>7}  {'intercept':>10}  {'R²':>6}")
    for label, fit, n in (
        (f"full {common[0]}..{common[-1]}", fit_full, len(common)),
        (f"first {common[0]}..{common[half-1]}", fit1, len(a1)),
        (f"second {common[half]}..{common[-1]}", fit2, len(a2)),
    ):
        if fit is None:
            print(f"{label:>14}  {n:>5}  (could not fit)")
            continue
        s, i, r2, _ = fit
        print(f"{label:>40}  {n:>5}  {s:>7.4f}  {i:>10.2f}  {r2:>6.3f}")
    if fit1 and fit2:
        ds = fit2[0] - fit1[0]
        di = fit2[1] - fit1[1]
        print(f"  Δslope = {ds:+.4f}, Δintercept = {di:+.2f}")
        if abs(ds) > 0.05 or abs(di) > 1.0:
            print("  → REGIME SHIFT: the linear relationship has changed meaningfully.")
        else:
            print("  → no major regime shift in the linear fit.")
    print()

    # ── 3. Rolling 30-day correlation ────────────────────────────────────
    print("## Rolling 30-day Pearson correlation (Agile vs Tracker)")
    rolling = rolling_correlation(agile_vals, tracker_vals, window=30)
    if rolling:
        first = rolling[0][1]
        last = rolling[-1][1]
        mn = min(r for _, r in rolling)
        mx = max(r for _, r in rolling)
        print(f"  first window starting {common[0]}:  r = {first:.4f}")
        print(f"  last  window starting {common[rolling[-1][0]]}:  r = {last:.4f}")
        print(f"  min over all windows: {mn:.4f}")
        print(f"  max over all windows: {mx:.4f}")
        if first - last > 0.10:
            print("  → r has dropped >0.10 across the window — consistent with drift.")
        elif last - first > 0.10:
            print("  → r has IMPROVED across the window.")
        else:
            print("  → correlation is roughly stable.")
    print()

    # ── 4. Day-of-week residuals ─────────────────────────────────────────
    print("## Residuals by day-of-week (using full-window fit)")
    if fit_full:
        slope, intercept = fit_full[0], fit_full[1]
        by_dow: dict[int, list[float]] = {}
        for d_str, a, t in zip(common, agile_vals, tracker_vals):
            d = date_cls.fromisoformat(d_str)
            r = t - (slope * a + intercept)
            by_dow.setdefault(d.weekday(), []).append(r)
        print(f"{'dow':>4}  {'n':>3}  {'mean_resid':>11}  {'std_resid':>10}")
        for dow in range(7):
            vals = by_dow.get(dow, [])
            if not vals:
                continue
            m = statistics.mean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            print(f"{WEEKDAY_NAMES[dow]:>4}  {len(vals):>3}  {m:>+11.3f}  {sd:>10.3f}")
        means = [statistics.mean(v) for v in by_dow.values() if v]
        if max(means) - min(means) > 1.0:
            print("  → DOW EFFECT: residual mean spans >1 p/kWh across weekdays.")
        else:
            print("  → no large DOW effect in residual mean.")
    print()

    # ── 5. Rank-quality split: first half vs second half ─────────────────
    print("## Rank quality on each half (Agile-spot vs Tracker)")
    print(f"  first half (n={len(a1)}):  spearman={spearman_rho(a1, t1):.4f}  top3/7={top_n_window_overlap(a1, t1, 3, 7):.4f}")
    print(f"  second half (n={len(a2)}): spearman={spearman_rho(a2, t2):.4f}  top3/7={top_n_window_overlap(a2, t2, 3, 7):.4f}")
    print()

    # ── 6. Date-aligned table tail (last 14 days) ────────────────────────
    print("## Last 14 days (date | agile | tracker | resid | a_rank | t_rank in tail)")
    tail_n = min(14, len(common))
    tail_dates = common[-tail_n:]
    tail_a = agile_vals[-tail_n:]
    tail_t = tracker_vals[-tail_n:]
    tail_a_rank = sorted(range(tail_n), key=lambda i: tail_a[i])
    tail_t_rank = sorted(range(tail_n), key=lambda i: tail_t[i])
    a_rank_pos = {i: r + 1 for r, i in enumerate(tail_a_rank)}
    t_rank_pos = {i: r + 1 for r, i in enumerate(tail_t_rank)}
    if fit_full:
        slope, intercept = fit_full[0], fit_full[1]
    else:
        slope, intercept = 0.0, 0.0
    print(f"{'date':>10}  {'agile':>6}  {'tracker':>7}  {'resid':>6}  {'a_r':>3}  {'t_r':>3}")
    for i, (d_str, a, t) in enumerate(zip(tail_dates, tail_a, tail_t)):
        r = t - (slope * a + intercept)
        print(f"{d_str:>10}  {a:>6.2f}  {t:>7.2f}  {r:>+6.2f}  {a_rank_pos[i]:>3}  {t_rank_pos[i]:>3}")
    print()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="A")
    p.add_argument(
        "--period-to",
        type=lambda s: datetime.combine(
            date_cls.fromisoformat(s), time(23, 59, 59), tzinfo=timezone.utc
        ),
        default=None,
        help="ISO date; window ends at 23:59:59Z on this day (default: now)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.region, args.period_to)))


if __name__ == "__main__":
    main()
