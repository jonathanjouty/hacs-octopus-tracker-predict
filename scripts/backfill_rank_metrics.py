#!/usr/bin/env python3
"""Retroactively compute rank-correlation metrics for entries already present
in ``calibration_history.json``.

For each existing history entry we:

1. Re-fetch the 90-day Octopus window that ended on that entry's date (using
   the ``period_to`` support added to ``recalibrate.fetch_rates``).
2. Re-derive the paired (rolling-mean Agile, actual Tracker) series the entry
   was originally fitted on, using the entry's stored ``rolling_window``
   (defaulting to 1 — i.e. spot daily mean — for entries that pre-date the
   rolling-mean change).
3. Apply the entry's stored slope/intercept to produce model predictions and
   compute three new metrics per region:
       * ``rank_spearman`` — Spearman ρ of model predictions vs actual Tracker
       * ``rank_top3_of_7`` — mean overlap of "top-3 of every rolling 7-day
         window" between predicted and actual
       * ``baseline_top3_of_7`` — same overlap metric but using raw Agile daily
         mean as the predictor (no model). Reveals whether the linear
         regression layer adds value over ranking by Agile alone.
4. Merge the new fields into the per-region entry and write the JSON back.

The script is idempotent: entries whose first region already has
``rank_spearman`` are skipped.

Usage:
    python scripts/backfill_rank_metrics.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, time, timezone
from pathlib import Path

import aiohttp

# Ensure ``scripts`` is importable when run directly from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.recalibrate import (  # noqa: E402
    DEFAULT_AGILE_PRODUCT,
    DEFAULT_TRACKER_PRODUCT,
    REGIONS,
    compute_daily_means,
    compute_rolling_means,
    discover_product_code,
    fetch_rates,
    spearman_rho,
    top_n_window_overlap,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_LOG = logging.getLogger(__name__)

CALIBRATION_DAYS = 90
TOP_N = 3
WINDOW = 7

HISTORY_PATH = _REPO_ROOT / "calibration_history.json"


def entry_already_backfilled(entry: dict) -> bool:
    """An entry is considered backfilled if every region in it has the new
    fields. We pick the first region as a representative — partial backfills
    will be re-attempted, which is the desired behaviour."""
    regions = entry.get("regions") or {}
    if not regions:
        return True  # nothing to do
    first = next(iter(regions.values()))
    return "rank_spearman" in first


def _entry_period_to(entry: dict) -> datetime:
    """Treat the entry's date as end-of-day UTC."""
    date_str = entry["date"]
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc)


async def _backfill_region(
    session: aiohttp.ClientSession,
    region: str,
    region_data: dict,
    period_to: datetime,
    agile_product: str,
    tracker_product: str,
) -> dict | None:
    """Compute rank metrics for a single (date, region) combo.

    Returns a dict of new fields to merge into ``region_data``, or ``None`` if
    metrics could not be computed (e.g. no overlapping data).
    """
    slope = float(region_data["slope"])
    intercept = float(region_data["intercept"])
    rolling_window = int(region_data.get("rolling_window") or 1)

    agile_rates = await fetch_rates(
        session, agile_product, region, CALIBRATION_DAYS, period_to=period_to,
    )
    tracker_rates = await fetch_rates(
        session, tracker_product, region, CALIBRATION_DAYS, period_to=period_to,
    )

    if not agile_rates or not tracker_rates:
        _LOG.warning("  %s: empty rate response, skipping", region)
        return None

    agile_daily = compute_daily_means(agile_rates)
    tracker_daily = compute_daily_means(tracker_rates)

    common = sorted(set(agile_daily) & set(tracker_daily))
    if len(common) < WINDOW:
        _LOG.warning("  %s: only %d common dates, skipping", region, len(common))
        return None

    agile_rolling_map = compute_rolling_means(agile_daily, rolling_window)
    agile_rolling = [agile_rolling_map[d] for d in common]
    agile_spot = [agile_daily[d] for d in common]
    tracker_actual = [tracker_daily[d] for d in common]
    model_pred = [slope * x + intercept for x in agile_rolling]

    return {
        "rank_spearman": round(spearman_rho(model_pred, tracker_actual), 4),
        "rank_top3_of_7": round(
            top_n_window_overlap(model_pred, tracker_actual, n=TOP_N, window=WINDOW),
            4,
        ),
        "baseline_top3_of_7": round(
            top_n_window_overlap(agile_spot, tracker_actual, n=TOP_N, window=WINDOW),
            4,
        ),
    }


async def backfill() -> int:
    """Walk every entry in calibration_history.json and add rank metrics.

    Returns the number of (entry, region) pairs updated.
    """
    if not HISTORY_PATH.exists():
        _LOG.error("%s does not exist — nothing to backfill.", HISTORY_PATH)
        return 0

    history: list[dict] = json.loads(HISTORY_PATH.read_text())
    if not history:
        _LOG.info("History file is empty — nothing to backfill.")
        return 0

    updated_pairs = 0

    async with aiohttp.ClientSession() as session:
        agile_product = (
            await discover_product_code(session, "AGILE") or DEFAULT_AGILE_PRODUCT
        )
        tracker_product = (
            await discover_product_code(session, "SILVER") or DEFAULT_TRACKER_PRODUCT
        )
        _LOG.info("Using Agile product: %s", agile_product)
        _LOG.info("Using Tracker product: %s", tracker_product)

        for entry in history:
            date_str = entry.get("date", "<unknown>")
            if entry_already_backfilled(entry):
                _LOG.info("Entry %s already has rank metrics, skipping.", date_str)
                continue

            period_to = _entry_period_to(entry)
            _LOG.info("Backfilling entry %s (period_to=%s)", date_str, period_to.isoformat())

            for region in REGIONS:
                region_data = entry.get("regions", {}).get(region)
                if region_data is None:
                    continue
                metrics = await _backfill_region(
                    session, region, region_data, period_to,
                    agile_product, tracker_product,
                )
                if metrics is None:
                    continue
                region_data.update(metrics)
                updated_pairs += 1
                _LOG.info(
                    "  %s: spearman=%.4f, top3/7=%.4f, baseline=%.4f",
                    region,
                    metrics["rank_spearman"],
                    metrics["rank_top3_of_7"],
                    metrics["baseline_top3_of_7"],
                )

    HISTORY_PATH.write_text(json.dumps(history, indent=2) + "\n")
    _LOG.info("Wrote %s (%d region rows updated)", HISTORY_PATH, updated_pairs)
    return updated_pairs


def main() -> None:
    asyncio.run(backfill())


if __name__ == "__main__":
    main()
