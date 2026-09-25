"""Tests for the pure (offline) parts of scripts/eval_harness.py."""

from datetime import datetime

import pytest

from scripts.eval_harness import (
    Context,
    _UK_TZ,
    block_bootstrap_ci,
    day_features,
    linear_on_actual_agile,
    regret,
    score_issue,
    select_issues,
    slope_through_origin,
    magnitude_pairs,
    top_n_overlap,
)


def _slot(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}").replace(tzinfo=_UK_TZ)


class TestDayFeatures:
    def test_ex_peak_drops_16_to_19(self):
        slots = [(_slot("2026-09-01", h, m), 30.0 if 16 <= h < 19 else 10.0)
                 for h in range(24) for m in (0, 30)]
        f = day_features(slots)["2026-09-01"]
        assert f["n"] == 48
        assert f["ex_peak"] == pytest.approx(10.0)
        assert f["mean"] == pytest.approx((42 * 10 + 6 * 30) / 48)


class TestSelectIssues:
    def test_latest_before_hour(self):
        fcs = [
            {"created_at": "2026-09-01T06:15:00+01:00", "days": {}},
            {"created_at": "2026-09-01T09:15:00+01:00", "days": {}},
            {"created_at": "2026-09-01T11:15:00+01:00", "days": {}},
        ]
        chosen = select_issues(fcs, issue_hour=10)
        assert chosen["2026-09-01"]["created_at"] == "2026-09-01T09:15:00+01:00"

    def test_utc_timestamp_bucketed_by_uk_date(self):
        # 23:30 UTC on 31 Aug is 00:30 BST on 1 Sep.
        chosen = select_issues([{"created_at": "2026-08-31T23:30:00Z", "days": {}}], 10)
        assert list(chosen) == ["2026-09-01"]


class TestRankMetrics:
    def test_regret_zero_when_order_right(self):
        assert regret([1, 2, 3], [10, 20, 30]) == 0
        assert regret([1, 2, 3], [10, 20, 30], n=2) == 0

    def test_regret_measures_cost_of_wrong_pick(self):
        # Predicted cheapest is index 2 (actual 30); true cheapest is 10.
        assert regret([3, 2, 1], [10, 20, 30]) == pytest.approx(20)

    def test_top_n_overlap(self):
        assert top_n_overlap([1, 2, 3, 4], [1, 2, 3, 4], n=2) == 1.0
        assert top_n_overlap([4, 3, 2, 1], [1, 2, 3, 4], n=2) == 0.0

    def test_magnitude_slope(self):
        actual = [10.0, 20.0, 40.0]
        assert slope_through_origin(magnitude_pairs(actual, actual)) == pytest.approx(1.0)
        # A constant forecast has no relative differences at all.
        assert slope_through_origin(magnitude_pairs([15.0] * 3, actual)) == pytest.approx(0.0)


class TestBootstrap:
    def test_constant_series(self):
        lo, hi = block_bootstrap_ci([0.5] * 30)
        assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)

    def test_ci_contains_mean(self):
        diffs = [(-1) ** i * 0.1 + 0.2 for i in range(40)]
        lo, hi = block_bootstrap_ci(diffs)
        assert lo <= 0.2 <= hi


class TestWalkForward:
    def _ctx(self):
        days = [f"2026-08-{d:02d}" for d in range(1, 32)] + [f"2026-09-{d:02d}" for d in range(1, 11)]
        agile = {d: {"mean": 10.0 + i % 5, "ex_peak": 9.0 + i % 5, "n": 48} for i, d in enumerate(days)}
        tracker = {d: 2 * agile[d]["mean"] + 5 for d in days}
        fc_days = {d: {"mean": agile[d]["mean"], "ex_peak": 0.0, "n": 48} for d in days[-9:]}
        issues = {"2026-09-02": {"created_at": "2026-09-02T09:00:00+01:00", "days": fc_days}}
        return Context(agile, tracker, issues)

    def test_perfect_linear_relation_scores_perfectly(self):
        ctx = self._ctx()
        predictor = linear_on_actual_agile("mean", 60)(ctx, "2026-09-02")
        s = score_issue(ctx, "2026-09-02", predictor, horizon=7)
        assert s.mae == pytest.approx(0.0, abs=1e-9)
        assert s.regret1 == pytest.approx(0.0)
        assert s.top3 == 1.0

    def test_no_leakage_from_issue_day_onwards(self):
        ctx = self._ctx()
        # Corrupt everything from the issue date on; the fit must not change.
        for d in list(ctx.tracker):
            if d >= "2026-09-02":
                ctx.tracker[d] += 1000
        predictor = linear_on_actual_agile("mean", 60)(ctx, "2026-09-02")
        assert predictor("2026-09-03", {"mean": 10.0}) == pytest.approx(25.0)
