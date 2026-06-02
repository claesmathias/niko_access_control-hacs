"""Sensor platform for Niko Access Control."""
from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import CoordinatorData, NikoCoordinator


def _localise(naive: datetime | None) -> datetime | None:
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

    async_add_entities([
        NikoLastCallTimeSensor(coordinator, serial),
        NikoLastCallStatusSensor(coordinator, serial),
        NikoCallCountSensor(coordinator, serial),
        # Device info (diagnostic)
        NikoDeviceInfoSensor(coordinator, serial, "firmware_version",         "Firmware Version", "mdi:chip"),
        NikoDeviceInfoSensor(coordinator, serial, "firmware_released_date",  "Firmware Build",   "mdi:calendar"),
        # hardware_version_display falls back to firmware build date when empty;
        # unique_id_suffix="hardware_version" reuses the existing registry entry
        NikoDeviceInfoSensor(coordinator, serial, "hardware_version_display","Hardware Version", "mdi:memory",
                             unique_id_suffix="hardware_version"),
        NikoDeviceInfoSensor(coordinator, serial, "model",                   "Model",            "mdi:identifier"),
        NikoDeviceInfoSensor(coordinator, serial, "serial_number",           "Serial Number",    "mdi:barcode"),
        NikoDeviceInfoSensor(coordinator, serial, "mac_address",             "MAC Address",      "mdi:lan"),
        NikoDeviceInfoSensor(coordinator, serial, "ip_address",              "IP Address",       "mdi:ip-network"),
        NikoDeviceInfoSensor(coordinator, serial, "device_name",             "Device Name",      "mdi:doorbell"),
    ])


def _device_info(serial: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"Niko Doorbell {serial}",
        manufacturer="Niko / Hikvision",
        model="Access Control Doorbell",
    )


class _NikoBase(CoordinatorEntity[NikoCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_device_info = _device_info(serial)


class NikoLastCallTimeSensor(_NikoBase):
    _attr_name = "Last Call Time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:doorbell"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_last_call_time"

    @property
    def native_value(self) -> datetime | None:
        call = self.coordinator.data.last_call if self.coordinator.data else None
        return _localise(call.calling_datetime) if call else None

    @property
    def extra_state_attributes(self) -> dict:
        data: CoordinatorData | None = self.coordinator.data
        if not data or not data.calls:
            return {}
        return {
            "call_history": [c.as_dict() for c in data.calls],
        }


class NikoLastCallStatusSensor(_NikoBase):
    _attr_name = "Last Call Status"
    _attr_icon = "mdi:phone-check"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_last_call_status"

    @property
    def native_value(self) -> str | None:
        call = self.coordinator.data.last_call if self.coordinator.data else None
        return call.status_label if call else None


class NikoCallCountSensor(_NikoBase):
    _attr_name = "Total Calls"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_call_count"

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        return len(data.calls) if data else 0


class NikoDeviceInfoSensor(_NikoBase):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: NikoCoordinator,
        serial: str,
        attr: str,
        name: str,
        icon: str,
        unique_id_suffix: str | None = None,
    ) -> None:
        super().__init__(coordinator, serial)
        self._attr_name = name
        self._attr_icon = icon
        # unique_id_suffix lets us keep a stable registry ID while changing attr
        suffix = unique_id_suffix or attr
        self._attr_unique_id = f"{serial}_info_{suffix}"
        self._attr = attr

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if not data or not data.device_info:
            return None
        return getattr(data.device_info, self._attr, None) or None
