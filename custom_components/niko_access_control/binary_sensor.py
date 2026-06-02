"""Binary sensor platform for Niko Access Control."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NikoOnlineSensor(coordinator, entry.data[CONF_DEVICE_SERIAL])])


class NikoOnlineSensor(CoordinatorEntity[NikoCoordinator], BinarySensorEntity):
    """Reports whether the doorbell is reachable via HikConnect."""

    _attr_has_entity_name = True
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-check"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_online"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"Niko Doorbell {serial}",
            manufacturer="Niko / Hikvision",
            model="Access Control Doorbell",
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        # If we got any calls back the cloud is reachable; online field adds ISAPI reachability
        if data.online is not None:
            return data.online
        return len(data.calls) > 0
