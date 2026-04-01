"""Tests for the coordinator transformation logic."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.tracker_predict.calibration import CalibrationModel
from custom_components.tracker_predict.coordinator import (
    DayForecast,
    TrackerPredictCoordinator,
    TrackerPredictData,
)


def make_model(slope=0.56, intercept=12.75):
    return CalibrationModel(
        slope=slope, intercept=intercept, r_squared=0.90,
        calibrated_at=datetime(2026, 3, 25, tzinfo=timezone.utc),
        sample_count=60,
    )


def make_prices(day_configs):
    """Generate half-hourly prices.

    day_configs: list of (date_str, num_slots, base_price, price_step)
    """
    prices = []
    for date_str, num_slots, base, step in day_configs:
        for i in range(num_slots):
            hour = i // 2
            minute = "00" if i % 2 == 0 else "30"
            prices.append({
                "date_time": f"{date_str}T{hour:02d}:{minute}:00Z",
                "agile_pred": base + i * step,
                "agile_low": base - 2 + i * step,
                "agile_high": base + 2 + i * step,
            })
    return prices


class FakeCoordinator:
    """Minimal stand-in with the _model and _transform_forecast method."""

    def __init__(self, model=None):
        self._model = model or make_model()

    _transform_forecast = TrackerPredictCoordinator._transform_forecast
    _overlay_actual_rates = TrackerPredictCoordinator._overlay_actual_rates


class TestTransformForecast:
    def test_basic_three_days(self):
        coord = FakeCoordinator()
        prices = make_prices([
            ("2026-03-25", 16, 30.0, 0.5),
            ("2026-03-26", 48, 15.0, 0.2),
            ("2026-03-27", 48, 40.0, 0.3),
        ])
        forecasts = coord._transform_forecast(prices)
        assert len(forecasts) == 3
        dates = [f.date for f in forecasts]
        assert "2026-03-25" in dates
        assert "2026-03-26" in dates
        assert "2026-03-27" in dates

    def test_cheapest_day(self):
        coord = FakeCoordinator()
        prices = make_prices([
            ("2026-03-26", 48, 15.0, 0.2),
            ("2026-03-27", 48, 40.0, 0.3),
        ])
        forecasts = coord._transform_forecast(prices)
        cheapest = min(forecasts, key=lambda f: f.tracker_est)
        assert cheapest.date == "2026-03-26"

    def test_expensive_day(self):
        coord = FakeCoordinator()
        prices = make_prices([
            ("2026-03-26", 48, 15.0, 0.2),
            ("2026-03-27", 48, 40.0, 0.3),
        ])
        forecasts = coord._transform_forecast(prices)
        most_expensive = max(forecasts, key=lambda f: f.tracker_est)
        assert most_expensive.date == "2026-03-27"

    def test_partial_day_slot_count(self):
        coord = FakeCoordinator()
        prices = make_prices([("2026-03-25", 10, 30.0, 0.5)])
        forecasts = coord._transform_forecast(prices)
        assert len(forecasts) == 1
        assert forecasts[0].slot_count == 10

    def test_clamped_range(self):
        coord = FakeCoordinator()
        prices = make_prices([
            ("2026-03-26", 48, 15.0, 0.2),
            ("2026-03-27", 48, 40.0, 0.3),
        ])
        forecasts = coord._transform_forecast(prices)
        for f in forecasts:
            assert 0 <= f.tracker_est <= 100
            assert 0 <= f.tracker_low <= 100
            assert 0 <= f.tracker_high <= 100

    def test_low_high_ordering(self):
        coord = FakeCoordinator()
        prices = make_prices([("2026-03-26", 48, 20.0, 0.3)])
        forecasts = coord._transform_forecast(prices)
        for f in forecasts:
            assert f.tracker_low <= f.tracker_est <= f.tracker_high

    def test_empty_prices(self):
        coord = FakeCoordinator()
        assert coord._transform_forecast([]) == []

    def test_single_slot(self):
        coord = FakeCoordinator()
        prices = [{
            "date_time": "2026-03-26T12:00:00Z",
            "agile_pred": 25.0,
            "agile_low": 23.0,
            "agile_high": 27.0,
        }]
        forecasts = coord._transform_forecast(prices)
        assert len(forecasts) == 1
        assert forecasts[0].slot_count == 1

    def test_all_same_price(self):
        coord = FakeCoordinator()
        prices = make_prices([("2026-03-26", 48, 25.0, 0.0)])
        forecasts = coord._transform_forecast(prices)
        assert len(forecasts) == 1
        assert forecasts[0].agile_daily_mean == 25.0

    def test_day_of_week(self):
        coord = FakeCoordinator()
        # 2026-03-26 is a Thursday
        prices = make_prices([("2026-03-26", 48, 20.0, 0.1)])
        forecasts = coord._transform_forecast(prices)
        assert forecasts[0].day_of_week == "Thu"


class TestRankedForecasts:
    def test_ranking(self):
        from custom_components.tracker_predict.sensor import _ranked_forecasts

        data = TrackerPredictData(
            forecasts=[
                DayForecast(date="2026-03-25", tracker_est=25.0, tracker_low=23.0,
                            tracker_high=27.0, confidence="high", day_of_week="Wed",
                            agile_daily_mean=22.0, slot_count=48),
                DayForecast(date="2026-03-26", tracker_est=18.0, tracker_low=16.0,
                            tracker_high=20.0, confidence="high", day_of_week="Thu",
                            agile_daily_mean=10.0, slot_count=48),
                DayForecast(date="2026-03-27", tracker_est=30.0, tracker_low=28.0,
                            tracker_high=32.0, confidence="medium", day_of_week="Fri",
                            agile_daily_mean=31.0, slot_count=48),
            ],
            model=make_model(),
        )
        ranked = _ranked_forecasts(data)
        assert len(ranked) == 3
        assert next(r for r in ranked if r["date"] == "2026-03-26")["rank"] == 1
        assert next(r for r in ranked if r["date"] == "2026-03-25")["rank"] == 2
        assert next(r for r in ranked if r["date"] == "2026-03-27")["rank"] == 3

    def test_empty_data(self):
        from custom_components.tracker_predict.sensor import _ranked_forecasts
        assert _ranked_forecasts(None) == []
        assert _ranked_forecasts(TrackerPredictData()) == []


class TestPartialTodayFiltering:
    """Tests for excluding today when it has too few slots."""

    def _today_str(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _tomorrow_str(self):
        return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    def test_partial_today_excluded(self):
        """Today with fewer than 48 slots is excluded from forecasts."""
        coord = FakeCoordinator()
        today = self._today_str()
        tomorrow = self._tomorrow_str()
        prices = make_prices([
            (today, 10, 5.0, 0.1),       # partial today — cheap evening slots
            (tomorrow, 48, 25.0, 0.2),    # full tomorrow
        ])
        forecasts = coord._transform_forecast(prices)
        dates = [f.date for f in forecasts]
        assert today not in dates
        assert tomorrow in dates
        assert len(forecasts) == 1

    def test_full_today_included(self):
        """Today with a full 48 slots is kept in forecasts."""
        coord = FakeCoordinator()
        today = self._today_str()
        tomorrow = self._tomorrow_str()
        prices = make_prices([
            (today, 48, 20.0, 0.1),
            (tomorrow, 48, 25.0, 0.2),
        ])
        forecasts = coord._transform_forecast(prices)
        dates = [f.date for f in forecasts]
        assert today in dates
        assert tomorrow in dates
        assert len(forecasts) == 2

    def test_partial_future_day_not_excluded(self):
        """A future day with fewer than 48 slots is NOT filtered out."""
        coord = FakeCoordinator()
        tomorrow = self._tomorrow_str()
        day_after = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        prices = make_prices([
            (tomorrow, 48, 25.0, 0.2),
            (day_after, 10, 30.0, 0.3),   # partial future day
        ])
        forecasts = coord._transform_forecast(prices)
        dates = [f.date for f in forecasts]
        assert day_after in dates
        assert len(forecasts) == 2


class TestOverlayActualRates:
    """Tests for overlaying actual Tracker rates onto forecasts."""

    def _today_str(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _tomorrow_str(self):
        return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    def _yesterday_str(self):
        return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def _make_forecast(self, date_str, est=20.0, confidence="high"):
        return DayForecast(
            date=date_str, tracker_est=est, tracker_low=est - 2,
            tracker_high=est + 2, confidence=confidence,
            day_of_week="Mon", agile_daily_mean=15.0, slot_count=48,
        )

    def test_actual_replaces_prediction(self):
        """Actual rate overwrites predicted values and sets confidence='actual'."""
        coord = FakeCoordinator()
        today = self._today_str()
        forecasts = [self._make_forecast(today, est=20.0)]
        actual_rates = {today: 24.5}

        result = coord._overlay_actual_rates(forecasts, actual_rates)
        assert len(result) == 1
        assert result[0].tracker_est == 24.5
        assert result[0].tracker_low == 24.5
        assert result[0].tracker_high == 24.5
        assert result[0].confidence == "actual"

    def test_actual_inserts_filtered_today(self):
        """Today excluded by MIN_TODAY_SLOTS is re-inserted with actual rate."""
        coord = FakeCoordinator()
        today = self._today_str()
        tomorrow = self._tomorrow_str()
        # Forecasts only have tomorrow (today was filtered out)
        forecasts = [self._make_forecast(tomorrow, est=22.0)]
        actual_rates = {today: 19.8}

        result = coord._overlay_actual_rates(forecasts, actual_rates)
        dates = [f.date for f in result]
        assert today in dates
        assert len(result) == 2
        today_forecast = next(f for f in result if f.date == today)
        assert today_forecast.tracker_est == 19.8
        assert today_forecast.confidence == "actual"
        assert today_forecast.slot_count == 0  # inserted, not from Agile Predict

    def test_actual_tomorrow(self):
        """Tomorrow's actual rate replaces its prediction."""
        coord = FakeCoordinator()
        tomorrow = self._tomorrow_str()
        forecasts = [self._make_forecast(tomorrow, est=25.0)]
        actual_rates = {tomorrow: 21.3}

        result = coord._overlay_actual_rates(forecasts, actual_rates)
        assert result[0].tracker_est == 21.3
        assert result[0].confidence == "actual"

    def test_actual_ignores_past_dates(self):
        """Yesterday's rate from API is not inserted."""
        coord = FakeCoordinator()
        yesterday = self._yesterday_str()
        today = self._today_str()
        forecasts = [self._make_forecast(today, est=20.0)]
        actual_rates = {yesterday: 18.0, today: 22.0}

        result = coord._overlay_actual_rates(forecasts, actual_rates)
        dates = [f.date for f in result]
        assert yesterday not in dates
        assert len(result) == 1

    def test_actual_empty_no_change(self):
        """Empty actual rates dict leaves forecasts unchanged."""
        coord = FakeCoordinator()
        today = self._today_str()
        forecasts = [self._make_forecast(today, est=20.0)]

        result = coord._overlay_actual_rates(forecasts, {})
        assert len(result) == 1
        assert result[0].tracker_est == 20.0
        assert result[0].confidence == "high"

    def test_overlay_preserves_sort_order(self):
        """Output is sorted by date after overlay."""
        coord = FakeCoordinator()
        today = self._today_str()
        tomorrow = self._tomorrow_str()
        day_after = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        # Forecasts have tomorrow and day_after, today was filtered
        forecasts = [
            self._make_forecast(day_after, est=30.0),
            self._make_forecast(tomorrow, est=25.0),
        ]
        actual_rates = {today: 19.0}

        result = coord._overlay_actual_rates(forecasts, actual_rates)
        dates = [f.date for f in result]
        assert dates == sorted(dates)


