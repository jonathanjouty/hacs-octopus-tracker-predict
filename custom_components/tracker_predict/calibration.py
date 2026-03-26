"""Calibration module: fetches historical Agile/Tracker rates and fits a linear model."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession

from .const import (
    DEFAULT_INTERCEPT,
    DEFAULT_SLOPE,
    OCTOPUS_API_BASE,
    OCTOPUS_PRODUCTS_URL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CalibrationModel:
    """Linear model: tracker_est = slope * agile_daily_mean + intercept."""

    slope: float
    intercept: float
    r_squared: float
    calibrated_at: datetime
    sample_count: int

    def predict(self, agile_daily_mean: float) -> float:
        """Predict Tracker rate from Agile daily mean, clamped to [0, 100]."""
        return max(0.0, min(100.0, self.slope * agile_daily_mean + self.intercept))


def default_model() -> CalibrationModel:
    """Return fallback model from 2025 East England data."""
    return CalibrationModel(
        slope=DEFAULT_SLOPE,
        intercept=DEFAULT_INTERCEPT,
        r_squared=0.0,
        calibrated_at=datetime.now(timezone.utc),
        sample_count=0,
    )


def fit_linear_model(
    agile_means: list[float], tracker_rates: list[float]
) -> CalibrationModel:
    """Fit a simple linear regression from paired daily values.

    Uses pure Python (statistics module) to avoid numpy dependency.
    """
    n = len(agile_means)
    if n < 7:
        _LOGGER.warning(
            "Only %d paired days available for calibration, need at least 7. "
            "Using default model.",
            n,
        )
        return default_model()

    mean_x = statistics.mean(agile_means)
    mean_y = statistics.mean(tracker_rates)

    # Compute slope and intercept
    numerator = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(agile_means, tracker_rates)
    )
    denominator = sum((x - mean_x) ** 2 for x in agile_means)

    if denominator == 0:
        _LOGGER.warning("Zero variance in Agile means, using default model.")
        return default_model()

    slope = numerator / denominator
    intercept = mean_y - slope * mean_x

    # Compute R²
    ss_res = sum(
        (y - (slope * x + intercept)) ** 2
        for x, y in zip(agile_means, tracker_rates)
    )
    ss_tot = sum((y - mean_y) ** 2 for y in tracker_rates)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    model = CalibrationModel(
        slope=slope,
        intercept=intercept,
        r_squared=r_squared,
        calibrated_at=datetime.now(timezone.utc),
        sample_count=n,
    )

    _LOGGER.info(
        "Calibration complete: slope=%.4f, intercept=%.4f, R²=%.4f, samples=%d",
        slope,
        intercept,
        r_squared,
        n,
    )

    if r_squared < 0.80:
        _LOGGER.warning(
            "Model R² (%.4f) is below 0.80. The model may be unreliable. "
            "Check if your tariff version has changed.",
            r_squared,
        )

    return model


async def discover_product_code(
    session: ClientSession, prefix: str
) -> str | None:
    """Discover the latest product code matching a prefix (e.g. 'AGILE' or 'SILVER').

    Searches the Octopus products API for the most recent matching product.
    Paginates through results since the product listing can span multiple pages.
    """
    try:
        all_results: list[dict] = []
        url: str | None = OCTOPUS_PRODUCTS_URL
        params: dict = {"page_size": "100"}

        while url:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Failed to fetch Octopus products (status %d)", resp.status
                    )
                    break
                data = await resp.json()

            all_results.extend(data.get("results", []))

            # Check if we already have matches — no need to paginate further
            matches = [
                p for p in all_results if p.get("code", "").startswith(prefix)
            ]
            if matches:
                break

            url = data.get("next")
            params = {}  # next URL already includes params

        matches = [
            p for p in all_results if p.get("code", "").startswith(prefix)
        ]

        if not matches:
            _LOGGER.warning("No products found matching prefix '%s'", prefix)
            return None

        # Sort by available_from descending to get the latest
        matches.sort(
            key=lambda p: p.get("available_from", ""), reverse=True
        )
        code = matches[0]["code"]
        _LOGGER.info("Discovered product code: %s", code)
        return code

    except Exception:
        _LOGGER.exception("Error discovering product code for prefix '%s'", prefix)
        return None


async def fetch_octopus_rates(
    session: ClientSession,
    product_code: str,
    region: str,
    days: int,
) -> list[dict]:
    """Fetch historical unit rates from the Octopus API.

    Returns list of {"valid_from": str, "value_inc_vat": float} dicts.
    """
    tariff_code = f"E-1R-{product_code}-{region}"
    url = f"{OCTOPUS_API_BASE}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    period_from = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT00:00:00Z")

    all_results: list[dict] = []
    page_url: str | None = url
    params: dict = {"period_from": period_from, "page_size": "1500"}

    try:
        while page_url:
            async with session.get(page_url, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Octopus API returned status %d for %s",
                        resp.status,
                        product_code,
                    )
                    break
                data = await resp.json()

            all_results.extend(data.get("results", []))
            page_url = data.get("next")
            params = {}  # next URL already includes params

    except Exception:
        _LOGGER.exception("Error fetching Octopus rates for %s", product_code)

    return all_results


def compute_daily_means(rates: list[dict]) -> dict[str, float]:
    """Group half-hourly rates by date and compute daily means.

    Returns {date_str: mean_rate} dict.
    """
    daily: dict[str, list[float]] = {}
    for rate in rates:
        valid_from = rate.get("valid_from", "")
        value = rate.get("value_inc_vat")
        if not valid_from or value is None:
            continue
        date_str = valid_from[:10]  # YYYY-MM-DD
        daily.setdefault(date_str, []).append(float(value))

    return {date: statistics.mean(vals) for date, vals in daily.items() if vals}


async def calibrate(
    session: ClientSession,
    region: str,
    days: int,
    agile_product: str | None = None,
    tracker_product: str | None = None,
) -> CalibrationModel:
    """Run full calibration: discover products, fetch rates, fit model."""

    # Discover product codes if not provided
    if not agile_product:
        agile_product = await discover_product_code(session, "AGILE")
    if not tracker_product:
        for prefix in ("SILVER-FLEX", "SILVER-VAR", "SILVER-BB", "SILVER"):
            tracker_product = await discover_product_code(session, prefix)
            if tracker_product:
                break

    if not agile_product or not tracker_product:
        _LOGGER.warning(
            "Could not determine product codes (agile=%s, tracker=%s). "
            "Using default model.",
            agile_product,
            tracker_product,
        )
        return default_model()

    # Fetch historical rates
    agile_rates = await fetch_octopus_rates(session, agile_product, region, days)
    tracker_rates = await fetch_octopus_rates(session, tracker_product, region, days)

    if not agile_rates or not tracker_rates:
        _LOGGER.warning("No historical rate data available. Using default model.")
        return default_model()

    # Compute daily means
    agile_daily = compute_daily_means(agile_rates)
    tracker_daily = compute_daily_means(tracker_rates)

    # Pair up days that exist in both
    common_dates = sorted(set(agile_daily) & set(tracker_daily))
    if not common_dates:
        _LOGGER.warning("No overlapping dates between Agile and Tracker. Using default model.")
        return default_model()

    agile_means = [agile_daily[d] for d in common_dates]
    tracker_means = [tracker_daily[d] for d in common_dates]

    return fit_linear_model(agile_means, tracker_means)
