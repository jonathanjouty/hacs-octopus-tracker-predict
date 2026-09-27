#!/usr/bin/env python3
"""Walk-forward backtest of Tracker predictions against archived Agile Predict
forecasts.

``scripts/recalibrate.py`` scores the regression **in-sample** and feeds it
**actual** Agile prices. The live integration never sees actual Agile prices
for future days. It sees Agile Predict *forecasts*. This harness replays what
a user would have seen:

1. For each UK issue date, take the latest Agile Predict forecast created
   before ``--issue-hour`` (local time).
2. Fit each candidate model using only data available before that date
   (walk-forward, no leakage).
3. Predict Tracker for target days 1..``--horizon`` ahead and score against
   the Tracker rates Octopus actually published.

Candidates are compared with paired, block-bootstrapped confidence intervals
so "is this better, and by how much?" has an answer with error bars.

Agile Predict only serves its ~200 most recent forecasts (~2 months). Every
run merges newly seen forecasts into ``.eval_cache/`` so the backtest window
grows each time you run it.

Usage:
    python scripts/eval_harness.py                     # region A
    python scripts/eval_harness.py --regions A,C,M
    python scripts/eval_harness.py --regions all --out eval-report.md
    python scripts/eval_harness.py --offline           # cached data only

Requires: aiohttp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.recalibrate import (  # noqa: E402
    DEFAULT_AGILE_PRODUCT,
    DEFAULT_TRACKER_PRODUCT,
    REGIONS,
    compute_daily_means,
    discover_product_code,
    fetch_rates,
    spearman_rho,
)

_UK_TZ = ZoneInfo("Europe/London")
_LOG = logging.getLogger("eval_harness")

AGILE_PREDICT_URL = "https://agilepredict.com/api/{region}"
CACHE_DIR = _REPO_ROOT / ".eval_cache"

# Agile adds a fixed peak uplift 16:00–19:00 UK time. Tracker has no such
# uplift, so an ex-peak mean may track Tracker more closely.
PEAK_HOURS = (16, 19)
# A full UK day is 48 slots (46/50 on clock-change days).
MIN_DAY_SLOTS = 46
# Minimum training pairs before a fitted candidate is used (else it abstains).
MIN_TRAIN = 14
PRODUCTION_CAL_DAYS = 60  # DEFAULT_CALIBRATION_DAYS in const.py


# ── Data loading ─────────────────────────────────────────────────────────────


def _uk(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(_UK_TZ)


def day_features(slots: list[tuple[datetime, float]]) -> dict[str, dict]:
    """Aggregate half-hourly (UK datetime, price) slots into per-UK-day features.

    Returns {date: {"mean", "ex_peak", "n"}}.
    """
    by_day: dict[str, list[tuple[datetime, float]]] = {}
    for dt, value in slots:
        by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append((dt, value))
    out: dict[str, dict] = {}
    for d, vals in by_day.items():
        allv = [v for _, v in vals]
        off = [v for dt, v in vals if not PEAK_HOURS[0] <= dt.hour < PEAK_HOURS[1]]
        out[d] = {
            "mean": statistics.mean(allv),
            "ex_peak": statistics.mean(off) if off else statistics.mean(allv),
            "n": len(vals),
        }
    return out


async def load_forecasts(
    session: aiohttp.ClientSession | None, region: str
) -> list[dict]:
    """Return archived Agile Predict forecasts for a region, oldest first.

    Each item: {"created_at": iso, "days": {date: {"mean", "ex_peak", "n"}}}.
    Fetches the API's full backlog (unless offline) and merges into the cache.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"agile_predict_{region}.json"
    cached: dict[str, dict] = {}
    if path.exists():
        cached = {f["created_at"]: f for f in json.loads(path.read_text())}

    if session is not None:
        url = AGILE_PREDICT_URL.format(region=region)
        params = {"days": "14", "forecast_count": "1000"}
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=180)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            before = len(cached)
            for fc in data:
                slots = [
                    (_uk(p["date_time"]), float(p["agile_pred"]))
                    for p in fc.get("prices", [])
                    if "agile_pred" in p
                ]
                cached[fc["created_at"]] = {
                    "created_at": fc["created_at"],
                    "days": day_features(slots),
                }
            _LOG.info(
                "Region %s: %d forecasts fetched, %d new, %d cached",
                region, len(data), len(cached) - before, len(cached),
            )
            path.write_text(json.dumps(sorted(cached.values(), key=lambda f: f["created_at"])))
        except Exception:
            _LOG.exception("Agile Predict fetch failed for %s; using cache only", region)

    return sorted(cached.values(), key=lambda f: f["created_at"])


