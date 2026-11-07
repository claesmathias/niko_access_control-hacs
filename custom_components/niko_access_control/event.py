"""Event platform for Niko Access Control — the standard HA doorbell event."""
from __future__ import annotations

import time

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import CALL_STATE_RINGING, CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCoordinator
from .entity import device_info

EVENT_RING = "ring"

# A new call in the cloud history within this window of a detected ring is the same press.
_DEDUP_SECONDS = 120
# Snapshots are uploaded a few seconds after the press; refresh history then.
_SNAPSHOT_DELAY = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NikoDoorbellEvent(coordinator, entry.data[CONF_DEVICE_SERIAL])])


class NikoDoorbellEvent(EventEntity):
    """Fires `ring` when someone presses the doorbell.

    Primary source is the 5 s live call-state poll (rising edge to "ringing").
    A new entry in the cloud call history also fires, so short presses that
    fall between two polls are not lost.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_RING]
    _attr_should_poll = False

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        self._coordinator = coordinator
        self._ring = coordinator.ring_coordinator
        self._attr_unique_id = f"{serial}_doorbell"
        self._attr_device_info: DeviceInfo = device_info(serial)
        self._was_ringing = self._ring.call_state == CALL_STATE_RINGING
        last = coordinator.data.last_call if coordinator.data else None
        self._last_call_id = last.calling_id if last else None
        self._last_fired = 0.0

    @property
    def available(self) -> bool:
        return self._ring.last_update_success or self._coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._ring.async_add_listener(self._on_call_state))
        self.async_on_remove(self._coordinator.async_add_listener(self._on_history))

    @callback
    def _fire(self, **attrs) -> None:
        self._last_fired = time.monotonic()
        self._trigger_event(EVENT_RING, attrs or None)
        self.async_write_ha_state()

    @callback
    def _on_call_state(self) -> None:
        ringing = self._ring.call_state == CALL_STATE_RINGING
        if ringing and not self._was_ringing:
            self._fire()
            self.async_on_remove(
                async_call_later(
                    self.hass, _SNAPSHOT_DELAY,
                    lambda _now: self.hass.async_create_task(self._coordinator.async_request_refresh()),
                )
            )
        self._was_ringing = ringing
        self.async_write_ha_state()  # availability may have changed

    @callback
    def _on_history(self) -> None:
        last = self._coordinator.data.last_call if self._coordinator.data else None
        if not last or last.calling_id == self._last_call_id:
            return
        self._last_call_id = last.calling_id
        if time.monotonic() - self._last_fired > _DEDUP_SECONDS:
            self._fire(calling_id=last.calling_id, status=last.status_label)
