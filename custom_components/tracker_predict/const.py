"""Constants for the Tracker Predict integration."""

DOMAIN = "tracker_predict"

CONF_REGION = "region"
CONF_POLL_INTERVAL = "poll_interval_minutes"
CONF_CALIBRATION_DAYS = "calibration_days"
CONF_CALIBRATION_INTERVAL = "calibration_interval_hours"
CONF_AGILE_PRODUCT_CODE = "agile_product_code"
CONF_TRACKER_PRODUCT_CODE = "tracker_product_code"

DEFAULT_REGION = "A"
DEFAULT_POLL_INTERVAL = 60
DEFAULT_CALIBRATION_DAYS = 60
DEFAULT_CALIBRATION_INTERVAL = 168  # 7 days in hours

# Fallback linear model — overall average used when region lookup fails
DEFAULT_SLOPE = 0.6182
DEFAULT_INTERCEPT = 11.39
# Rolling window (days) for the Agile mean used as the model input feature.
# After fixing UTC→UK day-bucketing in compute_daily_means, the spot daily
# mean (window=1) wins the recalibration grid-search across every region:
# smoothing the input flattens the predictions and hurts both R² and rank.
# Larger windows are still tried (see ROLLING_WINDOW_CANDIDATES in
# scripts/recalibrate.py) in case future data shifts the trade-off.
DEFAULT_ROLLING_WINDOW = 1

# Per-region default calibration (slope, intercept).
# All regions currently share the same fallback values. The quarterly
# recalibrate.yml workflow will differentiate them with real API data.
DEFAULT_CALIBRATION: dict[str, tuple[float, float]] = {
    "A": (0.6342, 11.5),
    "B": (0.6524, 10.56),
    "C": (0.6667, 10.72),
    "D": (0.6141, 12.48),
    "E": (0.6265, 10.67),
    "F": (0.6171, 10.78),
    "G": (0.6265, 12.04),
    "H": (0.6255, 11.56),
    "J": (0.5992, 11.92),
    "K": (0.5918, 11.72),
    "L": (0.5602, 11.99),
    "M": (0.6682, 10.25),
    "N": (0.6318, 10.85),
    "P": (0.5404, 12.49),
}

# All known Octopus Tracker (SILVER) product codes, newest first.
# Tracker products are deliberately excluded from the Octopus products listing
# API, so we probe these codes directly to find the currently-active one.
# Add new codes here when Octopus releases a new Tracker version.
KNOWN_TRACKER_PRODUCTS = [
    "SILVER-25-09-02",       # September 2025 v1
    "SILVER-25-04-15",       # April 2025 v1
    "SILVER-24-12-31",       # December 2024 v1
    "SILVER-24-10-01",       # October 2024 v1
    "SILVER-24-07-01",       # July 2024 v1
    "SILVER-24-04-03",       # April 2024 v1
    "SILVER-23-12-06",       # December 2023 v1
    "SILVER-FLEX-22-11-25",  # November 2022 v1 (original)
]

# Default product codes — Tracker is no longer listed in the Octopus products
# API but the tariff endpoints still work with known product codes.
DEFAULT_AGILE_PRODUCT = "AGILE-24-10-01"
DEFAULT_TRACKER_PRODUCT = KNOWN_TRACKER_PRODUCTS[0]

AGILE_PREDICT_URL = "https://agilepredict.com/api/{region}"
AGILE_PREDICT_ALT_URL = "https://prices.fly.dev/api/{region}"

OCTOPUS_API_BASE = "https://api.octopus.energy/v1"
OCTOPUS_PRODUCTS_URL = f"{OCTOPUS_API_BASE}/products/"

REGIONS = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside and North Wales",
    "E": "West Midlands",
    "F": "North Eastern England",
    "G": "North Western England",
    "H": "Northern Scotland",
    "J": "South Eastern England",
    "K": "Southern England",
    "L": "South Wales",
    "M": "South Western England",
    "N": "Southern Scotland",
    "P": "Yorkshire",
}

# Octopus API tariff URL templates
AGILE_TARIFF_URL = (
    "{base}/products/{product}/electricity-tariffs/"
    "E-1R-{product}-{region}/standard-unit-rates/"
)
TRACKER_TARIFF_URL = (
    "{base}/products/{product}/electricity-tariffs/"
    "E-1R-{product}-{region}/standard-unit-rates/"
)

# Minimum half-hourly slots required to include "today" in forecasts.
# A full day has 48 slots; partial days produce misleading averages
# because late-evening slots tend to be cheap.
MIN_TODAY_SLOTS = 48

# Tracker rate clamp bounds (p/kWh)
TRACKER_MIN_RATE = 0.0
TRACKER_MAX_RATE = 100.0

PLATFORMS = ["sensor", "calendar"]
