"""Tests for the calibration module."""

import pytest

from custom_components.tracker_predict.calibration import (
    CalibrationModel,
    compute_daily_means,
    compute_rolling_means,
    default_model,
    fit_linear_model,
)
from custom_components.tracker_predict.const import DEFAULT_ROLLING_WINDOW
from scripts.recalibrate import (
    _average_ranks,
    spearman_rho,
    top_n_window_overlap,
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

    def test_default_rolling_window(self):
        model = default_model()
        assert model.rolling_window == DEFAULT_ROLLING_WINDOW

    def test_custom_rolling_window_stored(self):
        model = CalibrationModel(
            slope=0.5, intercept=14.0, r_squared=0.9,
            calibrated_at=None, sample_count=60, rolling_window=21,
        )
        assert model.rolling_window == 21


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

    def test_rolling_window_stored_in_model(self):
        x = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0]
        y = [0.5 * xi + 10.0 for xi in x]
        model = fit_linear_model(x, y, rolling_window=21)
        assert model.rolling_window == 21

    def test_default_rolling_window_in_model(self):
        x = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0]
        y = [0.5 * xi + 10.0 for xi in x]
        model = fit_linear_model(x, y)
        assert model.rolling_window == DEFAULT_ROLLING_WINDOW


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

    def test_bst_late_evening_slot_buckets_to_next_uk_day(self):
        # BST: UK = UTC+1. UTC 23:00–23:30 on 2026-04-15 is 00:00–00:30 BST
        # on 2026-04-16, so the slot belongs to UK day 04-16 not 04-15.
        rates = [
            {"valid_from": "2026-04-15T22:00:00Z", "value_inc_vat": 10.0},
            {"valid_from": "2026-04-15T23:00:00Z", "value_inc_vat": 99.0},
            {"valid_from": "2026-04-15T23:30:00Z", "value_inc_vat": 99.0},
            {"valid_from": "2026-04-16T00:00:00Z", "value_inc_vat": 99.0},
        ]
        means = compute_daily_means(rates)
        # 22:00 UTC = 23:00 BST → still 04-15 in UK
        assert abs(means["2026-04-15"] - 10.0) < 0.01
        # 23:00 / 23:30 UTC and 00:00 UTC the next day all → 04-16 in UK
        assert abs(means["2026-04-16"] - 99.0) < 0.01

    def test_gmt_late_evening_slot_stays_on_same_day(self):
        # GMT (winter): UK = UTC. UTC 23:00 on 2026-12-15 is 23:00 GMT on
        # 2026-12-15, so this slot stays on UK day 12-15.
        rates = [
            {"valid_from": "2026-12-15T22:00:00Z", "value_inc_vat": 10.0},
            {"valid_from": "2026-12-15T23:00:00Z", "value_inc_vat": 20.0},
            {"valid_from": "2026-12-16T00:00:00Z", "value_inc_vat": 99.0},
        ]
        means = compute_daily_means(rates)
        assert abs(means["2026-12-15"] - 15.0) < 0.01
        assert abs(means["2026-12-16"] - 99.0) < 0.01

    def test_invalid_timestamp_is_skipped(self):
        rates = [
            {"valid_from": "not-a-date", "value_inc_vat": 10.0},
            {"valid_from": "2026-03-25T00:00:00Z", "value_inc_vat": 20.0},
        ]
        means = compute_daily_means(rates)
        assert means == {"2026-03-25": 20.0}


class TestComputeRollingMeans:
    def test_single_day(self):
        # Only one date — rolling mean equals the day's value regardless of window
        means = compute_rolling_means({"2026-03-25": 20.0}, window=14)
        assert abs(means["2026-03-25"] - 20.0) < 0.001

    def test_window_larger_than_series(self):
        # 3-day series with window=14 — rolling mean is just the cumulative mean
        daily = {
            "2026-03-25": 10.0,
            "2026-03-26": 20.0,
            "2026-03-27": 30.0,
        }
        rolling = compute_rolling_means(daily, window=14)
        assert abs(rolling["2026-03-25"] - 10.0) < 0.001
        assert abs(rolling["2026-03-26"] - 15.0) < 0.001   # mean(10, 20)
        assert abs(rolling["2026-03-27"] - 20.0) < 0.001   # mean(10, 20, 30)

    def test_window_equals_one(self):
        # Window of 1 is just the spot price
        daily = {"2026-03-25": 10.0, "2026-03-26": 20.0, "2026-03-27": 30.0}
        rolling = compute_rolling_means(daily, window=1)
        assert rolling == daily

    def test_exact_window_fit(self):
        # 4-day series with window=3; first two days use partial windows
        daily = {
            "2026-03-24": 10.0,
            "2026-03-25": 20.0,
            "2026-03-26": 30.0,
            "2026-03-27": 40.0,
        }
        rolling = compute_rolling_means(daily, window=3)
        assert abs(rolling["2026-03-24"] - 10.0) < 0.001          # partial: 1 day
        assert abs(rolling["2026-03-25"] - 15.0) < 0.001          # partial: 2 days
        assert abs(rolling["2026-03-26"] - 20.0) < 0.001          # full: mean(10,20,30)
        assert abs(rolling["2026-03-27"] - 30.0) < 0.001          # full: mean(20,30,40)

    def test_rolling_smooths_extremes(self):
        # Verify that rolling mean is less extreme than spot on a spike day
        daily = {f"2026-03-{d:02d}": 20.0 for d in range(1, 15)}
        daily["2026-03-14"] = 50.0  # spike on last day
        rolling = compute_rolling_means(daily, window=14)
        # 13 days at 20 + 1 day at 50 → mean = (13*20 + 50) / 14 ≈ 21.43
        assert rolling["2026-03-14"] < daily["2026-03-14"]
        assert abs(rolling["2026-03-14"] - (13 * 20.0 + 50.0) / 14) < 0.01

    def test_empty_input(self):
        assert compute_rolling_means({}, window=14) == {}


