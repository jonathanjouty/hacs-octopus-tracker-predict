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

import aiohttp

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
) -> list[dict]:
    """Fetch historical unit rates from the Octopus API."""
    tariff_code = f"E-1R-{product_code}-{region}"
    url = f"{OCTOPUS_API_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    period_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )

    all_results: list[dict] = []
    page_url: str | None = url
    params: dict[str, str] = {"period_from": period_from, "page_size": "1500"}

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
    """Group half-hourly rates by date and compute daily means."""
    daily: dict[str, list[float]] = {}
    for rate in rates:
        valid_from = rate.get("valid_from", "")
        value = rate.get("value_inc_vat")
        if not valid_from or value is None:
            continue
        date_str = valid_from[:10]
        daily.setdefault(date_str, []).append(float(value))

    return {date: statistics.mean(vals) for date, vals in daily.items() if vals}


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

            agile_vals = [agile_daily[d] for d in common_dates]
            tracker_vals = [tracker_daily[d] for d in common_dates]

            fit = fit_linear_model(agile_vals, tracker_vals)
            if fit is None:
                _LOG.warning("  Fit failed for region %s", region)
                continue

            slope, intercept, r_squared, samples = fit

            # Compute residual summary statistics
            residuals = [ty - (slope * ax + intercept) for ax, ty in zip(agile_vals, tracker_vals)]
            abs_residuals = [abs(r) for r in residuals]
            mae = round(statistics.mean(abs_residuals), 4)
            rmse = round((sum(r**2 for r in residuals) / len(residuals)) ** 0.5, 4)
            std_res = round(statistics.stdev(residuals), 4) if len(residuals) > 1 else 0.0
            max_abs_res = round(max(abs_residuals), 4)

            results[region] = {
                "slope": round(slope, 4),
                "intercept": round(intercept, 2),
                "r_squared": round(r_squared, 4),
                "samples": samples,
                "mae": mae,
                "rmse": rmse,
                "std_residual": std_res,
                "max_abs_residual": max_abs_res,
            }
            _LOG.info(
                "  Region %s: slope=%.4f, intercept=%.2f, R²=%.4f, samples=%d, MAE=%.4f, RMSE=%.4f",
                region, slope, intercept, r_squared, samples, mae, rmse,
            )

    return results


# ── const.py updater ─────────────────────────────────────────────────────────────────


def update_const_file(results: dict[str, dict], const_path: Path) -> None:
    """Replace DEFAULT_CALIBRATION dict in const.py with new values."""
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

    # Also update DEFAULT_SLOPE and DEFAULT_INTERCEPT with the mean across all regions
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
            "slope", "intercept", "r_squared", "samples",
            "mae", "rmse", "std_residual", "max_abs_residual",
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
