"""Sensor platform for Tracker Predict."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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


class TrackerPredictTodaySensor(
    CoordinatorEntity[TrackerPredictCoordinator], SensorEntity
):
    """Sensor showing today's predicted Tracker rate."""

    _attr_native_unit_of_measurement = "p/kWh"
    _attr_icon = "mdi:currency-gbp"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._attr_unique_id = f"tracker_predict_{region}_today"
        self._attr_name = f"Tracker Predict Today ({region})"

    @property
    def native_value(self) -> float | None:
        """Return today's predicted rate."""
        forecast = _get_today_forecast(self.coordinator.data)
        return forecast.tracker_est if forecast else None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        data = self.coordinator.data
        forecast = _get_today_forecast(data)
        if not forecast or not data:
            return {}

        attrs = {
            "forecast_date": forecast.date,
            "tracker_estimate": forecast.tracker_est,
            "tracker_low": forecast.tracker_low,
            "tracker_high": forecast.tracker_high,
            "confidence": forecast.confidence,
            "agile_daily_mean": forecast.agile_daily_mean,
            "model_r_squared": round(data.model.r_squared, 4),
            "stale": data.stale,
        }
        if data.last_updated:
            attrs["last_updated"] = data.last_updated.isoformat()
        return attrs


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
