"""Binary sensor platform for Tracker Predict."""

from __future__ import annotations

import statistics

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHEAP_THRESHOLD_PERCENTILE,
    CONF_REGION,
    DEFAULT_CHEAP_THRESHOLD_PERCENTILE,
    DOMAIN,
)
from .coordinator import TrackerPredictCoordinator
from .sensor import _get_today_forecast, _make_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tracker Predict binary sensor entities."""
    coordinator: TrackerPredictCoordinator = hass.data[DOMAIN][entry.entry_id]
    region = entry.data[CONF_REGION]

    async_add_entities(
        [TrackerPredictCheapTodaySensor(coordinator, entry, region)]
    )


class TrackerPredictCheapTodaySensor(
    CoordinatorEntity[TrackerPredictCoordinator], BinarySensorEntity
):
    """Binary sensor: on if today is a cheap day relative to the forecast window."""

    _attr_icon = "mdi:cash-check"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._percentile = entry.options.get(
            CONF_CHEAP_THRESHOLD_PERCENTILE, DEFAULT_CHEAP_THRESHOLD_PERCENTILE
        )
        self._attr_unique_id = f"tracker_predict_{region}_cheap_today"
        self._attr_name = f"Tracker Predict Cheap Today ({region})"

    @property
    def device_info(self) -> DeviceInfo:
        return _make_device_info(self._region)

    def _compute_threshold(self) -> float | None:
        """Compute the threshold rate at the configured percentile."""
        data = self.coordinator.data
        if not data or not data.forecasts:
            return None
        rates = sorted(f.tracker_est for f in data.forecasts)
        if not rates:
            return None
        # Use quantiles to find the threshold
        if len(rates) < 2:
            return rates[0]
        return statistics.quantiles(rates, n=100)[
            min(self._percentile - 1, len(statistics.quantiles(rates, n=100)) - 1)
        ]

    @property
    def is_on(self) -> bool | None:
        """Return True if today is a cheap day."""
        today = _get_today_forecast(self.coordinator.data)
        threshold = self._compute_threshold()
        if today is None or threshold is None:
            return None
        return today.tracker_est <= threshold

    @property
    def extra_state_attributes(self) -> dict:
        """Return threshold and estimate info."""
        today = _get_today_forecast(self.coordinator.data)
        threshold = self._compute_threshold()
        attrs: dict = {}
        if threshold is not None:
            attrs["threshold"] = round(threshold, 2)
        if today is not None:
            attrs["tracker_est"] = today.tracker_est
        return attrs