async def load_actuals(
    session: aiohttp.ClientSession | None,
    region: str,
    days: int,
    agile_product: str,
    tracker_product: str,
) -> tuple[dict[str, dict], dict[str, float]]:
    """Return (agile_actual_features, tracker_daily) for a region, cached."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"actuals_{region}.json"
    if session is None:
        data = json.loads(path.read_text())
        return data["agile"], data["tracker"]

    agile_rates = await fetch_rates(session, agile_product, region, days)
    tracker_rates = await fetch_rates(session, tracker_product, region, days)
    agile = day_features(
        [(_uk(r["valid_from"]), float(r["value_inc_vat"])) for r in agile_rates]
    )
    agile = {d: f for d, f in agile.items() if f["n"] >= MIN_DAY_SLOTS}
    tracker = compute_daily_means(tracker_rates)
    path.write_text(json.dumps({"agile": agile, "tracker": tracker}))
    return agile, tracker


def select_issues(forecasts: list[dict], issue_hour: int) -> dict[str, dict]:
    """Pick one forecast per UK date: the latest created before ``issue_hour``."""
    chosen: dict[str, dict] = {}
    for fc in forecasts:
        created = _uk(fc["created_at"])
        if created.hour >= issue_hour:
            continue
        d = created.strftime("%Y-%m-%d")
        if d not in chosen or fc["created_at"] > chosen[d]["created_at"]:
            chosen[d] = fc
    return chosen


# ── Models ───────────────────────────────────────────────────────────────────


def ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Ordinary least squares; returns (slope, intercept) or None."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


def clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


@dataclass
class Context:
    """Everything known about a region, used to build as-of-issue predictors."""

    agile: dict[str, dict]
    tracker: dict[str, float]
    issues: dict[str, dict]  # issue_date -> forecast


def _days_before(d: str, n: int) -> str:
    return (date_cls.fromisoformat(d) - timedelta(days=n)).isoformat()


def _horizon(issue: str, target: str) -> int:
    return (date_cls.fromisoformat(target) - date_cls.fromisoformat(issue)).days


# A candidate returns, for one issue date, a function
#   (target_date, forecast_day_features) -> predicted Tracker p/kWh | None
Predictor = Callable[[str, dict], float | None]
Candidate = Callable[[Context, str], Predictor | None]


def linear_on_actual_agile(feature: str, cal_days: int) -> Candidate:
    """Tracker ~ actual Agile ``feature`` over the ``cal_days`` before issue.

    With feature="mean" and cal_days=60 this is what the integration ships.
    """

    def build(ctx: Context, issue: str) -> Predictor | None:
        lo = _days_before(issue, cal_days)
        train = [
            d for d in ctx.agile
            if lo <= d < issue and d in ctx.tracker
        ]
        if len(train) < MIN_TRAIN:
            return None
        fit = ols([ctx.agile[d][feature] for d in train], [ctx.tracker[d] for d in train])
        if fit is None:
            return None
        slope, intercept = fit
        return lambda _t, f: clamp(slope * f[feature] + intercept)

    return build


def linear_on_forecasts(feature: str, cal_days: int) -> Candidate:
    """Tracker ~ *forecast* Agile ``feature``, fitted per horizon.

    Training pairs are (forecast issued on I', target D') with D' < issue so
    the Tracker outcome was known. Learns Agile Predict's horizon-dependent
    bias and shrinks long-range forecasts toward the mean.
    """

    def build(ctx: Context, issue: str) -> Predictor | None:
        lo = _days_before(issue, cal_days)
        by_h: dict[int, tuple[list[float], list[float]]] = {}
        for i_date, fc in ctx.issues.items():
            if not lo <= i_date < issue:
                continue
            for t, feats in fc["days"].items():
                if t >= issue or t not in ctx.tracker or feats["n"] < MIN_DAY_SLOTS:
                    continue
                xs, ys = by_h.setdefault(_horizon(i_date, t), ([], []))
                xs.append(feats[feature])
                ys.append(ctx.tracker[t])
        fits = {h: ols(xs, ys) for h, (xs, ys) in by_h.items() if len(xs) >= MIN_TRAIN}
        fits = {h: f for h, f in fits.items() if f is not None}
        if not fits:
            return None

        def predict(t: str, f: dict) -> float | None:
            fit = fits.get(_horizon(issue, t))
            if fit is None:
                return None
            return clamp(fit[0] * f[feature] + fit[1])

        return predict

    return build


def oracle_agile(cal_days: int) -> Candidate:
    """Production regression fed the *actual* Agile mean for the target day.

    Not achievable live: it is the ceiling if Agile Predict were perfect, so
    the gap to ``production`` is the error attributable to the upstream
    forecast rather than to our transform.
    """
    base = linear_on_actual_agile("mean", cal_days)

    def build(ctx: Context, issue: str) -> Predictor | None:
        inner = base(ctx, issue)
        if inner is None:
            return None
        return lambda t, _f: inner(t, ctx.agile[t]) if t in ctx.agile else None

    return build


def stretched(base: Candidate, horizon: int, cal_days: int) -> Candidate:
    """Widen ``base``'s day-to-day spread by a factor learnt from past issues.

    Agile Predict's daily means are under-dispersed (roughly half the real
    within-week spread), so predicted differences between days come out too
    small. For each past issue with all 1..horizon targets known, regress the
    actual deviation from the issue's mean on the predicted deviation
    (through the origin); apply that factor about the current issue's mean.
    Strictly rank-preserving: only MAE and magnitude can move.
    """

    def issue_targets(i: str) -> list[str]:
        d0 = date_cls.fromisoformat(i)
        return [(d0 + timedelta(days=h)).isoformat() for h in range(1, horizon + 1)]

    def build(ctx: Context, issue: str) -> Predictor | None:
        inner = base(ctx, issue)
        if inner is None:
            return None
        lo = _days_before(issue, cal_days)
        sxy = sxx = 0.0
        for i_date, fc in ctx.issues.items():
            ts = issue_targets(i_date)
            if not lo <= i_date < issue or ts[-1] >= issue:
                continue
            if not all(t in ctx.tracker and t in fc["days"] for t in ts):
                continue
            # The past issue's own model isn't needed: slope is shared, so
            # deviations of inner() are proportional to the true ones.
            preds = [inner(t, fc["days"][t]) for t in ts]
            acts = [ctx.tracker[t] for t in ts]
            mp, ma = statistics.mean(preds), statistics.mean(acts)
            sxy += sum((p - mp) * (a - ma) for p, a in zip(preds, acts))
            sxx += sum((p - mp) ** 2 for p in preds)
        if sxx == 0:
            return None
        k = sxy / sxx
        if k <= 0:
            return None
        fc = ctx.issues[issue]
        own = [
            inner(t, fc["days"][t]) for t in issue_targets(issue) if t in fc["days"]
        ]
        own = [p for p in own if p is not None]
        if not own:
            return None
        centre = statistics.mean(own)

        def predict(t: str, f: dict) -> float | None:
            p = inner(t, f)
            return None if p is None else clamp(centre + k * (p - centre))

        return predict

    return build


def last_known_tracker(ctx: Context, issue: str) -> Predictor | None:
    """Naive baseline: every future day = the most recent published Tracker rate."""
    known = [d for d in ctx.tracker if d < issue]
    if not known:
        return None
    value = ctx.tracker[max(known)]
    return lambda _t, _f: value


def make_candidates(horizon: int) -> dict[str, Candidate]:
    production = linear_on_actual_agile("mean", PRODUCTION_CAL_DAYS)
    return {
        "production": production,
        "cal_30d": linear_on_actual_agile("mean", 30),
        "cal_120d": linear_on_actual_agile("mean", 120),
        "ex_peak": linear_on_actual_agile("ex_peak", PRODUCTION_CAL_DAYS),
        "fit_on_forecasts": linear_on_forecasts("mean", PRODUCTION_CAL_DAYS),
        "stretch": stretched(production, horizon, PRODUCTION_CAL_DAYS),
        "last_known": last_known_tracker,
        "oracle_agile*": oracle_agile(PRODUCTION_CAL_DAYS),
    }


BASELINE = "production"


# ── Metrics ──────────────────────────────────────────────────────────────────


def top_n_overlap(pred: list[float], actual: list[float], n: int = 3) -> float:
    """Fraction of the actual n cheapest days that the prediction also picks."""
    idx = range(len(pred))
    p = set(sorted(idx, key=lambda i: (pred[i], i))[:n])
    a = set(sorted(idx, key=lambda i: (actual[i], i))[:n])
    return len(p & a) / n


def regret(pred: list[float], actual: list[float], n: int = 1) -> float:
    """Extra p/kWh paid by charging on the n days ``pred`` ranks cheapest,
    versus the n days that were actually cheapest (mean over the n days)."""
    idx = range(len(pred))
    p = sorted(idx, key=lambda i: (pred[i], i))[:n]
    return statistics.mean(actual[i] for i in p) - statistics.mean(sorted(actual)[:n])


def magnitude_pairs(pred: list[float], actual: list[float]) -> list[tuple[float, float]]:
    """(actual % diff, predicted % diff) for every ordered day pair i<j."""
    out = []
    for i in range(len(pred)):
        for j in range(i + 1, len(pred)):
            if actual[j] > 0 and pred[j] > 0:
                out.append((actual[i] / actual[j] - 1, pred[i] / pred[j] - 1))
    return out


def slope_through_origin(pairs: list[tuple[float, float]]) -> float:
    """Regress predicted % diff on actual % diff (no intercept).

    1.0 = relative differences are the right size; <1 = predictions too flat.
    """
    sxx = sum(a * a for a, _ in pairs)
    return sum(a * p for a, p in pairs) / sxx if sxx else float("nan")


@dataclass
class IssueScore:
    issue: str
    abs_err: dict[int, float] = field(default_factory=dict)  # horizon -> |err|
    err: dict[int, float] = field(default_factory=dict)       # horizon -> signed
    spearman: float | None = None
    top3: float | None = None
    regret1: float | None = None
    regret3: float | None = None
    mag_pairs: list[tuple[float, float]] = field(default_factory=list)

    @property
    def mae(self) -> float:
        return statistics.mean(self.abs_err.values())


def score_issue(
    ctx: Context, issue: str, predictor: Predictor, horizon: int
) -> IssueScore | None:
    """Score one issue. Rank metrics need the full 1..horizon set to be scored."""
    fc = ctx.issues[issue]
    s = IssueScore(issue)
    targets, preds, acts = [], [], []
    for h in range(1, horizon + 1):
        t = (date_cls.fromisoformat(issue) + timedelta(days=h)).isoformat()
        feats = fc["days"].get(t)
        if t not in ctx.tracker or feats is None or feats["n"] < MIN_DAY_SLOTS:
            continue
        p = predictor(t, feats)
        if p is None:
            continue
        a = ctx.tracker[t]
        s.abs_err[h] = abs(p - a)
        s.err[h] = p - a
        targets.append(t)
        preds.append(p)
        acts.append(a)
    if not targets:
        return None
    if len(targets) == horizon:
        s.spearman = spearman_rho(preds, acts)
        s.top3 = top_n_overlap(preds, acts, 3)
        s.regret1 = regret(preds, acts, 1)
        s.regret3 = regret(preds, acts, 3)
        s.mag_pairs = magnitude_pairs(preds, acts)
    return s


def block_bootstrap_ci(
    diffs: list[float], block: int = 7, reps: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% CI for the mean of a daily series with a moving-block bootstrap.

    Consecutive issues share target days, so they are not independent;
    resampling week-long blocks keeps that correlation in the CI.
    """
    n = len(diffs)
    if n < 2:
        return (float("nan"), float("nan"))
    block = min(block, n)
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n - block + 1)
            sample.extend(diffs[start : start + block])
        means.append(statistics.mean(sample[:n]))
    means.sort()
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


