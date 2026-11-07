"""Sensor platform for Niko Access Control."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CALL_RESULTS, CALL_STATES, CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import CoordinatorData, NikoCallStatusCoordinator, NikoCoordinator
from .entity import NikoEntity


def localise(naive: datetime | None) -> datetime | None:
    """Convert a naive API datetime (device local time) to HA-aware datetime."""
    if naive is None:
        return None
    tz = dt_util.DEFAULT_TIME_ZONE
    if hasattr(tz, "localize"):  # pytz
        return tz.localize(naive)
    return naive.replace(tzinfo=tz)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]

    entities: list[SensorEntity] = [
        NikoLastCallTimeSensor(coordinator, serial),
        NikoLastCallStatusSensor(coordinator, serial),
        NikoCallsTodaySensor(coordinator, serial),
        NikoIpAddressSensor(coordinator, serial),
    ]
    if coordinator.ring_coordinator.supported:  # live call state available
        entities.append(NikoCallStateSensor(coordinator.ring_coordinator, serial))
    async_add_entities(entities)


class _NikoSensor(NikoEntity, SensorEntity):
    coordinator: NikoCoordinator

    @property
    def _data(self) -> CoordinatorData | None:
        return self.coordinator.data


class NikoLastCallTimeSensor(_NikoSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # The history list is large and changes every ring; keep it out of the recorder.
    _unrecorded_attributes = frozenset({"call_history"})

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "last_call_time")
        self._attr_unique_id = f"{serial}_last_call_time"

    @property
    def native_value(self) -> datetime | None:
        call = self._data.last_call if self._data else None
        return localise(call.calling_datetime) if call else None

    @property
    def extra_state_attributes(self) -> dict:
        if not self._data or not self._data.calls:
            return {}
        return {"call_history": [c.as_dict() for c in self._data.calls]}


class NikoLastCallStatusSensor(_NikoSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_RESULTS

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "last_call_status")
        self._attr_unique_id = f"{serial}_last_call_status"

    @property
    def native_value(self) -> str | None:
        call = self._data.last_call if self._data else None
        return call.status_label if call else None


class NikoCallsTodaySensor(_NikoSensor):
    """Rings since local midnight (from the cloud history of the last 50 calls)."""

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "calls_today")
        self._attr_unique_id = f"{serial}_calls_today"

    def _today(self) -> list:
        if not self._data:
            return []
        today = dt_util.now().date()
        return [
            c for c in self._data.calls
            if (dt := localise(c.calling_datetime)) and dt.date() == today
        ]

    @property
    def native_value(self) -> int | None:
        return len(self._today()) if self._data else None

    @property
    def extra_state_attributes(self) -> dict:
        calls = self._today()
        return {
            "answered": sum(1 for c in calls if c.is_answered),
            "missed": sum(1 for c in calls if not c.is_answered),
        }


class NikoCallStateSensor(NikoEntity, SensorEntity):
    """Live call state: idle / ringing / on call."""

    coordinator: NikoCallStatusCoordinator
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_STATES

    def __init__(self, coordinator: NikoCallStatusCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "call_state")
        self._attr_unique_id = f"{serial}_call_state"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.call_state

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator.data or {}
        return {"raw_call_status": status.get("callStatus")}


class NikoIpAddressSensor(_NikoSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "ip_address")
        self._attr_unique_id = f"{serial}_info_ip_address"

    @property
    def native_value(self) -> str | None:
        info = self._data.device_info if self._data else None
        return (info.ip_address if info else None) or None
