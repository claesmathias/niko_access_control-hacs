"""Button platform for Niko Access Control — call control."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]
    async_add_entities([
        NikoAnswerButton(coordinator, serial),
        NikoRejectButton(coordinator, serial),
        NikoHangupButton(coordinator, serial),
    ])


class _NikoCallButton(ButtonEntity):
    """Base class for doorbell call-control buttons."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        self._coordinator = coordinator
        self._serial = serial
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"Niko Doorbell {serial}",
            manufacturer="Niko / Hikvision",
            model="Access Control Doorbell",
        )


class NikoAnswerButton(_NikoCallButton):
    _attr_name = "Answer call"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_btn_answer"

    async def async_press(self) -> None:
        await self._coordinator.api.answer_call(self._serial)


class NikoRejectButton(_NikoCallButton):
    _attr_name = "Reject call"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_btn_reject"

    async def async_press(self) -> None:
        await self._coordinator.api.reject_call(self._serial)


class NikoHangupButton(_NikoCallButton):
    _attr_name = "Hang up"
    _attr_icon = "mdi:phone-off"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_btn_hangup"

    async def async_press(self) -> None:
        await self._coordinator.api.hangup_call(self._serial)