# ── Report ───────────────────────────────────────────────────────────────────


METRICS: dict[str, tuple[str, Callable[[IssueScore], float | None], bool]] = {
    # key: (label, getter, higher_is_better)
    "mae": ("MAE p/kWh", lambda s: s.mae, False),
    "regret1": ("regret@1 p/kWh", lambda s: s.regret1, False),
    "regret3": ("regret@3 p/kWh", lambda s: s.regret3, False),
    "top3": ("top-3 overlap", lambda s: s.top3, True),
    "spearman": ("Spearman ρ", lambda s: s.spearman, True),
}


def _fmt(v: float | None, nd: int = 3) -> str:
    return "–" if v is None or v != v else f"{v:.{nd}f}"


def evaluate(
    ctx: Context, horizon: int, candidates: dict[str, Candidate]
) -> dict[str, dict[str, IssueScore]]:
    results: dict[str, dict[str, IssueScore]] = {}
    for name, cand in candidates.items():
        per_issue: dict[str, IssueScore] = {}
        for issue in sorted(ctx.issues):
            predictor = cand(ctx, issue)
            if predictor is None:
                continue
            s = score_issue(ctx, issue, predictor, horizon)
            if s is not None:
                per_issue[issue] = s
        results[name] = per_issue
    return results


def render(
    region: str,
    results: dict[str, dict[str, IssueScore]],
    horizon: int,
    issue_hour: int,
) -> str:
    # Rank metrics need every day 1..horizon scored. Each candidate is
    # averaged over the issues it shares with the baseline (candidates that
    # need warm-up history start later), so pairs are like-for-like.
    def ranked(name: str) -> set[str]:
        return {i for i, s in results[name].items() if s.top3 is not None}

    base = results[BASELINE]
    base_issues = sorted(ranked(BASELINE))
    lines = [f"## Region {region} ({REGIONS.get(region, '?')})", ""]
    if not base_issues:
        return "\n".join(lines + ["No fully scored issues.", ""])

    lines += [
        f"{len(base_issues)} issue dates, {base_issues[0]} → {base_issues[-1]}; "
        f"forecast = latest before {issue_hour:02d}:00 UK; targets = days 1–{horizon} ahead.",
        "",
        "| candidate | n | " + " | ".join(m[0] for m in METRICS.values()) + " | magnitude slope |",
        "| --- |" + " ---: |" * (len(METRICS) + 2),
    ]
    for name, per_issue in results.items():
        common = sorted(ranked(name) & set(base_issues))
        if not common:
            continue
        row = [str(len(common))]
        for _label, get, _hib in METRICS.values():
            row.append(_fmt(statistics.mean(get(per_issue[i]) for i in common)))
        pairs = [p for i in common for p in per_issue[i].mag_pairs]
        row.append(_fmt(slope_through_origin(pairs), 2))
        lines.append(f"| {name} | " + " | ".join(row) + " |")
    lines += [
        f"| *random ranking* | | – | – | – | {3 / horizon:.3f} | 0.000 | – |",
        "",
        f"Difference vs `{BASELINE}` on shared issues (mean, 95% block-bootstrap CI; "
        "✓ = CI excludes 0 in the good direction):",
        "",
        "| candidate | n | " + " | ".join(m[0] for m in METRICS.values()) + " |",
        "| --- |" + " ---: |" * (len(METRICS) + 1),
    ]
    for name, per_issue in results.items():
        common = sorted(ranked(name) & set(base_issues))
        if name == BASELINE or not common:
            continue
        cells = [str(len(common))]
        for _label, get, hib in METRICS.values():
            diffs = [get(per_issue[i]) - get(base[i]) for i in common]
            lo, hi = block_bootstrap_ci(diffs)
            better = (lo > 0) if hib else (hi < 0)
            cells.append(
                f"{statistics.mean(diffs):+.3f} [{lo:+.3f}, {hi:+.3f}]" + (" ✓" if better else "")
            )
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    lines += ["", "MAE by horizon (days ahead), all scored issues:", ""]
    lines += [
        "| candidate | " + " | ".join(f"h={h}" for h in range(1, horizon + 1)) + " |",
        "| --- |" + " ---: |" * horizon,
    ]
    for name, per_issue in results.items():
        cells = []
        for h in range(1, horizon + 1):
            errs = [s.abs_err[h] for s in per_issue.values() if h in s.abs_err]
            cells.append(_fmt(statistics.mean(errs), 2) if errs else "–")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Bias by horizon (predicted − actual, p/kWh):",
        "",
        "| candidate | " + " | ".join(f"h={h}" for h in range(1, horizon + 1)) + " |",
        "| --- |" + " ---: |" * horizon,
    ]
    for name, per_issue in results.items():
        cells = []
        for h in range(1, horizon + 1):
            errs = [s.err[h] for s in per_issue.values() if h in s.err]
            cells.append(f"{statistics.mean(errs):+.2f}" if errs else "–")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def render_summary(all_results: dict[str, dict[str, dict[str, IssueScore]]]) -> str:
    """Pool every (region, issue) pair shared with the baseline.

    Regions see near-identical wholesale prices, so pooled pairs are highly
    correlated across regions; CIs therefore resample issue *dates* in blocks
    (region scores for a date are averaged first) rather than treating 14
    regions as 14× the data.
    """
    names = list(next(iter(all_results.values())))
    lines = [
        "## All regions",
        "",
        "Mean over regions of each region's per-issue mean. Diff CIs are over issue dates, "
        "with regions averaged per date first.",
        "",
        "| candidate | " + " | ".join(m[0] for m in METRICS.values()) + " |",
        "| --- |" + " ---: |" * len(METRICS),
    ]
    diff_rows = []
    for name in names:
        cells, dcells = [], []
        for _label, get, hib in METRICS.values():
            by_date: dict[str, list[float]] = {}
            by_date_diff: dict[str, list[float]] = {}
            for res in all_results.values():
                base, mine = res[BASELINE], res[name]
                for i, s in mine.items():
                    if s.top3 is None or i not in base or base[i].top3 is None:
                        continue
                    by_date.setdefault(i, []).append(get(s))
                    by_date_diff.setdefault(i, []).append(get(s) - get(base[i]))
            dates = sorted(by_date)
            if not dates:
                cells.append("–")
                dcells.append("–")
                continue
            cells.append(_fmt(statistics.mean(statistics.mean(by_date[d]) for d in dates)))
            diffs = [statistics.mean(by_date_diff[d]) for d in dates]
            lo, hi = block_bootstrap_ci(diffs)
            better = (lo > 0) if hib else (hi < 0)
            dcells.append(
                f"{statistics.mean(diffs):+.3f} [{lo:+.3f}, {hi:+.3f}]" + (" ✓" if better else "")
            )
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
        if name != BASELINE:
            diff_rows.append(f"| {name} | " + " | ".join(dcells) + " |")
    lines += [
        "",
        f"Difference vs `{BASELINE}`:",
        "",
        "| candidate | " + " | ".join(m[0] for m in METRICS.values()) + " |",
        "| --- |" + " ---: |" * len(METRICS),
        *diff_rows,
        "",
    ]
    return "\n".join(lines)


