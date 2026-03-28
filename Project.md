# Project Status

## Current state (March 2026)

The integration is **functional but pre-release**. Core logic is implemented, unit and live API tests pass, and CI is set up. PR #1 is open for initial merge.

### What works

- Config flow with region selection
- Agile Predict API fetching with fallback to alt endpoint
- Octopus historical rate fetching with pagination
- Linear regression calibration from historical Agile vs Tracker data
- Forecast transformation: daily means, confidence levels, clamping
- 4 entities: today's rate, full forecast, cheapest day, cheap-today binary sensor
- 24 unit tests (calibration + coordinator)
- 13 live API tests (Agile Predict + Octopus Energy)
- GitHub Actions CI (unit tests on 3.13/3.14, live API tests, JSON validation)

### What hasn't been tested in a real HA instance

- The full integration lifecycle (install via HACS, configure, see entities)
- Config flow UI rendering
- DataUpdateCoordinator polling loop
- Entity state updates and attributes in HA dashboard
- Error recovery / stale data fallback in practice

## Possible next steps

### High priority

1. **Test in a real Home Assistant instance** — Install via HACS custom repo, configure, verify entities appear and update correctly
2. **Add `info.md` / README** — Required for HACS repository listing; describe what it does, how to install, screenshots
3. **Options flow** — Allow changing calibration interval, cheap threshold percentile, forecast window after initial setup
4. **Recalibration robustness** — Handle Octopus API outages gracefully; cache last-good calibration model to disk

### Medium priority

5. **More entity attributes** — Expose calibration model params (slope, intercept, R-squared) as diagnostic attributes
6. **Services** — `tracker_predict.force_recalibrate` service to trigger on-demand recalibration
7. **Unit tests for config_flow** — Currently untested; needs HA test harness or more mocking
8. **Unit tests for __init__.py** — Test setup/unload entry lifecycle
9. **Improve confidence levels** — Currently based on fixed thresholds; could use prediction intervals from regression

### Lower priority

10. **Gas tracker prediction** — Same approach could work for Octopus Tracker gas rates
11. **Multiple region support** — Allow configuring multiple entries for different regions
12. **Historical accuracy tracking** — Log predicted vs actual rates over time to show calibration quality
13. **Configurable product codes** — UI option to override the hardcoded AGILE/SILVER product codes
14. **HACS default repository submission** — Once stable, submit to the HACS default repo list

## Known issues

- **Tracker product discovery is broken**: Octopus delisted Tracker products from their API. We hardcode `SILVER-24-04-03`. If Octopus changes product codes, this will need updating.
- **Agile product discovery may also break**: Currently discovers via API but falls back to `AGILE-24-10-01`.
- **No persistent storage**: Calibration model is recalculated on every HA restart. Could use `hass.data` or a JSON file for caching.
- **Default model may drift**: The fallback slope=0.56/intercept=12.75 is from 2025 East England data. May need periodic updating.
