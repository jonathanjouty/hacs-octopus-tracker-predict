"""Tests for calibration model persistence."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.tracker_predict.calibration import CalibrationModel, default_model
from custom_components.tracker_predict.coordinator import (
    TrackerPredictCoordinator,
)

# Import the fake Store from conftest (loaded via sys.modules patching)
from homeassistant.helpers.storage import Store as FakeStore


def make_model(slope=0.60, intercept=11.0, r_squared=0.92, sample_count=45):
    return CalibrationModel(
        slope=slope,
        intercept=intercept,
        r_squared=r_squared,
        calibrated_at=datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc),
        sample_count=sample_count,
    )


def make_stored_data(model=None):
    """Create stored data dict from a CalibrationModel."""
    m = model or make_model()
    return {
        "slope": m.slope,
        "intercept": m.intercept,
        "r_squared": m.r_squared,
        "calibrated_at": m.calibrated_at.isoformat(),
        "sample_count": m.sample_count,
    }


class FakeCoordinator:
    """Minimal stand-in with persistence methods from TrackerPredictCoordinator."""

    def __init__(self, store=None):
        self._model = default_model()
        self._last_calibration = None
        self._store = store or FakeStore()
        self._calibration_interval = 168  # 7 days in hours

    _async_load_cached_model = TrackerPredictCoordinator._async_load_cached_model
    _async_save_model = TrackerPredictCoordinator._async_save_model


class TestLoadCachedModel:
    async def test_loads_valid_cached_model(self):
        store = FakeStore()
        model = make_model()
        await store.async_save(make_stored_data(model))

        coord = FakeCoordinator(store=store)
        result = await coord._async_load_cached_model()

        assert result is True
        assert coord._model.slope == model.slope
        assert coord._model.intercept == model.intercept
        assert coord._model.r_squared == model.r_squared
        assert coord._model.sample_count == model.sample_count
        assert coord._model.calibrated_at == model.calibrated_at
        assert coord._last_calibration == model.calibrated_at

    async def test_returns_false_when_no_cached_data(self):
        coord = FakeCoordinator()
        result = await coord._async_load_cached_model()

        assert result is False
        # Should still have the default model
        assert coord._model.slope == 0.56
        assert coord._last_calibration is None

    async def test_returns_false_for_invalid_data(self):
        store = FakeStore()
        await store.async_save({"bad": "data"})

        coord = FakeCoordinator(store=store)
        result = await coord._async_load_cached_model()

        assert result is False
        assert coord._model.slope == 0.56  # default

    async def test_returns_false_for_non_numeric_values(self):
        store = FakeStore()
        await store.async_save({
            "slope": "banana",
            "intercept": 11.0,
            "r_squared": 0.92,
            "calibrated_at": "2026-03-20T12:00:00+00:00",
            "sample_count": 45,
        })

        coord = FakeCoordinator(store=store)
        result = await coord._async_load_cached_model()

        assert result is False
        assert coord._model.slope == 0.56  # default

    async def test_coerces_string_numbers(self):
        """Values stored as strings (e.g. from manual edits) are coerced."""
        store = FakeStore()
        await store.async_save({
            "slope": "0.60",
            "intercept": "11.0",
            "r_squared": "0.92",
            "calibrated_at": "2026-03-20T12:00:00+00:00",
            "sample_count": "45",
        })

        coord = FakeCoordinator(store=store)
        result = await coord._async_load_cached_model()

        assert result is True
        assert coord._model.slope == 0.60
        assert coord._model.sample_count == 45

    async def test_returns_false_for_non_dict_data(self):
        store = FakeStore()
        await store.async_save("not a dict")

        coord = FakeCoordinator(store=store)
        result = await coord._async_load_cached_model()

        assert result is False

    async def test_handles_load_exception(self):
        store = FakeStore()
        store.async_load = AsyncMock(side_effect=Exception("disk error"))

        coord = FakeCoordinator(store=store)
        result = await coord._async_load_cached_model()

        assert result is False


class TestSaveModel:
    async def test_saves_model_to_store(self):
        store = FakeStore()
        coord = FakeCoordinator(store=store)
        coord._model = make_model()

        await coord._async_save_model()

        saved = await store.async_load()
        assert saved["slope"] == 0.60
        assert saved["intercept"] == 11.0
        assert saved["r_squared"] == 0.92
        assert saved["sample_count"] == 45
        assert saved["calibrated_at"] == "2026-03-20T12:00:00+00:00"

    async def test_handles_save_exception(self):
        store = FakeStore()
        store.async_save = AsyncMock(side_effect=Exception("disk error"))

        coord = FakeCoordinator(store=store)
        coord._model = make_model()

        # Should not raise
        await coord._async_save_model()


class TestRoundTrip:
    async def test_save_then_load_preserves_model(self):
        store = FakeStore()
        coord1 = FakeCoordinator(store=store)
        coord1._model = make_model(slope=0.72, intercept=9.5, r_squared=0.95, sample_count=90)

        await coord1._async_save_model()

        # Simulate restart — new coordinator, same store
        coord2 = FakeCoordinator(store=store)
        result = await coord2._async_load_cached_model()

        assert result is True
        assert coord2._model.slope == 0.72
        assert coord2._model.intercept == 9.5
        assert coord2._model.r_squared == 0.95
        assert coord2._model.sample_count == 90
