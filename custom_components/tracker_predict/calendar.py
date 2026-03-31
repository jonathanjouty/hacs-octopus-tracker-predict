"""Calendar platform for Tracker Predict."""

from __future__ import annotations

from datetime import date, datetime, timezone

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_REGION, DOMAIN
from .coordinator import DayForecast, TrackerPredictCoordinator, TrackerPredictData
from .sensor import _make_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tracker Predict calendar entities."""
    coordinator: TrackerPredictCoordinator = hass.data[DOMAIN][entry.entry_id]
    region = entry.data[CONF_REGION]
    async_add_entities([TrackerPredictCalendar(coordinator, entry, region)])


def _rank_label(rank: int, total: int) -> str:
    """Return a human-readable rank label."""
    if rank == 1:
        return "cheapest"
    if rank == 2:
        return "2nd cheapest"
    if rank == 3:
        return "3rd cheapest"
    return f"#{rank} of {total}"


def _build_event(forecast: DayForecast, rank: int, total: int) -> CalendarEvent:
    """Build a CalendarEvent for a forecast day."""
    day = date.fromisoformat(forecast.date)
    summary = f"Tracker: {forecast.tracker_est:.1f}p/kWh ({_rank_label(rank, total)})"
    description = (
        f"Range: {forecast.tracker_low:.1f}–{forecast.tracker_high:.1f}p/kWh\n"
        f"Confidence: {forecast.confidence}\n"
        f"Rank: {rank} of {total}"
    )
    return CalendarEvent(start=day, end=day, summary=summary, description=description)


def _events_from_data(data: TrackerPredictData) -> list[tuple[CalendarEvent, str]]:
    """Build (CalendarEvent, date_str) pairs for all forecast days, ranked by price."""
    if not data or not data.forecasts:
        return []
    sorted_by_price = sorted(data.forecasts, key=lambda f: f.tracker_est)
    rank_map = {f.date: i + 1 for i, f in enumerate(sorted_by_price)}
    total = len(data.forecasts)
    return [
        (_build_event(f, rank_map[f.date], total), f.date)
        for f in data.forecasts
    ]


class TrackerPredictCalendar(
    CoordinatorEntity[TrackerPredictCoordinator], CalendarEntity
):
    """Calendar showing Tracker rate predictions as all-day events."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: TrackerPredictCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._region = region
        self._attr_unique_id = f"tracker_predict_{region}_calendar"
        self._attr_name = f"Tracker Predict ({region})"

    @property
    def device_info(self) -> DeviceInfo:
        return _make_device_info(self._region)

    @property
    def event(self) -> CalendarEvent | None:
        """Return today's event, or the next upcoming event if today has no forecast."""
        data = self.coordinator.data
        if not data or not data.forecasts:
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for event, event_date in _events_from_data(data):
            if event_date >= today:
                return event
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return events within the requested time range."""
        data = self.coordinator.data
        if not data or not data.forecasts:
            return []
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        return [
            event
            for event, event_date in _events_from_data(data)
            if start_str <= event_date < end_str
        ]
