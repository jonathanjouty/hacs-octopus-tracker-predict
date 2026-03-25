"""Tests for the coordinator transformation logic."""

from datetime import datetime, timezone

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
