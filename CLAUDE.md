# CLAUDE.md

## What is this repo?

A HACS-compatible Home Assistant custom integration that predicts Octopus Tracker electricity rates by transforming Agile Predict forecasts. The goal is **relative rank accuracy** — identifying which upcoming days are cheapest for EV charging, not exact prices.

## Repo layout

```
custom_components/tracker_predict/   # The HA integration
  __init__.py          # async_setup_entry / async_unload_entry
  calibration.py       # Linear regression model, Octopus API fetching, calibration pipeline
  config_flow.py       # UI config flow (region selector)
  const.py             # All constants, defaults, API URLs, region codes
  coordinator.py       # DataUpdateCoordinator — fetches Agile Predict, transforms to Tracker estimates
  sensor.py            # 3 sensor entities (today rate, forecast, cheapest day)
  binary_sensor.py     # 1 binary sensor (cheap today threshold)
  manifest.json        # HACS/HA metadata
  strings.json         # UI strings
  translations/en.json # English translations

tests/
  conftest.py          # Mocks all homeassistant modules (HA is not installed in test env)
  test_calibration.py  # Unit tests for calibration math and data processing
  test_coordinator.py  # Unit tests for forecast transformation logic
  test_live_api.py     # Live API tests (call real Agile Predict + Octopus APIs)

tracker-predict-spec.md  # Original spec document
```

## Key technical decisions

- **No homeassistant dependency in tests**: `tests/conftest.py` mocks all `homeassistant.*` modules with fake base classes. This lets us run tests without installing HA.
- **Pure Python linear regression**: No numpy/scipy — keeps `manifest.json` requirement-free for HACS.
- **Hardcoded Tracker product code**: `SILVER-24-04-03` — Octopus delisted Tracker products from their API but the tariff endpoints still work.
- **Default calibration model**: slope=0.56, intercept=12.75 (2025 East England data). Recalibrated automatically from historical data when possible.

## Running tests

```bash
pip install pytest "pytest-asyncio>=0.23" aiohttp
pytest tests/test_calibration.py tests/test_coordinator.py -v   # unit tests
pytest tests/test_live_api.py -v                                 # live API tests (needs network)
```

## CI

GitHub Actions (`.github/workflows/ci.yml`):
- **test**: Unit tests on Python 3.13 + 3.14
- **live-api**: Live API tests (runs after unit tests pass)
- **validate**: JSON syntax + Python compile checks

## Common pitfalls

- Adding imports from `homeassistant` in test files requires updating the mocks in `conftest.py`
- The `conftest.py` fake classes need `__class_getitem__` to support generic syntax like `DataUpdateCoordinator[T]`
- `pytest-asyncio>=0.23` requires `asyncio_mode = "auto"` in pyproject.toml (no manual `@pytest.mark.asyncio` needed)
- Octopus products API changes frequently — product discovery may break; prefer hardcoded known-good product codes

## External APIs

- **Agile Predict**: `https://agilepredict.com/api/{region}` — no auth, poll max hourly
- **Octopus Energy**: `https://api.octopus.energy/v1/` — no auth for public tariff data
