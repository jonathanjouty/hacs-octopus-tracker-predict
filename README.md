# Tracker Predict

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![CI](https://github.com/jonathanjouty/hacs-octopus-tracker-predict/actions/workflows/ci.yml/badge.svg)](https://github.com/jonathanjouty/hacs-octopus-tracker-predict/actions/workflows/ci.yml)

Predicts [Octopus Tracker](https://octopus.energy/tracker/) electricity rates up to 14 days ahead, so you can plan EV charging and high-energy tasks around the cheapest upcoming days.

Forecasts are derived from [Agile Predict](https://agilepredict.com/) data and transformed into Tracker estimates using a self-calibrating linear regression model. The model automatically recalibrates every 7 days using your region's actual historical Agile and Tracker rates fetched from the Octopus Energy API.

**Validated accuracy (2025, East England):**
- R² = 0.90 between predicted and actual Tracker rates
- Cheapest-day identification: 84–85% exact match, 97% within the top 3

> **Goal is relative rank accuracy** — identifying which upcoming days are cheapest, not predicting exact prices.

---

## Prerequisites

- Home Assistant with [HACS](https://hacs.xyz/) installed
- An active Octopus Tracker electricity tariff

---

## Installation

1. In Home Assistant, open **HACS → Integrations**
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/jonathanjouty/hacs-octopus-tracker-predict` as an **Integration**
4. Search for **Tracker Predict** and install it
5. Restart Home Assistant

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **Tracker Predict**.

You will be asked to select your **DNO region** (Distribution Network Operator area). Choose the region matching your electricity supply address.

| Code | Region |
|------|--------|
| A | Eastern England |
| B | East Midlands |
| C | London |
| D | Merseyside and North Wales |
| E | West Midlands |
| F | North Eastern England |
| G | North Western England |
| H | Northern Scotland |
| J | South Eastern England |
| K | Southern England |
| L | South Wales |
| M | South Western England |
| N | Southern Scotland |
| P | Yorkshire |

> If you are unsure of your region, check your electricity bill or use the [Energy Networks Association postcode checker](https://www.energynetworks.org/customers/find-my-network-operator).

---

## Entities

Five entities are created per configured region.

### `sensor.tracker_predict_{region}_today`

Today's predicted Tracker rate.

| Attribute | Description |
|-----------|-------------|
| `tracker_estimate` | Central estimate (p/kWh) |
| `tracker_low` | Lower bound (p/kWh) |
| `tracker_high` | Upper bound (p/kWh) |
| `confidence` | `high` (≤2 days), `medium` (3–5 days), or `low` (6+ days) |
| `agile_daily_mean` | Underlying Agile mean used as model input |
| `model_r_squared` | R² of current calibration model |
| `stale` | `true` if the last API fetch failed and this is cached data |
| `last_updated` | Timestamp of last successful fetch |

---

### `sensor.tracker_predict_{region}_forecast`

Full 14-day forecast. State is the number of days in the forecast; the data is in attributes.

| Attribute | Description |
|-----------|-------------|
| `forecast` | List of daily forecast objects (see below) |
| `model_slope` | Current regression slope |
| `model_intercept` | Current regression intercept |
| `model_r_squared` | Current R² |
| `model_last_calibrated` | Timestamp of last recalibration |
| `stale` | Whether forecast data is cached |

Each entry in `forecast`:
```json
{
  "date": "2026-04-01",
  "day_of_week": "Wednesday",
  "tracker_est": 24.3,
  "tracker_low": 21.1,
  "tracker_high": 27.5,
  "confidence": "high",
  "rank": 1
}
```
`rank` is 1 for the cheapest day in the forecast window.

---

### `sensor.tracker_predict_{region}_cheapest_5d`

The date of the cheapest predicted day within the next 5 days.

| Attribute | Description |
|-----------|-------------|
| `date` | Date string (YYYY-MM-DD) |
| `day_of_week` | Day name |
| `tracker_est` | Predicted rate (p/kWh) |
| `tracker_low` / `tracker_high` | Confidence bounds |
| `days_away` | How many days from today |
| `confidence` | Forecast confidence level |

---

### `sensor.tracker_predict_{region}_cheapest_10d`

Same as above but for the next 10 days.

---

### `binary_sensor.tracker_predict_{region}_cheap_today`

`ON` if today's predicted rate is in the cheapest 20th percentile of the current forecast window.

| Attribute | Description |
|-----------|-------------|
| `threshold` | The p/kWh rate used as the cheap/expensive cutoff |
| `tracker_est` | Today's predicted rate |

---

## How it works

1. **Fetch**: Polls [Agile Predict](https://agilepredict.com/) hourly for half-hourly price forecasts for your region
2. **Transform**: Groups forecasts by day, computes daily means, applies `Tracker ≈ slope × agile_mean + intercept`
3. **Calibrate**: Every 7 days, fetches the last 60 days of actual Agile and Tracker rates from the Octopus Energy API and refits the linear model
4. **Fallback**: If calibration or the API is unavailable, retains the last successful forecast and marks it as stale

No API keys or authentication are required — both Agile Predict and the Octopus public tariff endpoints are freely accessible.

---

## Limitations

- **Forecast accuracy degrades beyond ~5 days** — confidence is marked `low` for days 6+
- **Hardcoded Tracker product code**: Uses `SILVER-24-04-03`. If Octopus changes this code, calibration will silently fall back to the default model until manually updated
- **Default model is East England 2025 data**: On first install (before calibration completes), predictions use slope=0.56 / intercept=12.75 — reasonable for most UK regions but may be slightly off
- **Calibration resets on HA restart**: The model is recalibrated on startup; there is no persistence between restarts

---

## License

MIT
