"""Tests for the calibration module."""

import pytest

from custom_components.tracker_predict.calibration import (
    CalibrationModel,
    compute_daily_means,
    default_model,
    fit_linear_model,
)


class TestCalibrationModel:
    def test_predict_basic(self):
        model = default_model()
        result = model.predict(20.0)
        expected = model.slope * 20.0 + model.intercept
        assert abs(result - expected) < 0.01

    def test_predict_clamp_low(self):
        model = CalibrationModel(
            slope=1.0, intercept=-200.0, r_squared=0.9,
            calibrated_at=None, sample_count=10,
        )
        assert model.predict(50.0) == 0.0

    def test_predict_clamp_high(self):
        model = CalibrationModel(
            slope=2.0, intercept=50.0, r_squared=0.9,
            calibrated_at=None, sample_count=10,
        )
        assert model.predict(50.0) == 100.0


class TestFitLinearModel:
    def test_perfect_linear(self):
        x = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0]
        y = [0.5 * xi + 10.0 for xi in x]
        model = fit_linear_model(x, y)
        assert abs(model.slope - 0.5) < 0.001
        assert abs(model.intercept - 10.0) < 0.001
        assert abs(model.r_squared - 1.0) < 0.001
        assert model.sample_count == 7

    def test_too_few_samples(self):
        model = fit_linear_model([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
        assert model.sample_count == 0

    def test_noisy_data(self):
        x = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0]
        y = [18.0, 24.0, 28.0, 35.0, 41.0, 45.0, 52.0]
        model = fit_linear_model(x, y)
        assert 0.9 < model.r_squared <= 1.0
        assert model.slope > 0
        assert model.sample_count == 7

    def test_zero_variance(self):
        x = [20.0] * 10
        y = [15.0 + i for i in range(10)]
        model = fit_linear_model(x, y)
        assert model.sample_count == 0

    def test_known_relationship(self):
        import random
        random.seed(42)
        x = [random.uniform(10, 50) for _ in range(60)]
        y = [0.56 * xi + 12.75 + random.gauss(0, 1) for xi in x]
        model = fit_linear_model(x, y)
        assert abs(model.slope - 0.56) < 0.1
        assert abs(model.intercept - 12.75) < 2.0
        assert model.r_squared > 0.85


class TestComputeDailyMeans:
    def test_basic(self):
        rates = [
            {"valid_from": "2026-03-25T00:00:00Z", "value_inc_vat": 10.0},
            {"valid_from": "2026-03-25T00:30:00Z", "value_inc_vat": 20.0},
            {"valid_from": "2026-03-26T00:00:00Z", "value_inc_vat": 30.0},
        ]
        means = compute_daily_means(rates)
        assert abs(means["2026-03-25"] - 15.0) < 0.01
        assert abs(means["2026-03-26"] - 30.0) < 0.01

    def test_empty(self):
        assert compute_daily_means([]) == {}

    def test_missing_fields(self):
        rates = [
            {"valid_from": "2026-03-25T00:00:00Z"},
            {"value_inc_vat": 10.0},
        ]
        assert compute_daily_means(rates) == {}

    def test_full_day(self):
        rates = []
        for i in range(48):
            hour = i // 2
            minute = "00" if i % 2 == 0 else "30"
            rates.append({
                "valid_from": f"2026-03-25T{hour:02d}:{minute}:00Z",
                "value_inc_vat": 20.0 + i,
            })
        means = compute_daily_means(rates)
        assert len(means) == 1
        assert abs(means["2026-03-25"] - 43.5) < 0.01
