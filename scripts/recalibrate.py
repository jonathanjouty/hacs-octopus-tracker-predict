#!/usr/bin/env python3
"""Recalibrate default per-region linear models from live Octopus API data.

Usage:
    python scripts/recalibrate.py                  # print results as JSON
    python scripts/recalibrate.py --update-const   # update const.py in-place

Requires: aiohttp (pip install aiohttp)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

_UK_TZ = ZoneInfo("Europe/London")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_LOG = logging.getLogger(__name__)

# ── Constants (duplicated from const.py to keep this script standalone) ───

REGIONS = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside and North Wales",
    "E": "West Midlands",
    "F": "North Eastern England",
    "G": "North Western England",
    "H": "Northern Scotland",
    "J": "South Eastern England",
    "K": "Southern England",
    "L": "South Wales",
    "M": "South Western England",
    "N": "Southern Scotland",
    "P": "Yorkshire",
}

OCTOPUS_API_BASE = "https://api.octopus.energy/v1"
OCTOPUS_PRODUCTS_URL = f"{OCTOPUS_API_BASE}/products/"

DEFAULT_AGILE_PRODUCT = "AGILE-24-10-01"

# All known Octopus Tracker (SILVER) product codes, newest first.
# Tracker products are not listed in the Octopus products API — probe directly.
# Keep in sync with KNOWN_TRACKER_PRODUCTS in const.py.
KNOWN_TRACKER_PRODUCTS = [
    "SILVER-25-09-02",       # September 2025 v1
    "SILVER-25-04-15",       # April 2025 v1
    "SILVER-24-12-31",       # December 2024 v1
    "SILVER-24-10-01",       # October 2024 v1
    "SILVER-24-07-01",       # July 2024 v1
    "SILVER-24-04-03",       # April 2024 v1
    "SILVER-23-12-06",       # December 2023 v1
    "SILVER-FLEX-22-11-25",  # November 2022 v1 (original)
]
DEFAULT_TRACKER_PRODUCT = KNOWN_TRACKER_PRODUCTS[0]

CALIBRATION_DAYS = 90
MIN_SAMPLES = 7
# Candidate rolling windows (days) tried during calibration; best R² wins.
ROLLING_WINDOW_CANDIDATES = [7, 14, 21, 30]


# ── API helpers ──────────────────────────────────────────────────────────────────


async def discover_product_code(
    session: aiohttp.ClientSession, prefix: str
) -> str | None:
    """Discover the latest product code matching a prefix.

    For SILVER (Tracker), probes KNOWN_TRACKER_PRODUCTS directly because
    Octopus excludes Tracker products from the listing API.
    For other prefixes, searches the products listing API.
    """
    if prefix == "SILVER":
        return await _discover_tracker_product_code(session)

    all_results: list[dict] = []
    url: str | None = OCTOPUS_PRODUCTS_URL
    params: dict[str, str] = {"page_size": "100"}

    try:
        while url:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()

            all_results.extend(data.get("results", []))
            url = data.get("next")
            params = {}  # next URL already includes params

        matches = [p for p in all_results if p.get("code", "").startswith(prefix)]
        if not matches:
            return None

        matches.sort(key=lambda p: p.get("available_from", ""), reverse=True)
        return matches[0]["code"]
    except Exception:
        _LOG.exception("Error discovering product code for prefix '%s'", prefix)
        return None


async def _discover_tracker_product_code(session: aiohttp.ClientSession) -> str | None:
    """Find the active Tracker product by probing KNOWN_TRACKER_PRODUCTS directly."""
    first_existing: str | None = None
    for code in KNOWN_TRACKER_PRODUCTS:
        try:
            url = f"{OCTOPUS_API_BASE}/products/{code}/"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
            if first_existing is None:
                first_existing = code
            if data.get("available_to") is None:
                _LOG.info("Discovered active Tracker product: %s", code)
                return code
        except Exception:
            _LOG.debug("Error probing Tracker product %s", code, exc_info=True)

    if first_existing:
        _LOG.warning(
            "No Tracker product in KNOWN_TRACKER_PRODUCTS has available_to=null. "
            "A newer product code may exist — update KNOWN_TRACKER_PRODUCTS. "
            "Using most recent known code: %s",
            first_existing,
        )
    return first_existing


async def fetch_rates(
    session: aiohttp.ClientSession,
    product_code: str,
    region: str,
    days: int,
    period_to: datetime | None = None,
) -> list[dict]:
    """Fetch historical unit rates from the Octopus API.

    If ``period_to`` is given, fetches the ``days``-long window ending on that
    instant; otherwise the window ends "now". This enables retroactive fetches
    of windows that ended on a past date (used by the rank-metric backfill).
    """
    tariff_code = f"E-1R-{product_code}-{region}"
    url = f"{OCTOPUS_API_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    end = period_to if period_to is not None else datetime.now(timezone.utc)
    period_from = (end - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")

    all_results: list[dict] = []
    page_url: str | None = url
    params: dict[str, str] = {"period_from": period_from, "page_size": "1500"}
    if period_to is not None:
        params["period_to"] = period_to.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        while page_url:
            async with session.get(page_url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    _LOG.warning("API returned %d for %s region %s", resp.status, product_code, region)
                    break
                data = await resp.json()

            all_results.extend(data.get("results", []))
            page_url = data.get("next")
            params = {}
    except Exception:
        _LOG.exception("Error fetching rates for %s region %s", product_code, region)

    return all_results


def compute_daily_means(rates: list[dict]) -> dict[str, float]:
    """Group half-hourly rates by UK local date and compute daily means.

    Octopus rate ``valid_from`` is UTC. During BST the first hour of each UK
    day belongs to the previous UTC date, so bucketing on ``valid_from[:10]``
    misallocates those slots and biases the daily mean. Convert to
    ``Europe/London`` before extracting the date.

    Must match ``custom_components.tracker_predict.calibration.compute_daily_means``.
    """
    daily: dict[str, list[float]] = {}
    for rate in rates:
        valid_from = rate.get("valid_from", "")
        value = rate.get("value_inc_vat")
        if not valid_from or value is None:
            continue
        try:
            dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except ValueError:
            continue
        date_str = dt.astimezone(_UK_TZ).strftime("%Y-%m-%d")
        daily.setdefault(date_str, []).append(float(value))

    return {date: statistics.mean(vals) for date, vals in daily.items() if vals}


def compute_rolling_means(daily_means: dict[str, float], window: int) -> dict[str, float]:
    """Compute a trailing N-day mean for each date in the series."""
    sorted_dates = sorted(daily_means)
    result: dict[str, float] = {}
    for i, date in enumerate(sorted_dates):
        start = max(0, i - window + 1)
        window_vals = [daily_means[d] for d in sorted_dates[start : i + 1]]
        result[date] = statistics.mean(window_vals)
    return result


def fit_linear_model(
    agile_means: list[float], tracker_rates: list[float]
) -> tuple[float, float, float, int] | None:
    """Fit linear regression, return (slope, intercept, r_squared, sample_count) or None."""
    n = len(agile_means)
    if n < MIN_SAMPLES:
        return None

    mean_x = statistics.mean(agile_means)
    mean_y = statistics.mean(tracker_rates)

    denominator = sum((x - mean_x) ** 2 for x in agile_means)
    if denominator == 0:
        return None

    numerator = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(agile_means, tracker_rates)
    )
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x

    ss_res = sum(
        (y - (slope * x + intercept)) ** 2
        for x, y in zip(agile_means, tracker_rates)
    )
    ss_tot = sum((y - mean_y) ** 2 for y in tracker_rates)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return (slope, intercept, r_squared, n)


def _average_ranks(values: list[float]) -> list[float]:
    """Return the average ranks (1-based) of values. Tied values get the
    average of the ranks they would otherwise occupy.

    Example: [10, 20, 20, 30] -> [1, 2.5, 2.5, 4]
    """
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # +1 for 1-based ranking
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_rho(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation coefficient.

    Computed as the Pearson correlation of the average-ranked inputs, which
    gives the correct value when ties are present.
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    cov = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    var_x = sum((rx[i] - mean_rx) ** 2 for i in range(n))
    var_y = sum((ry[i] - mean_ry) ** 2 for i in range(n))
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / (var_x * var_y) ** 0.5


def top_n_window_overlap(
    predicted: list[float],
    actual: list[float],
    n: int = 3,
    window: int = 7,
) -> float:
    """Mean overlap of the n cheapest days picked by ``predicted`` vs ``actual``
    across every contiguous ``window``-length slice of the time-ordered series.

    Returns a value in [0, 1]: 1.0 means the predictor identifies the same set
    of n cheapest days as the actuals in every window. The two input lists are
    assumed to be aligned and sorted by date.

    With ties, the n indices picked are the n smallest by (value, index) — i.e.
    earliest tie-broken — which keeps the metric deterministic.
    """
    total = len(predicted)
    if total != len(actual) or total < window or n <= 0 or n > window:
        return 0.0

    overlaps: list[float] = []
    for start in range(total - window + 1):
        idx_window = list(range(start, start + window))
        pred_top = set(sorted(idx_window, key=lambda i: (predicted[i], i))[:n])
        actual_top = set(sorted(idx_window, key=lambda i: (actual[i], i))[:n])
        overlaps.append(len(pred_top & actual_top) / n)

    if not overlaps:
        return 0.0
    return sum(overlaps) / len(overlaps)


def residuals_by_quintile(
    agile_vals: list[float],
    tracker_vals: list[float],
    slope: float,
    intercept: float,
    n_buckets: int = 5,
) -> list[dict]:
    """Return mean signed residual per quintile of agile_mean (sorted low→high).

    A negative slope across buckets (positive at low agile, negative at high)
    indicates that the model over-predicts at high prices and under-predicts at
    low prices — the signature of using a spot price instead of a rolling mean.
    """
    pairs = sorted(zip(agile_vals, tracker_vals), key=lambda p: p[0])
    n = len(pairs)
    buckets = []
    for i in range(n_buckets):
        start = (i * n) // n_buckets
        end = ((i + 1) * n) // n_buckets
        bucket = pairs[start:end]
        ax_vals = [p[0] for p in bucket]
        signed_residuals = [ty - (slope * ax + intercept) for ax, ty in bucket]
        buckets.append({
            "agile_mean_avg": round(statistics.mean(ax_vals), 2),
            "mean_signed_residual": round(statistics.mean(signed_residuals), 4),
            "n": len(bucket),
        })
    return buckets


# ── Main calibration logic ───────────────────────────────────────────────────────────


async def calibrate_all_regions() -> dict[str, dict]:
    """Run calibration for all regions. Returns {region: {slope, intercept, r_squared, samples}}."""
    async with aiohttp.ClientSession() as session:
        # Discover product codes once
        agile_product = await discover_product_code(session, "AGILE") or DEFAULT_AGILE_PRODUCT
        tracker_product = await discover_product_code(session, "SILVER") or DEFAULT_TRACKER_PRODUCT
        _LOG.info("Using Agile product: %s", agile_product)
        _LOG.info("Using Tracker product: %s", tracker_product)

        results: dict[str, dict] = {}

        for region, name in REGIONS.items():
            _LOG.info("Calibrating region %s (%s)...", region, name)

            agile_rates = await fetch_rates(session, agile_product, region, CALIBRATION_DAYS)
            tracker_rates = await fetch_rates(session, tracker_product, region, CALIBRATION_DAYS)

            if not agile_rates or not tracker_rates:
                _LOG.warning("  No data for region %s, skipping", region)
                continue

            agile_daily = compute_daily_means(agile_rates)
            tracker_daily = compute_daily_means(tracker_rates)

            common_dates = sorted(set(agile_daily) & set(tracker_daily))
            if len(common_dates) < MIN_SAMPLES:
                _LOG.warning("  Only %d common dates for region %s, skipping", len(common_dates), region)
                continue

            agile_spot_vals = [agile_daily[d] for d in common_dates]
            tracker_vals = [tracker_daily[d] for d in common_dates]

            # Grid-search rolling window: pick the window that maximises R²
            best_fit = None
            best_r2 = -1.0
            best_window = ROLLING_WINDOW_CANDIDATES[0]
            window_r2: dict[int, float] = {}
            for window in ROLLING_WINDOW_CANDIDATES:
                rolling = compute_rolling_means(agile_daily, window)
                rolling_vals = [rolling[d] for d in common_dates]
                fit = fit_linear_model(rolling_vals, tracker_vals)
                if fit is None:
                    continue
                r2 = fit[2]
                window_r2[window] = round(r2, 4)
                if r2 > best_r2:
                    best_r2 = r2
                    best_fit = fit
                    best_window = window
                    best_agile_vals = rolling_vals

            if best_fit is None:
                _LOG.warning("  Fit failed for region %s", region)
                continue

            slope, intercept, r_squared, samples = best_fit
            agile_vals = best_agile_vals  # rolling means for the winning window

            # Compute residual summary statistics
            residuals = [ty - (slope * ax + intercept) for ax, ty in zip(agile_vals, tracker_vals)]
            abs_residuals = [abs(r) for r in residuals]
            mae = round(statistics.mean(abs_residuals), 4)
            rmse = round((sum(r**2 for r in residuals) / len(residuals)) ** 0.5, 4)
            std_res = round(statistics.stdev(residuals), 4) if len(residuals) > 1 else 0.0
            max_abs_res = round(max(abs_residuals), 4)

            # Spot-price baseline: residuals by quintile reveal bias at extremes
            spot_fit = fit_linear_model(agile_spot_vals, tracker_vals)
            spot_quintiles: list[dict] = []
            if spot_fit is not None:
                spot_quintiles = residuals_by_quintile(
                    agile_spot_vals, tracker_vals, spot_fit[0], spot_fit[1]
                )

            # Rank-accuracy metrics. The integration's user-facing goal is
            # picking the cheapest charging days, so rank metrics matter more
            # than absolute-error metrics. ``baseline_top3_of_7`` uses raw Agile
            # spot means with no model, exposing whether the linear regression
            # adds rank value beyond ranking by Agile alone.
            model_pred = [slope * x + intercept for x in agile_vals]
            rank_spearman = round(spearman_rho(model_pred, tracker_vals), 4)
            rank_top3 = round(
                top_n_window_overlap(model_pred, tracker_vals, n=3, window=7), 4
            )
            baseline_top3 = round(
                top_n_window_overlap(agile_spot_vals, tracker_vals, n=3, window=7), 4
            )

            results[region] = {
                "slope": round(slope, 4),
                "intercept": round(intercept, 2),
                "r_squared": round(r_squared, 4),
                "rolling_window": best_window,
                "window_r2_comparison": window_r2,
                "samples": samples,
                "mae": mae,
                "rmse": rmse,
                "std_residual": std_res,
                "max_abs_residual": max_abs_res,
                "rank_spearman": rank_spearman,
                "rank_top3_of_7": rank_top3,
                "baseline_top3_of_7": baseline_top3,
                "spot_residuals_by_quintile": spot_quintiles,
            }
            _LOG.info(
                "  Region %s: slope=%.4f, intercept=%.2f, R²=%.4f (window=%d), "
                "samples=%d, MAE=%.4f, RMSE=%.4f, ρ=%.4f, top3/7=%.4f (baseline %.4f)",
                region, slope, intercept, r_squared, best_window, samples, mae, rmse,
                rank_spearman, rank_top3, baseline_top3,
            )

    return results


# ── const.py updater ─────────────────────────────────────────────────────────────────


def update_const_file(results: dict[str, dict], const_path: Path) -> None:
    """Replace DEFAULT_CALIBRATION dict and rolling window in const.py with new values."""
    content = const_path.read_text()

    # Build replacement dict literal
    lines = ["DEFAULT_CALIBRATION: dict[str, tuple[float, float]] = {"]
    for region in sorted(results, key=lambda r: list(REGIONS.keys()).index(r)):
        data = results[region]
        lines.append(f'    "{region}": ({data["slope"]}, {data["intercept"]}),')  # noqa: E501
    lines.append("}")
    new_dict = "\n".join(lines)

    # Replace existing DEFAULT_CALIBRATION block
    pattern = r"DEFAULT_CALIBRATION: dict\[str, tuple\[float, float\]\] = \{[^}]+\}"
    new_content, count = re.subn(pattern, new_dict, content, flags=re.DOTALL)

    if count == 0:
        _LOG.error("Could not find DEFAULT_CALIBRATION in %s", const_path)
        sys.exit(1)

    # Update DEFAULT_SLOPE and DEFAULT_INTERCEPT with the mean across all regions
    all_slopes = [r["slope"] for r in results.values()]
    all_intercepts = [r["intercept"] for r in results.values()]
    avg_slope = round(statistics.mean(all_slopes), 4) if all_slopes else 0.56
    avg_intercept = round(statistics.mean(all_intercepts), 2) if all_intercepts else 12.75

    new_content = re.sub(
        r"DEFAULT_SLOPE = [\d.]+",
        f"DEFAULT_SLOPE = {avg_slope}",
        new_content,
    )
    new_content = re.sub(
        r"DEFAULT_INTERCEPT = [\d.]+",
        f"DEFAULT_INTERCEPT = {avg_intercept}",
        new_content,
    )

    # Update DEFAULT_ROLLING_WINDOW with the most common window across regions
    windows = [r["rolling_window"] for r in results.values()]
    # Use the window that appeared most often; tie-break by smallest
    from collections import Counter
    window_counts = Counter(windows)
    best_window = min(window_counts, key=lambda w: (-window_counts[w], w))
    new_content = re.sub(
        r"DEFAULT_ROLLING_WINDOW = \d+",
        f"DEFAULT_ROLLING_WINDOW = {best_window}",
        new_content,
    )

    const_path.write_text(new_content)
    _LOG.info("Updated %s with calibration for %d regions", const_path, len(results))


def update_history_file(results: dict[str, dict], history_path: Path) -> None:
    """Append calibration results with residual stats and drift deltas to a JSON history file."""
    history: list[dict] = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
        except (json.JSONDecodeError, OSError):
            _LOG.warning("Could not read %s, starting fresh", history_path)
            history = []

    # Compute drift deltas vs previous entry
    prev_regions: dict[str, dict] = {}
    if history:
        prev_regions = history[-1].get("regions", {})

    regions_entry: dict[str, dict] = {}
    for region, data in results.items():
        entry = {k: data[k] for k in (
            "slope", "intercept", "r_squared", "rolling_window", "samples",
            "mae", "rmse", "std_residual", "max_abs_residual",
            "rank_spearman", "rank_top3_of_7", "baseline_top3_of_7",
        )}
        prev = prev_regions.get(region)
        if prev is not None:
            entry["slope_delta"] = round(data["slope"] - prev["slope"], 4)
            entry["intercept_delta"] = round(data["intercept"] - prev["intercept"], 4)
        else:
            entry["slope_delta"] = None
            entry["intercept_delta"] = None
        regions_entry[region] = entry

    history.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "regions": regions_entry,
    })

    history_path.write_text(json.dumps(history, indent=2) + "\n")
    _LOG.info("Updated history file %s (%d entries)", history_path, len(history))


# ── Entry point ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Recalibrate per-region defaults")
    parser.add_argument(
        "--update-const",
        action="store_true",
        help="Update const.py in-place with new calibration values",
    )
    args = parser.parse_args()

    results = asyncio.run(calibrate_all_regions())

    if not results:
        _LOG.error("No calibration results produced")
        sys.exit(1)

    if args.update_const:
        const_path = Path(__file__).resolve().parent.parent / "custom_components" / "tracker_predict" / "const.py"
        if not const_path.exists():
            _LOG.error("const.py not found at %s", const_path)
            sys.exit(1)
        update_const_file(results, const_path)
        history_path = Path(__file__).resolve().parent.parent / "calibration_history.json"
        update_history_file(results, history_path)
    else:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
