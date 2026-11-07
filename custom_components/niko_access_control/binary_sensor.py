"""Binary sensor platform for Niko Access Control."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CALL_STATE_RINGING, CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCallStatusCoordinator, NikoCoordinator
from .entity import NikoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]
    entities: list[BinarySensorEntity] = [NikoOnlineSensor(coordinator, serial)]
    if coordinator.ring_coordinator.supported:
        entities.append(NikoRingingSensor(coordinator.ring_coordinator, serial))
    async_add_entities(entities)


class NikoOnlineSensor(NikoEntity, BinarySensorEntity):
    """Whether the doorbell is online in the HikConnect cloud."""

    coordinator: NikoCoordinator
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "online")
        self._attr_unique_id = f"{serial}_online"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return data.online if data else None


class NikoRingingSensor(NikoEntity, BinarySensorEntity):
    """On while the doorbell is ringing (not yet answered or rejected)."""

    coordinator: NikoCallStatusCoordinator

    def __init__(self, coordinator: NikoCallStatusCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "ringing")
        self._attr_unique_id = f"{serial}_ringing"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.call_state
        return None if state is None else state == CALL_STATE_RINGING