class FakeCoordinatorForFetch:
    """Stand-in with attributes needed by _fetch_actual_tracker_rates."""

    def __init__(self, tracker_product=None, resolved_product=None):
        self._tracker_product = tracker_product
        self._resolved_tracker_product = resolved_product
        self._region = "A"
        self.session = AsyncMock()

    _fetch_actual_tracker_rates = TrackerPredictCoordinator._fetch_actual_tracker_rates


class TestFetchActualTrackerRates:
    """Tests for _fetch_actual_tracker_rates parsing logic."""

    async def test_happy_path_parses_rates(self):
        """Rates are parsed into a date→value dict, first value per date wins."""
        coord = FakeCoordinatorForFetch(tracker_product="SILVER-25-09-02")
        mock_rates = [
            {"valid_from": "2026-04-01T00:00:00Z", "value_inc_vat": 24.5},
            {"valid_from": "2026-04-01T12:00:00Z", "value_inc_vat": 25.0},
            {"valid_from": "2026-03-31T00:00:00Z", "value_inc_vat": 22.1},
        ]
        with patch(
            "custom_components.tracker_predict.coordinator.fetch_octopus_rates",
            new_callable=AsyncMock,
            return_value=mock_rates,
        ):
            result = await coord._fetch_actual_tracker_rates()
        assert result == {"2026-04-01": 24.5, "2026-03-31": 22.1}

    async def test_skips_entries_with_missing_fields(self):
        """Entries without valid_from or value_inc_vat are skipped."""
        coord = FakeCoordinatorForFetch(tracker_product="SILVER-25-09-02")
        mock_rates = [
            {"valid_from": "2026-04-01T00:00:00Z", "value_inc_vat": 24.5},
            {"valid_from": "", "value_inc_vat": 20.0},
            {"valid_from": "2026-03-31T00:00:00Z"},  # missing value_inc_vat
            {"value_inc_vat": 19.0},  # missing valid_from
        ]
        with patch(
            "custom_components.tracker_predict.coordinator.fetch_octopus_rates",
            new_callable=AsyncMock,
            return_value=mock_rates,
        ):
            result = await coord._fetch_actual_tracker_rates()
        assert result == {"2026-04-01": 24.5}

    async def test_error_returns_empty_dict(self):
        """API failure returns empty dict (non-fatal)."""
        coord = FakeCoordinatorForFetch(tracker_product="SILVER-25-09-02")
        with patch(
            "custom_components.tracker_predict.coordinator.fetch_octopus_rates",
            new_callable=AsyncMock,
            side_effect=Exception("network error"),
        ):
            result = await coord._fetch_actual_tracker_rates()
        assert result == {}

    async def test_uses_resolved_product_when_no_configured(self):
        """Falls back to _resolved_tracker_product from discovery."""
        coord = FakeCoordinatorForFetch(
            tracker_product=None, resolved_product="SILVER-25-09-02"
        )
        with patch(
            "custom_components.tracker_predict.coordinator.fetch_octopus_rates",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_fetch:
            await coord._fetch_actual_tracker_rates()
        assert mock_fetch.call_args[0][1] == "SILVER-25-09-02"
