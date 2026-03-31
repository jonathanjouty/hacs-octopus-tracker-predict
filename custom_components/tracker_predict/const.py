"""Constants for the Tracker Predict integration."""

DOMAIN = "tracker_predict"

CONF_REGION = "region"
CONF_POLL_INTERVAL = "poll_interval_minutes"
CONF_CALIBRATION_DAYS = "calibration_days"
CONF_CALIBRATION_INTERVAL = "calibration_interval_hours"
CONF_CHEAP_THRESHOLD_PERCENTILE = "cheap_threshold_percentile"
CONF_AGILE_PRODUCT_CODE = "agile_product_code"
CONF_TRACKER_PRODUCT_CODE = "tracker_product_code"

DEFAULT_REGION = "A"
DEFAULT_POLL_INTERVAL = 60
DEFAULT_CALIBRATION_DAYS = 60
DEFAULT_CALIBRATION_INTERVAL = 168  # 7 days in hours
DEFAULT_CHEAP_THRESHOLD_PERCENTILE = 20

# Fallback linear model — overall average used when region lookup fails
DEFAULT_SLOPE = 0.56
DEFAULT_INTERCEPT = 12.75

# Per-region default calibration (slope, intercept).
# All regions currently share the same fallback values. The quarterly
# recalibrate.yml workflow will differentiate them with real API data.
DEFAULT_CALIBRATION: dict[str, tuple[float, float]] = {
    "A": (0.56, 12.75),
    "B": (0.56, 12.75),
    "C": (0.56, 12.75),
    "D": (0.56, 12.75),
    "E": (0.56, 12.75),
    "F": (0.56, 12.75),
    "G": (0.56, 12.75),
    "H": (0.56, 12.75),
    "J": (0.56, 12.75),
    "K": (0.56, 12.75),
    "L": (0.56, 12.75),
    "M": (0.56, 12.75),
    "N": (0.56, 12.75),
    "P": (0.56, 12.75),
}

# Default product codes — Tracker is no longer listed in the Octopus products
# API but the tariff endpoints still work with known product codes.
DEFAULT_AGILE_PRODUCT = "AGILE-24-10-01"
DEFAULT_TRACKER_PRODUCT = "SILVER-25-09-02"

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
