"""Calibration module: fetches historical Agile/Tracker rates and fits a linear model."""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession

from .const import (
    DEFAULT_AGILE_PRODUCT,
    DEFAULT_CALIBRATION,
    DEFAULT_INTERCEPT,
    DEFAULT_ROLLING_WINDOW,
    DEFAULT_SLOPE,
    DEFAULT_TRACKER_PRODUCT,
    KNOWN_TRACKER_PRODUCTS,
    OCTOPUS_API_BASE,
    OCTOPUS_PRODUCTS_URL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CalibrationModel:
    """Linear model: tracker_est = slope * rolling_mean_agile + intercept.

    The input feature is a trailing rolling mean of Agile daily means rather
    than the single-day spot mean, because Octopus Tracker rates are set from
    a rolling average of wholesale prices. Using a rolling mean reduces the
    systematic bias at price extremes (over-prediction on high days, under-
    prediction on low days).
    """

    slope: float
    intercept: float
    r_squared: float
    calibrated_at: datetime
    sample_count: int
    rolling_window: int = DEFAULT_ROLLING_WINDOW

    def predict(self, agile_rolling_mean: float) -> float:
        """Predict Tracker rate from rolling Agile mean, clamped to [0, 100]."""
        return max(0.0, min(100.0, self.slope * agile_rolling_mean + self.intercept))


def default_model(region: str = "A") -> CalibrationModel:
    """Return fallback model using per-region defaults."""
    slope, intercept = DEFAULT_CALIBRATION.get(region, (DEFAULT_SLOPE, DEFAULT_INTERCEPT))
    return CalibrationModel(
        slope=slope,
        intercept=intercept,
        r_squared=0.0,
        calibrated_at=datetime.now(timezone.utc),
        sample_count=0,
        rolling_window=DEFAULT_ROLLING_WINDOW,
    )


def fit_linear_model(
    agile_means: list[float],
    tracker_rates: list[float],
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
) -> CalibrationModel:
    """Fit a simple linear regression from paired daily values.

    agile_means should already be rolling means (computed by compute_rolling_means)
    so the model input matches the Tracker formula's smoothing structure.

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
        rolling_window=rolling_window,
    )

    _LOGGER.info(
        "Calibration complete: slope=%.4f, intercept=%.4f, R²=%.4f, "
        "samples=%d, rolling_window=%d",
        slope,
        intercept,
        r_squared,
        n,
        rolling_window,
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

    For SILVER (Tracker) products, probes KNOWN_TRACKER_PRODUCTS directly
    because Octopus deliberately excludes Tracker products from the listing API.

    For other prefixes, searches the Octopus products listing API.
    """
    if prefix == "SILVER":
        return await _discover_tracker_product_code(session)

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


async def _discover_tracker_product_code(session: ClientSession) -> str | None:
    """Find the active Tracker product by probing KNOWN_TRACKER_PRODUCTS directly.

    Iterates newest-first and returns the first code whose available_to is null
    (i.e. still active). If none are active (a newer product exists beyond our
    list), returns the newest code that at least responds with HTTP 200 so
    calibration can still use recent historical data.
    """
    first_existing: str | None = None
    for code in KNOWN_TRACKER_PRODUCTS:
        try:
            url = f"{OCTOPUS_API_BASE}/products/{code}/"
            async with session.get(url) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
            if first_existing is None:
                first_existing = code
            if data.get("available_to") is None:
                _LOGGER.info("Discovered active Tracker product: %s", code)
                return code
        except Exception:
            _LOGGER.debug(
                "Error probing Tracker product %s", code, exc_info=True
            )

    if first_existing:
        _LOGGER.warning(
            "No Tracker product in KNOWN_TRACKER_PRODUCTS has available_to=null. "
            "A newer product code may exist — update KNOWN_TRACKER_PRODUCTS in const.py. "
            "Using most recent known code: %s",
            first_existing,
        )
    return first_existing


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


def compute_rolling_means(daily_means: dict[str, float], window: int) -> dict[str, float]:
    """Compute a trailing N-day mean for each date in the series.

    For each date D, the rolling mean is the arithmetic mean of the window
    ending at D (inclusive), or fewer days if the series starts later.

    Returns {date_str: rolling_mean} with the same keys as daily_means.
    """
    sorted_dates = sorted(daily_means)
    result: dict[str, float] = {}
    for i, date in enumerate(sorted_dates):
        start = max(0, i - window + 1)
        window_vals = [daily_means[d] for d in sorted_dates[start : i + 1]]
        result[date] = statistics.mean(window_vals)
    return result


async def calibrate(
    session: ClientSession,
    region: str,
    days: int,
    agile_product: str | None = None,
    tracker_product: str | None = None,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
) -> CalibrationModel:
    """Run full calibration: discover products, fetch rates, fit model.

    Uses a rolling mean of Agile daily prices as the input feature, which
    better matches how Octopus sets the Tracker rate.
    """

    # Discover product codes if not provided
    if not agile_product:
        agile_product = await discover_product_code(session, "AGILE")
        if not agile_product:
            agile_product = DEFAULT_AGILE_PRODUCT
            _LOGGER.info("Using default Agile product code: %s", agile_product)
    if not tracker_product:
        tracker_product = await discover_product_code(session, "SILVER")
        if not tracker_product:
            tracker_product = DEFAULT_TRACKER_PRODUCT
            _LOGGER.info("Using default Tracker product code: %s", tracker_product)

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

    # Compute daily means then rolling means for Agile
    agile_daily = compute_daily_means(agile_rates)
    tracker_daily = compute_daily_means(tracker_rates)
    agile_rolling = compute_rolling_means(agile_daily, rolling_window)

    # Pair up days that exist in both (use rolling means for Agile)
    common_dates = sorted(set(agile_rolling) & set(tracker_daily))
    if not common_dates:
        _LOGGER.warning("No overlapping dates between Agile and Tracker. Using default model.")
        return default_model()

    agile_means = [agile_rolling[d] for d in common_dates]
    tracker_means = [tracker_daily[d] for d in common_dates]

    return fit_linear_model(agile_means, tracker_means, rolling_window=rolling_window)