HEADER = """# Tracker Predict: backtest report

Generated {now} by `scripts/eval_harness.py`.

* **MAE**: mean absolute error of predicted vs published Tracker rate.
* **regret@1**: p/kWh lost by charging on the predicted cheapest day rather than
  the actual cheapest day in the window. **regret@3**: same for the 3 cheapest.
  0 = perfect. These are the most decision-relevant numbers.
* **top-3 overlap**: share of the actual 3 cheapest days that were predicted.
* **magnitude slope**: predicted % difference between two days regressed on
  actual % difference. 1.0 = right size, <1 = too flat, >1 = exaggerated.
* `oracle_agile*` is not achievable live. It feeds the model the *actual* Agile
  prices, so its gap to `production` is the share of error caused by the Agile
  Predict forecast rather than by our transform.
* Any strictly increasing transform of one feature ranks days identically, so
  `cal_*` windows can change MAE but never rank metrics.
"""


async def main_async(args: argparse.Namespace) -> str:
    regions = list(REGIONS) if args.regions == "all" else args.regions.split(",")
    sections = []
    all_results: dict[str, dict[str, dict[str, IssueScore]]] = {}
    session = None if args.offline else aiohttp.ClientSession()
    try:
        if session is not None:
            agile_product = await discover_product_code(session, "AGILE") or DEFAULT_AGILE_PRODUCT
            tracker_product = await discover_product_code(session, "SILVER") or DEFAULT_TRACKER_PRODUCT
        else:
            agile_product, tracker_product = DEFAULT_AGILE_PRODUCT, DEFAULT_TRACKER_PRODUCT
        for region in regions:
            forecasts = await load_forecasts(session, region)
            agile, tracker = await load_actuals(
                session, region, args.history_days, agile_product, tracker_product
            )
            ctx = Context(agile, tracker, select_issues(forecasts, args.issue_hour))
            results = evaluate(ctx, args.horizon, make_candidates(args.horizon))
            all_results[region] = results
            sections.append(render(region, results, args.horizon, args.issue_hour))
    finally:
        if session is not None:
            await session.close()
    now = datetime.now(_UK_TZ).strftime("%Y-%m-%d %H:%M %Z")
    if len(all_results) > 1:
        sections.insert(0, render_summary(all_results))
    return HEADER.format(now=now) + "\n" + "\n".join(sections)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--regions", default="A", help="comma-separated region codes, or 'all'")
    p.add_argument("--horizon", type=int, default=7, help="days ahead to score (default 7)")
    p.add_argument("--issue-hour", type=int, default=10, help="use latest forecast before this UK hour")
    p.add_argument("--history-days", type=int, default=240, help="days of Octopus actuals to fetch")
    p.add_argument("--offline", action="store_true", help="use .eval_cache only")
    p.add_argument("--out", type=Path, help="write the Markdown report here as well as stdout")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    report = asyncio.run(main_async(args))
    print(report)
    if args.out:
        args.out.write_text(report)


if __name__ == "__main__":
    main()
