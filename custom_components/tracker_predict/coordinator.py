"""DataUpdateCoordinator for Tracker Predict."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .calibration import CalibrationModel, calibrate, default_model
from .const import (
    AGILE_PREDICT_URL,
    CONF_AGILE_PRODUCT_CODE,
    CONF_CALIBRATION_DAYS,
    CONF_CALIBRATION_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_TRACKER_PRODUCT_CODE,
    DEFAULT_CALIBRATION_DAYS,
    DEFAULT_CALIBRATION_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

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

        self._model: CalibrationModel = default_model()
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
            self._model = await calibrate(
                self.session,
                self._region,
                self._calibration_days,
                self._agile_product or None,
                self._tracker_product or None,
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

    def _transform_forecast(self, prices: list[dict]) -> list[DayForecast]:
        """Transform half-hourly Agile predictions to daily Tracker estimates."""
        # Group by date
        daily: dict[str, list[dict]] = {}
        for slot in prices:
            dt_str = slot.get("date_time", "")
            if not dt_str:
                continue
            date_str = dt_str[:10]
            daily.setdefault(date_str, []).append(slot)

        now = datetime.now(timezone.utc)
        forecasts: list[DayForecast] = []

        for date_str in sorted(daily):
            slots = daily[date_str]
            slot_count = len(slots)

            agile_preds = [s["agile_pred"] for s in slots if "agile_pred" in s]
            agile_lows = [s.get("agile_low", s.get("agile_pred", 0)) for s in slots]
            agile_highs = [s.get("agile_high", s.get("agile_pred", 0)) for s in slots]

            if not agile_preds:
                continue

            agile_mean = sum(agile_preds) / len(agile_preds)
            agile_low_mean = sum(agile_lows) / len(agile_lows)
            agile_high_mean = sum(agile_highs) / len(agile_highs)

            tracker_est = self._model.predict(agile_mean)
            tracker_low = self._model.predict(agile_low_mean)
            tracker_high = self._model.predict(agile_high_mean)

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

        # Assign ranks (by tracker_est ascending)
        return forecasts

    async def _async_update_data(self) -> TrackerPredictData:
        """Fetch data from APIs."""
        await self._maybe_calibrate()

        try:
            prices, forecast_generated_at = await self._fetch_agile_predict()
            forecasts = self._transform_forecast(prices)

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
