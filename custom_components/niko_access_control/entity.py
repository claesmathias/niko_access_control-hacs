"""Shared base entity for Niko Access Control."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN


def device_info(serial: str) -> DeviceInfo:
    """Device link only — full details are written by __init__._sync_device."""
    return DeviceInfo(identifiers={(DOMAIN, serial)})


class NikoEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """Base entity: entity naming via translation keys, shared device link."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_translation_key = key
        self._attr_device_info = device_info(serial)
