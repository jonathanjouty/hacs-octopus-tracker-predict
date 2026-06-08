# CLAUDE.md

## What is this repo?

A HACS-compatible Home Assistant custom integration that predicts Octopus Tracker electricity rates by transforming Agile Predict forecasts. The goal is **relative rank accuracy** — identifying which upcoming days are cheapest for EV charging, not exact prices.

## Repo layout

```
custom_components/tracker_predict/   # The HA integration
  __init__.py          # async_setup_entry / async_unload_entry / async_remove_entry
  calibration.py       # Linear regression model, Octopus API fetching, calibration pipeline
  config_flow.py       # UI config flow (region selector) + options flow
  const.py             # All constants, defaults, API URLs, region codes
  coordinator.py       # DataUpdateCoordinator — fetches Agile Predict, transforms to Tracker estimates
  sensor.py            # 5 sensor entities (today rank, forecast, cheapest 5d, cheapest 10d, forecast generated)
  calendar.py          # Calendar entity (one all-day event per forecast day)
  binary_sensor.py     # "Cheap today" binary sensor — present but NOT registered (not in PLATFORMS)
  manifest.json        # HACS/HA metadata
  strings.json         # UI strings
  translations/en.json # English translations

tests/
  conftest.py          # Mocks all homeassistant modules (HA is not installed in test env)
  test_calibration.py  # Unit tests for calibration math and data processing
  test_coordinator.py  # Unit tests for forecast transformation logic
  test_calendar.py     # Unit tests for the calendar entity
  test_persistence.py  # Unit tests for calibration model storage/restore
  test_live_api.py     # Live API tests (call real Agile Predict + Octopus APIs)

scripts/
  recalibrate.py            # Standalone script to recalibrate per-region defaults from live API data
  backfill_rank_metrics.py  # Backfill rank-correlation metrics into calibration_history.json
  drift_diagnostic.py       # Diagnostic for the rank-accuracy / day-bucketing investigation

tracker-predict-spec.md  # Original spec document
rank-accuracy-notes.md   # Findings from rank-metric backfill + candidate next steps
```

## Key technical decisions

- **No homeassistant dependency in tests**: `tests/conftest.py` mocks all `homeassistant.*` modules with fake base classes. This lets us run tests without installing HA.
- **Pure Python linear regression**: No numpy/scipy — keeps `manifest.json` requirement-free for HACS.
- **Tracker product discovery**: Octopus delists Tracker products from the listing API, so we probe `KNOWN_TRACKER_PRODUCTS` (in `const.py`) newest-first and use the first still-active code (default `SILVER-25-09-02`). Agile uses the normal listing API with an `AGILE-24-10-01` fallback. Both are overridable via the options flow.
- **Per-region default calibration**: `DEFAULT_CALIBRATION` dict in `const.py` stores slope/intercept per region. Recalibrated automatically from historical data when possible, with `scripts/recalibrate.py` for periodic updates.

## Running tests

```bash
pip install pytest "pytest-asyncio>=0.23" aiohttp
pytest tests/test_calibration.py tests/test_coordinator.py tests/test_calendar.py tests/test_persistence.py -v   # unit tests
pytest tests/test_live_api.py -v                                 # live API tests (needs network)
```

## CI

GitHub Actions (`.github/workflows/ci.yml`):
- **test**: Unit tests on Python 3.13 + 3.14 (runs `test_calibration.py` + `test_coordinator.py` only)
- **live-api**: Live API tests (runs after unit tests pass)
- **validate**: JSON syntax + Python compile checks

GitHub Actions (`.github/workflows/recalibrate.yml`):
- **recalibrate**: Weekly scheduled job (Mondays 06:00 UTC) that recalibrates per-region defaults from live API data and maintains a single rolling PR (`recalibrate/auto`)

## Common pitfalls

- Adding imports from `homeassistant` in test files requires updating the mocks in `conftest.py`
- The `conftest.py` fake classes need `__class_getitem__` to support generic syntax like `DataUpdateCoordinator[T]`
- `pytest-asyncio>=0.23` requires `asyncio_mode = "auto"` in pyproject.toml (no manual `@pytest.mark.asyncio` needed)
- Octopus products API changes frequently — product discovery may break; prefer hardcoded known-good product codes

## External APIs

- **Agile Predict**: `https://agilepredict.com/api/{region}` — no auth, poll max hourly
- **Octopus Energy**: `https://api.octopus.energy/v1/` — no auth for public tariff data
