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

No API keys or account credentials are needed. This integration is most useful if you are on an Octopus Tracker tariff, but it has no technical dependency on one.

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

Six entities are created per configured region, grouped under a single **Tracker Predict (region)** device in Settings → Devices.

### `sensor.tracker_predict_today_rank_{region}`

Today's rank among all forecast days. `1` means today is the cheapest day in the current forecast window.

| Attribute | Description |
|-----------|-------------|
| `days_in_window` | Total number of days in the forecast |
| `stale` | `true` if the last API fetch failed and this is cached data |

---

### `sensor.tracker_predict_forecast_{region}`

Full 14-day forecast. State is the number of days in the forecast; the data is in attributes.

| Attribute | Description |
|-----------|-------------|
| `forecast` | List of daily forecast objects (see below) |
| `model_slope` | Current regression slope |
| `model_intercept` | Current regression intercept |
| `model_r_squared` | Current R² |
| `model_last_calibrated` | Timestamp of last recalibration |
| `forecast_generated_at` | When AgilePredict generated this forecast |
| `stale` | Whether forecast data is cached |

Each entry in `forecast`:
```json
{
  "date": "2026-04-01",
  "day_of_week": "Wed",
  "tracker_est": 24.3,
  "tracker_low": 21.1,
  "tracker_high": 27.5,
  "confidence": "high",
  "rank": 1
}
```
`rank` is 1 for the cheapest day in the forecast window.

---

### `sensor.tracker_predict_cheapest_5d_{region}`

The date of the cheapest predicted day within the next 5 days.

| Attribute | Description |
|-----------|-------------|
| `date` | Date string (YYYY-MM-DD) |
| `day_of_week` | Day abbreviation (e.g. `Wed`) |
| `tracker_est` | Predicted rate (p/kWh) |
| `tracker_low` / `tracker_high` | Confidence bounds |
| `days_away` | How many days from today |
| `confidence` | Forecast confidence level |

---

### `sensor.tracker_predict_cheapest_10d_{region}`

Same as above but for the next 10 days.

---

### `sensor.tracker_predict_forecast_generated_{region}`

Timestamp showing when AgilePredict last generated the forecast. AgilePredict publishes new forecasts approximately 4 times per day; this sensor updates within an hour of each new forecast being available.

---

### `calendar.tracker_predict_{region}`

A calendar entity showing each forecast day as an all-day event. Each event is labelled with the predicted rate and rank, e.g.:

```
Tracker: 24.3p/kWh (cheapest)
Tracker: 31.7p/kWh (2nd cheapest)
```

Add this calendar to a Lovelace **Calendar card** to visualise the forecast at a glance. It can also be used in calendar-based automations (e.g. trigger EV charging on the cheapest day).

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

## A note on how this was built

This integration was built almost entirely with [Claude](https://claude.ai/), Anthropic's AI assistant. The author is not a Home Assistant developer — just someone on an Octopus Tracker tariff who wanted to know which day to charge the car. It turns out that "imperfect thing that exists" beats "perfect thing that doesn't" every time.

If this project is open-sourced, PRs and issues will likely also be triaged and addressed by Claude unless a kind human decides to take over as maintainer (very welcome to!).

---

## License

MIT
