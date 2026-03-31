"""Tests for the calendar platform."""

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.tracker_predict.calendar import (
    TrackerPredictCalendar,
    _build_event,
    _events_from_data,
    _rank_label,
)
from custom_components.tracker_predict.coordinator import DayForecast, TrackerPredictData
from custom_components.tracker_predict.calibration import CalibrationModel


def make_model():
    return CalibrationModel(
        slope=0.56, intercept=12.75, r_squared=0.90,
        calibrated_at=datetime(2026, 3, 25, tzinfo=timezone.utc),
        sample_count=60,
    )


def make_forecast(date_str, tracker_est, tracker_low=None, tracker_high=None,
                  confidence="high", day_of_week="Mon"):
    return DayForecast(
        date=date_str,
        tracker_est=tracker_est,
        tracker_low=tracker_low if tracker_low is not None else tracker_est - 3,
        tracker_high=tracker_high if tracker_high is not None else tracker_est + 3,
        confidence=confidence,
        day_of_week=day_of_week,
        agile_daily_mean=tracker_est / 0.56,
        slot_count=48,
    )


def make_data(forecasts):
    return TrackerPredictData(
        forecasts=forecasts,
        model=make_model(),
        last_updated=datetime(2026, 3, 25, tzinfo=timezone.utc),
        stale=False,
    )


def make_calendar(data):
    coord = MagicMock()
    coord.data = data
    entry = MagicMock()
    cal = TrackerPredictCalendar.__new__(TrackerPredictCalendar)
    cal.coordinator = coord
    cal._region = "A"
    cal._attr_unique_id = "tracker_predict_A_calendar"
    cal._attr_name = "Tracker Predict (A)"
    return cal


# --- _rank_label ---

def test_rank_label_cheapest():
    assert _rank_label(1, 5) == "cheapest"

def test_rank_label_second():
    assert _rank_label(2, 5) == "2nd cheapest"

def test_rank_label_third():
    assert _rank_label(3, 5) == "3rd cheapest"

def test_rank_label_other():
    assert _rank_label(4, 10) == "#4 of 10"


# --- _build_event ---

def test_build_event_cheapest():
    f = make_forecast("2026-03-26", 20.0)
    event = _build_event(f, rank=1, total=3)
    assert event.summary == "Tracker: 20.0p/kWh (cheapest)"
    assert event.start == date(2026, 3, 26)
    assert event.end == date(2026, 3, 26)
    assert "Range:" in event.description
    assert "Rank: 1 of 3" in event.description

def test_build_event_rank3():
    f = make_forecast("2026-03-28", 45.0, confidence="low")
    event = _build_event(f, rank=3, total=5)
    assert "3rd cheapest" in event.summary
    assert "Confidence: low" in event.description


# --- _events_from_data ---

def test_events_from_data_empty():
    assert _events_from_data(make_data([])) == []

def test_events_from_data_ranking():
    """Cheapest day should get rank 1 regardless of order in forecast list."""
    forecasts = [
        make_forecast("2026-03-25", 35.0, day_of_week="Wed"),  # expensive → rank 3
        make_forecast("2026-03-26", 20.0, day_of_week="Thu"),  # cheapest → rank 1
        make_forecast("2026-03-27", 28.0, day_of_week="Fri"),  # middle → rank 2
    ]
    data = make_data(forecasts)
    events = _events_from_data(data)

    assert len(events) == 3
    # Events are in forecast order; find the cheapest (2026-03-26)
    event_map = {d: e for e, d in events}
    assert "cheapest" in event_map["2026-03-26"].summary
    assert "2nd cheapest" in event_map["2026-03-27"].summary
    assert "3rd cheapest" in event_map["2026-03-25"].summary

def test_events_from_data_none():
    assert _events_from_data(None) == []


# --- TrackerPredictCalendar.event ---

def test_calendar_event_returns_today(monkeypatch):
    forecasts = [
        make_forecast("2026-03-31", 25.0, day_of_week="Tue"),
        make_forecast("2026-04-01", 30.0, day_of_week="Wed"),
    ]
    cal = make_calendar(make_data(forecasts))
    # Today is 2026-03-31 in test context
    monkeypatch.setattr(
        "custom_components.tracker_predict.calendar.datetime",
        type("dt", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 3, 31, tzinfo=timezone.utc)),
            "strptime": datetime.strptime,
        })
    )
    event = cal.event
    assert event is not None
    assert "2026-03-31" in str(event.start) or event.start == date(2026, 3, 31)

def test_calendar_event_none_when_no_data():
    cal = make_calendar(make_data([]))
    assert cal.event is None


# --- TrackerPredictCalendar.async_get_events ---

def test_async_get_events_filters_by_range():
    forecasts = [
        make_forecast("2026-03-25", 35.0),
        make_forecast("2026-03-26", 20.0),
        make_forecast("2026-03-27", 28.0),
    ]
    cal = make_calendar(make_data(forecasts))
    start = datetime(2026, 3, 26, tzinfo=timezone.utc)
    end = datetime(2026, 3, 27, tzinfo=timezone.utc)  # exclusive
    events = asyncio.run(cal.async_get_events(MagicMock(), start, end))
    assert len(events) == 1
    assert events[0].start == date(2026, 3, 26)

def test_async_get_events_empty_when_no_data():
    cal = make_calendar(make_data([]))
    events = asyncio.run(cal.async_get_events(
        MagicMock(),
        datetime(2026, 3, 25, tzinfo=timezone.utc),
        datetime(2026, 3, 30, tzinfo=timezone.utc),
    ))
    assert events == []

def test_async_get_events_all_in_range():
    forecasts = [
        make_forecast("2026-03-25", 35.0),
        make_forecast("2026-03-26", 20.0),
        make_forecast("2026-03-27", 28.0),
    ]
    cal = make_calendar(make_data(forecasts))
    start = datetime(2026, 3, 25, tzinfo=timezone.utc)
    end = datetime(2026, 3, 28, tzinfo=timezone.utc)
    events = asyncio.run(cal.async_get_events(MagicMock(), start, end))
    assert len(events) == 3
