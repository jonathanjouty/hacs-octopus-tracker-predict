"""Shared test fixtures and HA module mocks for Tracker Predict tests."""

import sys
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest


# --- Mock homeassistant modules ---
# These need to be set up before any custom_components imports.


class _FakeDataUpdateCoordinator:
    """Stand-in for homeassistant DataUpdateCoordinator."""
    def __init__(self, *args, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls


class _FakeEntity:
    """Common base for all fake entities."""
    _attr_native_unit_of_measurement = None
    _attr_icon = None
    _attr_unique_id = None
    _attr_name = None

    def __init__(self, *args, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls


class _FakeCoordinatorEntity(_FakeEntity):
    """Stand-in for CoordinatorEntity."""
    pass


class _FakeSensorEntity(_FakeEntity):
    """Stand-in for SensorEntity."""
    pass


class _FakeBinarySensorEntity(_FakeEntity):
    """Stand-in for BinarySensorEntity."""
    pass


class _FakeCalendarEntity(_FakeEntity):
    """Stand-in for CalendarEntity."""
    pass


@dataclass
class _FakeCalendarEvent:
    """Stand-in for CalendarEvent."""
    start: object
    end: object
    summary: str
    description: str | None = None
    location: str | None = None
    uid: str | None = None


class _FakeDeviceEntryType:
    """Stand-in for DeviceEntryType enum."""
    SERVICE = "service"


class _FakeConfigFlow:
    """Stand-in for ConfigFlow."""
    VERSION = 1

    def __init__(self, *args, **kwargs):
        pass


class _FakeOptionsFlow:
    """Stand-in for OptionsFlow."""
    def __init__(self, *args, **kwargs):
        pass


# Build module mocks
_update_coord_mock = MagicMock()
_update_coord_mock.DataUpdateCoordinator = _FakeDataUpdateCoordinator
_update_coord_mock.CoordinatorEntity = _FakeCoordinatorEntity
_update_coord_mock.UpdateFailed = type("UpdateFailed", (Exception,), {})

_sensor_mock = MagicMock()
_sensor_mock.SensorEntity = _FakeSensorEntity

_binary_sensor_mock = MagicMock()
_binary_sensor_mock.BinarySensorEntity = _FakeBinarySensorEntity

_entity_platform_mock = MagicMock()
_entity_platform_mock.CoordinatorEntity = _FakeCoordinatorEntity
_entity_platform_mock.AddEntitiesCallback = MagicMock

_config_entries_mock = MagicMock()
_config_entries_mock.ConfigEntry = MagicMock
_config_entries_mock.ConfigFlow = _FakeConfigFlow
_config_entries_mock.ConfigFlowResult = dict
_config_entries_mock.OptionsFlow = _FakeOptionsFlow

_vol_mock = MagicMock()
_vol_mock.Schema = lambda x: x
_vol_mock.Required = lambda key, **kwargs: key
_vol_mock.Optional = lambda key, **kwargs: key
_vol_mock.In = lambda x: x
_vol_mock.All = lambda *args: args[-1] if args else None
_vol_mock.Range = lambda **kwargs: int

_calendar_mock = MagicMock()
_calendar_mock.CalendarEntity = _FakeCalendarEntity
_calendar_mock.CalendarEvent = _FakeCalendarEvent

_device_registry_mock = MagicMock()
_device_registry_mock.DeviceEntryType = _FakeDeviceEntryType

_entity_mock = MagicMock()
_entity_mock.DeviceInfo = dict  # DeviceInfo is a TypedDict; dict is a fine stand-in

_MOCKED_MODULES = {
    "homeassistant": MagicMock(),
    "homeassistant.config_entries": _config_entries_mock,
    "homeassistant.core": MagicMock(),
    "homeassistant.helpers": MagicMock(),
    "homeassistant.helpers.aiohttp_client": MagicMock(),
    "homeassistant.helpers.update_coordinator": _update_coord_mock,
    "homeassistant.helpers.entity_platform": _entity_platform_mock,
    "homeassistant.helpers.entity": _entity_mock,
    "homeassistant.helpers.device_registry": _device_registry_mock,
    "homeassistant.components": MagicMock(),
    "homeassistant.components.sensor": _sensor_mock,
    "homeassistant.components.binary_sensor": _binary_sensor_mock,
    "homeassistant.components.calendar": _calendar_mock,
    "voluptuous": _vol_mock,
}

for mod_name, mock_obj in _MOCKED_MODULES.items():
    sys.modules.setdefault(mod_name, mock_obj)


# --- Fixtures ---


@pytest.fixture
def sample_agile_predict_response():
    """Sample Agile Predict API response with 3 days of data."""
    prices = []
    # Day 1: 2026-03-25 (partial, 16 slots) — moderate prices
    for i in range(16):
        hour = 16 + i // 2
        minute = "00" if i % 2 == 0 else "30"
        prices.append({
            "date_time": f"2026-03-25T{hour:02d}:{minute}:00Z",
            "agile_pred": 30.0 + i * 0.5,
            "agile_low": 28.0 + i * 0.5,
            "agile_high": 32.0 + i * 0.5,
        })
    # Day 2: 2026-03-26 (full, 48 slots) — cheap day
    for i in range(48):
        hour = i // 2
        minute = "00" if i % 2 == 0 else "30"
        prices.append({
            "date_time": f"2026-03-26T{hour:02d}:{minute}:00Z",
            "agile_pred": 15.0 + i * 0.2,
            "agile_low": 13.0 + i * 0.2,
            "agile_high": 17.0 + i * 0.2,
        })
    # Day 3: 2026-03-27 (full, 48 slots) — expensive day
    for i in range(48):
        hour = i // 2
        minute = "00" if i % 2 == 0 else "30"
        prices.append({
            "date_time": f"2026-03-27T{hour:02d}:{minute}:00Z",
            "agile_pred": 40.0 + i * 0.3,
            "agile_low": 38.0 + i * 0.3,
            "agile_high": 42.0 + i * 0.3,
        })
    return [{
        "name": "Region | A 2026-03-25 16:15",
        "created_at": "2026-03-25T16:20:00Z",
        "prices": prices,
    }]