class TestAverageRanks:
    def test_strictly_increasing(self):
        assert _average_ranks([10.0, 20.0, 30.0]) == [1.0, 2.0, 3.0]

    def test_strictly_decreasing(self):
        assert _average_ranks([30.0, 20.0, 10.0]) == [3.0, 2.0, 1.0]

    def test_two_way_tie(self):
        # 20 and 20 share ranks 2 and 3 -> average 2.5
        assert _average_ranks([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]

    def test_three_way_tie(self):
        # All equal -> mean rank
        assert _average_ranks([5.0, 5.0, 5.0]) == [2.0, 2.0, 2.0]

    def test_singleton(self):
        assert _average_ranks([42.0]) == [1.0]


class TestSpearmanRho:
    def test_perfect_monotone(self):
        rho = spearman_rho([1.0, 2.0, 3.0, 4.0, 5.0], [10.0, 20.0, 30.0, 40.0, 50.0])
        assert abs(rho - 1.0) < 1e-9

    def test_reversed(self):
        rho = spearman_rho([1.0, 2.0, 3.0, 4.0, 5.0], [50.0, 40.0, 30.0, 20.0, 10.0])
        assert abs(rho + 1.0) < 1e-9

    def test_invariant_to_monotonic_transform(self):
        # Spearman should be 1.0 for any monotone-increasing transform
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [x ** 3 for x in xs]
        assert abs(spearman_rho(xs, ys) - 1.0) < 1e-9

    def test_zero_variance_returns_zero(self):
        assert spearman_rho([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0

    def test_too_few_points(self):
        assert spearman_rho([1.0], [2.0]) == 0.0
        assert spearman_rho([], []) == 0.0

    def test_mismatched_lengths(self):
        assert spearman_rho([1.0, 2.0], [1.0]) == 0.0

    def test_with_ties(self):
        # Hand-checked: same ordering with one tie on each side -> rho == 1
        xs = [1.0, 2.0, 2.0, 3.0]
        ys = [10.0, 20.0, 20.0, 30.0]
        assert abs(spearman_rho(xs, ys) - 1.0) < 1e-9


class TestTopNWindowOverlap:
    def test_perfect_agreement(self):
        # Identical series -> overlap is always 1.0
        s = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        assert top_n_window_overlap(s, s, n=3, window=7) == 1.0

    def test_known_8day_series(self):
        # 8 days, window=7: two windows (indices 0..6 and 1..7).
        # predicted is monotone ascending, so its bottom-3 in each window is the
        # first three indices: {0,1,2} then {1,2,3}.
        # actual values: [1, 5, 2, 6, 3, 7, 4, 8]
        #   window1 (idx 0..6, vals [1,5,2,6,3,7,4]) -> bottom-3 idx {0,2,4}
        #   window2 (idx 1..7, vals [5,2,6,3,7,4,8]) -> bottom-3 idx {2,4,6}
        # overlap window1 = |{0,1,2} ∩ {0,2,4}| / 3 = 2/3
        # overlap window2 = |{1,2,3} ∩ {2,4,6}| / 3 = 1/3
        # mean = (2/3 + 1/3) / 2 = 0.5
        predicted = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        actual = [1.0, 5.0, 2.0, 6.0, 3.0, 7.0, 4.0, 8.0]
        result = top_n_window_overlap(predicted, actual, n=3, window=7)
        assert abs(result - 0.5) < 1e-9

    def test_no_overlap_at_extremes(self):
        # Reversed predictor: top-3 cheapest predicted == top-3 most expensive actual
        s = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        rev = list(reversed(s))
        assert top_n_window_overlap(rev, s, n=3, window=7) == 0.0

    def test_window_too_large_returns_zero(self):
        assert top_n_window_overlap([1.0, 2.0], [1.0, 2.0], n=1, window=7) == 0.0

    def test_n_larger_than_window_returns_zero(self):
        s = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        assert top_n_window_overlap(s, s, n=10, window=7) == 0.0

    def test_mismatched_lengths_returns_zero(self):
        assert top_n_window_overlap([1.0, 2.0], [1.0], n=1, window=2) == 0.0

    def test_ties_are_deterministic(self):
        # All identical values -> top-N picks earliest indices on both sides,
        # so overlap is 1.0 (deterministic tie-break by index).
        s = [5.0] * 7
        assert top_n_window_overlap(s, s, n=3, window=7) == 1.0
