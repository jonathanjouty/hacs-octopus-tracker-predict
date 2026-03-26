"""Live API tests for Agile Predict and Octopus Energy APIs.

These tests call the real APIs to validate that:
1. Endpoints are reachable
2. Response structure matches what our code expects
3. Data can be processed by our transformation/calibration logic

Run with: pytest -m live_api
Skipped automatically when there is no network connectivity.
"""

import socket
from datetime import datetime, timezone

import aiohttp
import pytest
import pytest_asyncio

from custom_components.tracker_predict.calibration import (
    compute_daily_means,
    discover_product_code,
    fetch_octopus_rates,
)
from custom_components.tracker_predict.const import (
    AGILE_PREDICT_ALT_URL,
    AGILE_PREDICT_URL,
    OCTOPUS_PRODUCTS_URL,
)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _has_network() -> bool:
    """Check if we can resolve a public hostname."""
    try:
        socket.getaddrinfo("api.octopus.energy", 443)
        return True
    except (socket.gaierror, OSError):
        return False


# All tests in this file require network access
pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(not _has_network(), reason="No network connectivity"),
]


@pytest_asyncio.fixture
async def session():
    """Create an aiohttp session for tests."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        yield s


async def _discover_tracker_product(session) -> str | None:
    """Try multiple prefixes to find the Tracker product code.

    Octopus has used different product code prefixes for Tracker over time
    (SILVER-FLEX, SILVER-VAR, SILVER-BB, etc).
    """
    for prefix in ["SILVER-FLEX", "SILVER-VAR", "SILVER-BB", "SILVER"]:
        code = await discover_product_code(session, prefix)
        if code:
            return code
    return None


# ── Agile Predict API ─────────────────────────────────────────────────────


class TestAgilePredict:
    """Tests against the live Agile Predict API."""

    async def test_fetch_forecast_region_a(self, session):
        """Fetch a forecast for region A and validate structure."""
        url = AGILE_PREDICT_URL.format(region="A")
        params = {"days": "3", "high_low": "True", "forecast_count": "1"}

        async with session.get(url, params=params) as resp:
            assert resp.status == 200, f"Expected 200, got {resp.status}"
            data = await resp.json()

        # Top-level: list of forecast runs
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) >= 1, "Expected at least 1 forecast run"

        run = data[0]
        assert "name" in run, "Forecast run missing 'name'"
        assert "prices" in run, "Forecast run missing 'prices'"

        prices = run["prices"]
        assert isinstance(prices, list), "prices should be a list"
        assert len(prices) > 0, "prices should not be empty"

        # Validate individual price slot structure
        slot = prices[0]
        assert "date_time" in slot, "Price slot missing 'date_time'"
        assert "agile_pred" in slot, "Price slot missing 'agile_pred'"
        assert isinstance(slot["agile_pred"], (int, float)), "agile_pred should be numeric"

        # high_low=True should give us confidence bounds
        assert "agile_low" in slot, "Price slot missing 'agile_low' (high_low=True)"
        assert "agile_high" in slot, "Price slot missing 'agile_high' (high_low=True)"

        # Sanity check: agile_low <= agile_pred <= agile_high
        assert slot["agile_low"] <= slot["agile_pred"] <= slot["agile_high"], (
            f"Expected low <= pred <= high, got "
            f"{slot['agile_low']} <= {slot['agile_pred']} <= {slot['agile_high']}"
        )

        # date_time should be parseable ISO format
        dt = datetime.fromisoformat(slot["date_time"].replace("Z", "+00:00"))
        assert dt.tzinfo is not None or "Z" in slot["date_time"]

    async def test_forecast_has_multiple_days(self, session):
        """A 7-day forecast should span multiple dates."""
        url = AGILE_PREDICT_URL.format(region="A")
        params = {"days": "7", "high_low": "True", "forecast_count": "1"}

        async with session.get(url, params=params) as resp:
            assert resp.status == 200
            data = await resp.json()

        prices = data[0]["prices"]
        dates = {p["date_time"][:10] for p in prices}
        assert len(dates) >= 2, f"Expected multiple dates in 7-day forecast, got {dates}"

    async def test_forecast_slots_are_half_hourly(self, session):
        """Slots should be at 30-minute intervals."""
        url = AGILE_PREDICT_URL.format(region="A")
        params = {"days": "2", "high_low": "False", "forecast_count": "1"}

        async with session.get(url, params=params) as resp:
            assert resp.status == 200
            data = await resp.json()

        prices = data[0]["prices"]
        if len(prices) >= 2:
            dt0 = datetime.fromisoformat(prices[0]["date_time"].replace("Z", "+00:00"))
            dt1 = datetime.fromisoformat(prices[1]["date_time"].replace("Z", "+00:00"))
            diff_minutes = (dt1 - dt0).total_seconds() / 60
            assert diff_minutes == 30, f"Expected 30-minute intervals, got {diff_minutes}"

    async def test_alt_endpoint_also_works(self, session):
        """The Fly.io mirror should return the same structure."""
        url = AGILE_PREDICT_ALT_URL.format(region="A")
        params = {"days": "1", "forecast_count": "1"}

        async with session.get(url, params=params) as resp:
            assert resp.status == 200
            data = await resp.json()

        assert isinstance(data, list)
        assert len(data) >= 1
        assert "prices" in data[0]
        assert len(data[0]["prices"]) > 0

    async def test_all_regions_return_data(self, session):
        """Spot-check a few regions to verify they all work."""
        for region in ["A", "C", "H", "P"]:
            url = AGILE_PREDICT_URL.format(region=region)
            params = {"days": "1", "forecast_count": "1"}

            async with session.get(url, params=params) as resp:
                assert resp.status == 200, f"Region {region} returned {resp.status}"
                data = await resp.json()

            assert len(data) >= 1, f"Region {region} returned empty data"
            assert len(data[0]["prices"]) > 0, f"Region {region} has no prices"


# ── Octopus Energy API ────────────────────────────────────────────────────


class TestOctopusProducts:
    """Tests against the Octopus Energy products API."""

    async def test_products_endpoint_reachable(self, session):
        """The products listing endpoint should return paginated results."""
        async with session.get(
            OCTOPUS_PRODUCTS_URL, params={"page_size": "10"}
        ) as resp:
            assert resp.status == 200
            data = await resp.json()

        assert "results" in data, "Products response missing 'results'"
        assert "count" in data, "Products response missing 'count'"
        assert isinstance(data["results"], list)
        assert len(data["results"]) > 0

        product = data["results"][0]
        assert "code" in product, "Product missing 'code'"
        assert "display_name" in product or "full_name" in product

    async def test_discover_agile_product(self, session):
        """Should find a current AGILE product code."""
        code = await discover_product_code(session, "AGILE")
        assert code is not None, "Failed to discover any AGILE product"
        assert code.startswith("AGILE"), f"Expected AGILE prefix, got {code}"

    async def test_discover_tracker_product(self, session):
        """Should find a current Tracker product code."""
        code = await _discover_tracker_product(session)
        assert code is not None, "Failed to discover any Tracker product (tried SILVER-FLEX, SILVER-VAR, SILVER-BB, SILVER)"
        assert "SILVER" in code or "TRACK" in code.upper(), f"Unexpected product code: {code}"


class TestOctopusRates:
    """Tests against the Octopus Energy tariff rates API."""

    async def test_fetch_agile_rates(self, session):
        """Fetch recent Agile rates and validate structure."""
        product = await discover_product_code(session, "AGILE")
        assert product is not None, "Could not discover AGILE product"

        rates = await fetch_octopus_rates(session, product, "A", days=7)
        assert len(rates) > 0, "No Agile rates returned"

        rate = rates[0]
        assert "valid_from" in rate, "Rate missing 'valid_from'"
        assert "value_inc_vat" in rate, "Rate missing 'value_inc_vat'"
        assert isinstance(rate["value_inc_vat"], (int, float)), "value_inc_vat should be numeric"

        # Should have roughly 48 slots per day × 7 days = 336, allow some slack
        assert len(rates) > 100, f"Expected 100+ rates for 7 days, got {len(rates)}"

    async def test_fetch_tracker_rates(self, session):
        """Fetch recent Tracker rates and validate structure."""
        product = await _discover_tracker_product(session)
        assert product is not None, "Could not discover Tracker product"

        rates = await fetch_octopus_rates(session, product, "A", days=7)
        assert len(rates) > 0, "No Tracker rates returned"

        rate = rates[0]
        assert "valid_from" in rate
        assert "value_inc_vat" in rate
        assert isinstance(rate["value_inc_vat"], (int, float))

    async def test_agile_rates_compute_daily_means(self, session):
        """Agile rates should produce valid daily means."""
        product = await discover_product_code(session, "AGILE")
        assert product is not None

        rates = await fetch_octopus_rates(session, product, "A", days=7)
        daily = compute_daily_means(rates)

        assert len(daily) >= 1, "Should have at least 1 day of means"

        for date_str, mean in daily.items():
            # Date format check
            datetime.strptime(date_str, "%Y-%m-%d")
            # Rates should be reasonable (negative is possible for Agile but bounded)
            assert -50 < mean < 200, f"Mean {mean} for {date_str} seems out of range"

    async def test_tracker_rates_are_daily(self, session):
        """Tracker rates should have ~1 rate per day (not half-hourly)."""
        product = await _discover_tracker_product(session)
        assert product is not None, "Could not discover Tracker product"

        rates = await fetch_octopus_rates(session, product, "A", days=7)
        daily = compute_daily_means(rates)

        # Tracker has 1 rate per day, so daily means should be ~7
        assert len(daily) >= 3, f"Expected several days, got {len(daily)}"

        for date_str, mean in daily.items():
            assert 0 < mean < 100, f"Tracker mean {mean} for {date_str} out of expected range"


# ── End-to-end: Agile Predict → Tracker estimate ─────────────────────────


class TestEndToEnd:
    """Validate the full pipeline from live API data."""

    async def test_agile_predict_to_tracker_estimate(self, session):
        """Fetch a live forecast and run it through the transformation."""
        from custom_components.tracker_predict.calibration import CalibrationModel

        # Use default model (known-good from 2025 data)
        model = CalibrationModel(
            slope=0.56,
            intercept=12.75,
            r_squared=0.90,
            calibrated_at=datetime.now(timezone.utc),
            sample_count=60,
        )

        # Fetch live forecast
        url = AGILE_PREDICT_URL.format(region="A")
        params = {"days": "7", "high_low": "True", "forecast_count": "1"}

        async with session.get(url, params=params) as resp:
            assert resp.status == 200
            data = await resp.json()

        prices = data[0]["prices"]

        # Group by date and compute daily means
        daily: dict[str, list[float]] = {}
        for slot in prices:
            date = slot["date_time"][:10]
            daily.setdefault(date, []).append(slot["agile_pred"])

        # Transform each day
        estimates = {}
        for date, agile_preds in sorted(daily.items()):
            agile_mean = sum(agile_preds) / len(agile_preds)
            tracker_est = model.predict(agile_mean)
            estimates[date] = tracker_est

        assert len(estimates) >= 2, "Should have estimates for multiple days"

        for date, est in estimates.items():
            assert 0 <= est <= 100, f"Estimate {est} for {date} out of [0,100] range"

        # Verify we can identify cheapest day
        cheapest = min(estimates, key=estimates.get)
        assert cheapest in estimates
