"""Sensor platform for Tracker Predict."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_REGION, DOMAIN
from .coordinator import DayForecast, TrackerPredictCoordinator, TrackerPredictData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tracker Predict sensor entities."""
    coordinator: TrackerPredictCoordinator = hass.data[DOMAIN][entry.entry_id]
    region = entry.data[CONF_REGION]

    entities: list[SensorEntity] = [
        TrackerPredictTodaySensor(coordinator, entry, region),
        TrackerPredictForecastSensor(coordinator, entry, region),
        TrackerPredictCheapestSensor(coordinator, entry, region, window=5),
        TrackerPredictCheapestSensor(coordinator, entry, region, window=10),
        TrackerPredictLastUpdatedSensor(coordinator, entry, region),
    ]
    async_add_entities(entities)


def _get_today_forecast(data: TrackerPredictData | None) -> DayForecast | None:
    """Get the forecast for today."""
    if not data or not data.forecasts:
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for f in data.forecasts:
        if f.date == today:
            return f
    # If today not found, return first forecast (may be partial day)
    return data.forecasts[0] if data.forecasts else None


def _ranked_forecasts(data: TrackerPredictData | None) -> list[dict]:
    """Return forecasts as dicts with rank assigned."""
    if not data or not data.forecasts:
        return []
    sorted_by_price = sorted(data.forecasts, key=lambda f: f.tracker_est)
    rank_map = {f.date: i + 1 for i, f in enumerate(sorted_by_price)}

    result = []
    for f in data.forecasts:
        result.append(
            {
                "date": f.date,
                "tracker_est": f.tracker_est,
                "tracker_low": f.tracker_low,
                "tracker_high": f.tracker_high,
                "confidence": f.confidence,
                "day_of_week": f.day_of_week,
                "rank": rank_map[f.date],
            }
        )
    return result


def _make_device_info(region: str) -> DeviceInfo:
    """Shared DeviceInfo for all entities in this region."""
    return DeviceInfo(
        identifiers={(DOMAIN, region)},
        name=f"Tracker Predict ({region})",
        manufacturer="Octopus Energy / Agile Predict",
        model="Tracker Rate Predictor",
        entry_type=DeviceEntryType.SERVICE,
    )


class TrackerPredictTodaySensor(
    CoordinatorEntity[TrackerPredictCoordinator], SensorEntity
):
    """Sensor showing today's rank among all forecast days (1 = cheapest)."""

    _attr_icon = "mdi:podium"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._attr_unique_id = f"tracker_predict_{region}_today_rank"
        self._attr_name = f"Tracker Predict Today Rank ({region})"

    @property
    def device_info(self) -> DeviceInfo:
        return _make_device_info(self._region)

    @property
    def native_value(self) -> int | None:
        """Return today's rank (1 = cheapest day in the forecast window)."""
        data = self.coordinator.data
        if not data or not data.forecasts:
            return None
        today_forecast = _get_today_forecast(data)
        if not today_forecast:
            return None
        sorted_by_price = sorted(data.forecasts, key=lambda f: f.tracker_est)
        for rank, f in enumerate(sorted_by_price, 1):
            if f.date == today_forecast.date:
                return rank
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        data = self.coordinator.data
        forecast = _get_today_forecast(data)
        if not forecast or not data:
            return {}
        return {
            "confidence": forecast.confidence,
            "days_in_window": len(data.forecasts),
            "stale": data.stale,
        }


class TrackerPredictForecastSensor(
    CoordinatorEntity[TrackerPredictCoordinator], SensorEntity
):
    """Sensor showing the full forecast with ranked days."""

    _attr_icon = "mdi:chart-line"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._attr_unique_id = f"tracker_predict_{region}_forecast"
        self._attr_name = f"Tracker Predict Forecast ({region})"

    @property
    def device_info(self) -> DeviceInfo:
        return _make_device_info(self._region)

    @property
    def native_value(self) -> int | None:
        """Return number of days in forecast."""
        data = self.coordinator.data
        if not data or not data.forecasts:
            return None
        return len(data.forecasts)

    @property
    def extra_state_attributes(self) -> dict:
        """Return forecast list and model info."""
        data = self.coordinator.data
        if not data:
            return {}

        attrs: dict = {
            "forecast": _ranked_forecasts(data),
            "model_slope": round(data.model.slope, 4),
            "model_intercept": round(data.model.intercept, 4),
            "model_r_squared": round(data.model.r_squared, 4),
            "model_last_calibrated": data.model.calibrated_at.isoformat(),
            "stale": data.stale,
        }
        if data.forecast_generated_at:
            attrs["forecast_generated_at"] = data.forecast_generated_at.isoformat()
        return attrs


class TrackerPredictCheapestSensor(
    CoordinatorEntity[TrackerPredictCoordinator], SensorEntity
):
    """Sensor showing the cheapest predicted day within a window."""

    _attr_icon = "mdi:cash-minus"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
        window: int,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._window = window
        self._attr_unique_id = f"tracker_predict_{region}_cheapest_{window}d"
        self._attr_name = f"Tracker Predict Cheapest {window}d ({region})"

    @property
    def device_info(self) -> DeviceInfo:
        return _make_device_info(self._region)

    def _get_cheapest(self) -> DayForecast | None:
        """Find cheapest day within window."""
        data = self.coordinator.data
        if not data or not data.forecasts:
            return None
        window_forecasts = data.forecasts[: self._window]
        if not window_forecasts:
            return None
        return min(window_forecasts, key=lambda f: f.tracker_est)

    @property
    def native_value(self) -> str | None:
        """Return the date of the cheapest day."""
        cheapest = self._get_cheapest()
        return cheapest.date if cheapest else None

    @property
    def extra_state_attributes(self) -> dict:
        """Return details of the cheapest day."""
        cheapest = self._get_cheapest()
        if not cheapest:
            return {}

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            cheapest_dt = datetime.strptime(cheapest.date, "%Y-%m-%d")
            days_away = (cheapest_dt - today_dt).days
        except ValueError:
            days_away = -1

        return {
            "date": cheapest.date,
            "day_of_week": cheapest.day_of_week,
            "tracker_est": cheapest.tracker_est,
            "tracker_low": cheapest.tracker_low,
            "tracker_high": cheapest.tracker_high,
            "days_away": days_away,
            "confidence": cheapest.confidence,
        }


class TrackerPredictLastUpdatedSensor(
    CoordinatorEntity[TrackerPredictCoordinator], SensorEntity
):
    """Sensor showing when the coordinator last successfully fetched data."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._attr_unique_id = f"tracker_predict_{region}_forecast_generated_at"
        self._attr_name = f"Tracker Predict Forecast Generated ({region})"

    @property
    def device_info(self) -> DeviceInfo:
        return _make_device_info(self._region)

    @property
    def native_value(self):
        """Return when AgilePredict last generated the forecast."""
        data = self.coordinator.data
        return data.forecast_generated_at if data else None
