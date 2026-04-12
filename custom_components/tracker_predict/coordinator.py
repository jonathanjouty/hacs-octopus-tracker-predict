"""DataUpdateCoordinator for Tracker Predict."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .calibration import (
    CalibrationModel,
    calibrate,
    compute_daily_means,
    compute_rolling_means,
    default_model,
    discover_product_code,
    fetch_octopus_rates,
)
from .const import (
    AGILE_PREDICT_URL,
    CONF_AGILE_PRODUCT_CODE,
    CONF_CALIBRATION_DAYS,
    CONF_CALIBRATION_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_TRACKER_PRODUCT_CODE,
    DEFAULT_AGILE_PRODUCT,
    DEFAULT_CALIBRATION_DAYS,
    DEFAULT_CALIBRATION_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ROLLING_WINDOW,
    DEFAULT_TRACKER_PRODUCT,
    DOMAIN,
    MIN_TODAY_SLOTS,
)

_LOGGER = logging.getLogger(__name__)
_UK_TZ = ZoneInfo("Europe/London")

STORAGE_KEY = f"{DOMAIN}.calibration"
STORAGE_VERSION = 1


@dataclass
class DayForecast:
    """Forecast for a single day."""

    date: str
    tracker_est: float
    tracker_low: float
    tracker_high: float
    confidence: str
    day_of_week: str
    agile_daily_mean: float
    slot_count: int


@dataclass
class TrackerPredictData:
    """Data returned by the coordinator."""

    forecasts: list[DayForecast] = field(default_factory=list)
    model: CalibrationModel = field(default_factory=default_model)
    last_updated: datetime | None = None
    forecast_generated_at: datetime | None = None
    stale: bool = False


class TrackerPredictCoordinator(DataUpdateCoordinator[TrackerPredictData]):
    """Coordinator that fetches Agile Predict data and transforms to Tracker estimates."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self._region: str = entry.data[CONF_REGION]
        self._poll_interval: int = entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        self._calibration_days: int = entry.options.get(
            CONF_CALIBRATION_DAYS, DEFAULT_CALIBRATION_DAYS
        )
        self._calibration_interval: int = entry.options.get(
            CONF_CALIBRATION_INTERVAL, DEFAULT_CALIBRATION_INTERVAL
        )
        self._agile_product: str | None = entry.options.get(CONF_AGILE_PRODUCT_CODE)
        self._tracker_product: str | None = entry.options.get(CONF_TRACKER_PRODUCT_CODE)

        self._resolved_tracker_product: str | None = None

        self._model: CalibrationModel = default_model(self._region)
        self._last_calibration: datetime | None = None
        self._session: ClientSession | None = None
        self._store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}.{entry.entry_id}",
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=self._poll_interval),
        )

    @property
    def session(self) -> ClientSession:
        """Get aiohttp session."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def _async_load_cached_model(self) -> bool:
        """Load cached calibration model from persistent storage.

        Returns True if a cached model was loaded.
        """
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.exception("Error loading cached calibration model")
            return False

        if not data or not isinstance(data, dict):
            return False

        try:
            calibrated_at = datetime.fromisoformat(data["calibrated_at"])
            self._model = CalibrationModel(
                slope=float(data["slope"]),
                intercept=float(data["intercept"]),
                r_squared=float(data["r_squared"]),
                calibrated_at=calibrated_at,
                sample_count=int(data["sample_count"]),
                rolling_window=int(data.get("rolling_window", DEFAULT_ROLLING_WINDOW)),
            )
            self._last_calibration = calibrated_at
            _LOGGER.info(
                "Loaded cached calibration model (slope=%.4f, intercept=%.4f, "
                "calibrated_at=%s)",
                self._model.slope,
                self._model.intercept,
                self._model.calibrated_at.isoformat(),
            )
            return True
        except (KeyError, ValueError, TypeError):
            _LOGGER.warning("Cached calibration data is invalid, ignoring")
            return False

    async def _async_save_model(self) -> None:
        """Persist the current calibration model to storage."""
        try:
            await self._store.async_save({
                "slope": self._model.slope,
                "intercept": self._model.intercept,
                "r_squared": self._model.r_squared,
                "calibrated_at": self._model.calibrated_at.isoformat(),
                "sample_count": self._model.sample_count,
                "rolling_window": self._model.rolling_window,
            })
        except Exception:
            _LOGGER.exception("Error saving calibration model to storage")

    async def _maybe_calibrate(self) -> None:
        """Run calibration if needed.

        On first run, loads cached model from storage. If the cached model is
        still within the calibration interval, skips recalibration. Falls back
        to the cached (or default) model when the API is unavailable.
        """
        now = datetime.now(timezone.utc)

        # On first run, try to load a cached model
        if self._last_calibration is None:
            await self._async_load_cached_model()

        if (
            self._last_calibration is not None
            and (now - self._last_calibration).total_seconds()
            <= self._calibration_interval * 3600
        ):
            return

        _LOGGER.debug("Running calibration for region %s", self._region)
        try:
            # Discover the active tracker product so both calibration and
            # actual-rate fetching use the same (current) product code.
            if not self._tracker_product:
                discovered = await discover_product_code(self.session, "SILVER")
                if discovered:
                    self._resolved_tracker_product = discovered
            self._model = await calibrate(
                self.session,
                self._region,
                self._calibration_days,
                self._agile_product or None,
                self._tracker_product or self._resolved_tracker_product,
            )
            self._last_calibration = now
            await self._async_save_model()
        except Exception:
            _LOGGER.exception("Calibration failed, keeping previous model")
            if self._last_calibration is None:
                self._last_calibration = now  # Don't retry immediately

    async def _fetch_agile_predict(self) -> tuple[list[dict], datetime | None]:
        """Fetch forecast from Agile Predict API.

        Returns (prices, forecast_generated_at).
        """
        url = AGILE_PREDICT_URL.format(region=self._region)
        params = {"days": "14", "high_low": "True", "forecast_count": "1"}

        async with self.session.get(url, params=params, timeout=30) as resp:
            if resp.status != 200:
                raise UpdateFailed(
                    f"Agile Predict API returned status {resp.status}"
                )
            data = await resp.json()

        if not data or not isinstance(data, list) or not data[0].get("prices"):
            raise UpdateFailed("Agile Predict API returned empty/invalid data")

        entry = data[0]
        prices = entry["prices"]

        forecast_generated_at: datetime | None = None
        created_at_str = entry.get("created_at")
        if created_at_str:
            try:
                forecast_generated_at = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        return prices, forecast_generated_at

    async def _fetch_recent_agile_actuals(self) -> dict[str, float]:
        """Fetch recent actual Agile rates for rolling mean computation.

        Returns a dict mapping date strings (YYYY-MM-DD) to daily mean rate.
        Fetches enough days to fill the model's rolling window. Non-fatal:
        returns empty dict on failure (rolling mean will use forecast values only).
        """
        agile_product = self._agile_product or DEFAULT_AGILE_PRODUCT
        try:
            rates = await fetch_octopus_rates(
                self.session, agile_product, self._region,
                days=self._model.rolling_window,
            )
            return compute_daily_means(rates)
        except Exception:
            _LOGGER.debug("Failed to fetch recent Agile actuals for rolling mean", exc_info=True)
            return {}

    def _transform_forecast(
        self,
        prices: list[dict],
        agile_actual_daily: dict[str, float] | None = None,
    ) -> list[DayForecast]:
        """Transform half-hourly Agile predictions to daily Tracker estimates.

        Uses a rolling mean of Agile daily prices as the model input, matching
        the smoothing structure of the Tracker formula.  agile_actual_daily
        should contain recent actual Agile daily means so the rolling window is
        anchored on real data rather than only forecast values.
        """
        # Group by date
        daily: dict[str, list[dict]] = {}
        for slot in prices:
            dt_str = slot.get("date_time", "")
            if not dt_str:
                continue
            date_str = dt_str[:10]
            daily.setdefault(date_str, []).append(slot)

        # Build per-date spot means for pred / low / high from the forecast
        forecast_preds: dict[str, float] = {}
        forecast_lows: dict[str, float] = {}
        forecast_highs: dict[str, float] = {}
        slot_counts: dict[str, int] = {}
        for date_str, slots in daily.items():
            agile_preds = [s["agile_pred"] for s in slots if "agile_pred" in s]
            if not agile_preds:
                continue
            agile_lows = [s.get("agile_low", s.get("agile_pred", 0)) for s in slots]
            agile_highs = [s.get("agile_high", s.get("agile_pred", 0)) for s in slots]
            forecast_preds[date_str] = sum(agile_preds) / len(agile_preds)
            forecast_lows[date_str] = sum(agile_lows) / len(agile_lows)
            forecast_highs[date_str] = sum(agile_highs) / len(agile_highs)
            slot_counts[date_str] = len(slots)

        # Compute rolling means.  The historical portion (actuals) anchors the
        # window so near-term predictions are more stable.
        actuals = agile_actual_daily or {}
        window = self._model.rolling_window
        rolling_preds = compute_rolling_means({**actuals, **forecast_preds}, window)
        rolling_lows = compute_rolling_means({**actuals, **forecast_lows}, window)
        rolling_highs = compute_rolling_means({**actuals, **forecast_highs}, window)

        now = datetime.now(_UK_TZ)
        forecasts: list[DayForecast] = []

        for date_str in sorted(forecast_preds):
            slot_count = slot_counts[date_str]

            agile_mean = forecast_preds[date_str]
            tracker_est = self._model.predict(rolling_preds[date_str])
            tracker_low = self._model.predict(rolling_lows[date_str])
            tracker_high = self._model.predict(rolling_highs[date_str])

            # Determine confidence
            try:
                forecast_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                days_ahead = (forecast_date - now).days
            except ValueError:
                days_ahead = 99

            if days_ahead <= 1:
                confidence = "high"
            elif days_ahead <= 5:
                confidence = "medium"
            else:
                confidence = "low"

            # Reduced confidence for partial days
            if slot_count < 12:
                confidence = "low"

            try:
                day_of_week = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
            except ValueError:
                day_of_week = "?"

            forecasts.append(
                DayForecast(
                    date=date_str,
                    tracker_est=round(tracker_est, 2),
                    tracker_low=round(tracker_low, 2),
                    tracker_high=round(tracker_high, 2),
                    confidence=confidence,
                    day_of_week=day_of_week,
                    agile_daily_mean=round(agile_mean, 2),
                    slot_count=slot_count,
                )
            )

        # Exclude today if it has too few slots (partial day with misleading average)
        today_str = now.strftime("%Y-%m-%d")
        today_slots = next(
            (f.slot_count for f in forecasts if f.date == today_str), None
        )
        if today_slots is not None and today_slots < MIN_TODAY_SLOTS:
            _LOGGER.debug(
                "Excluding today (%s) from forecasts: %d slots < %d minimum",
                today_str, today_slots, MIN_TODAY_SLOTS,
            )
            forecasts = [f for f in forecasts if f.date != today_str]

        return forecasts

    async def _fetch_actual_tracker_rates(self) -> dict[str, float]:
        """Fetch recent actual Tracker rates from the Octopus API.

        Returns a dict mapping date strings (YYYY-MM-DD) to value_inc_vat.
        Non-fatal: returns empty dict on failure.
        """
        product = self._tracker_product or self._resolved_tracker_product or DEFAULT_TRACKER_PRODUCT
        try:
            rates = await fetch_octopus_rates(
                self.session, product, self._region, days=2
            )
        except Exception:
            _LOGGER.debug("Failed to fetch actual Tracker rates", exc_info=True)
            return {}

        # Tracker rates are daily (one rate per day), so just take the first
        # value_inc_vat per date.
        daily: dict[str, float] = {}
        for rate in rates:
            valid_from = rate.get("valid_from", "")
            value = rate.get("value_inc_vat")
            if not valid_from or value is None:
                continue
            try:
                dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                date_str = dt.astimezone(_UK_TZ).strftime("%Y-%m-%d")
            except ValueError:
                continue
            if date_str not in daily:
                daily[date_str] = float(value)
        return daily

    def _overlay_actual_rates(
        self,
        forecasts: list[DayForecast],
        actual_rates: dict[str, float],
    ) -> list[DayForecast]:
        """Overlay actual Tracker rates onto forecasts.

        - Replaces predicted values for matching dates with actuals
        - Inserts today if it was filtered out by MIN_TODAY_SLOTS
        - Only overlays dates >= today (ignores yesterday)
        """
        if not actual_rates:
            return forecasts

        today_str = datetime.now(_UK_TZ).strftime("%Y-%m-%d")
        forecast_dates = {f.date for f in forecasts}

        # Replace predictions with actuals for existing forecast dates
        updated: list[DayForecast] = []
        for f in forecasts:
            if f.date in actual_rates and f.date >= today_str:
                rate = actual_rates[f.date]
                updated.append(DayForecast(
                    date=f.date,
                    tracker_est=round(rate, 2),
                    tracker_low=round(rate, 2),
                    tracker_high=round(rate, 2),
                    confidence="actual",
                    day_of_week=f.day_of_week,
                    agile_daily_mean=f.agile_daily_mean,
                    slot_count=f.slot_count,
                ))
            else:
                updated.append(f)

        # Insert actual rates for dates not in forecasts (e.g. today filtered out)
        for date_str, rate in actual_rates.items():
            if date_str >= today_str and date_str not in forecast_dates:
                try:
                    day_of_week = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
                except ValueError:
                    day_of_week = "?"
                updated.append(DayForecast(
                    date=date_str,
                    tracker_est=round(rate, 2),
                    tracker_low=round(rate, 2),
                    tracker_high=round(rate, 2),
                    confidence="actual",
                    day_of_week=day_of_week,
                    agile_daily_mean=0.0,
                    slot_count=0,
                ))

        updated.sort(key=lambda f: f.date)
        return updated

    async def _async_update_data(self) -> TrackerPredictData:
        """Fetch data from APIs."""
        await self._maybe_calibrate()

        try:
            prices, forecast_generated_at = await self._fetch_agile_predict()

            agile_actual_daily = await self._fetch_recent_agile_actuals()
            forecasts = self._transform_forecast(prices, agile_actual_daily)

            actual_rates = await self._fetch_actual_tracker_rates()
            forecasts = self._overlay_actual_rates(forecasts, actual_rates)

            return TrackerPredictData(
                forecasts=forecasts,
                model=self._model,
                last_updated=datetime.now(timezone.utc),
                forecast_generated_at=forecast_generated_at,
                stale=False,
            )
        except UpdateFailed:
            # If we have previous data, return it as stale
            if self.data and self.data.forecasts:
                _LOGGER.warning(
                    "Failed to update forecast, using stale data"
                )
                return TrackerPredictData(
                    forecasts=self.data.forecasts,
                    model=self._model,
                    last_updated=self.data.last_updated,
                    forecast_generated_at=self.data.forecast_generated_at,
                    stale=True,
                )
            raise
