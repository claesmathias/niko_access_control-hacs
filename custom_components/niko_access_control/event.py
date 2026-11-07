"""Event platform for Niko Access Control — the standard HA doorbell event."""
from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import CallingInfo
from .const import CALL_STATE_RINGING, CONF_DEVICE_SERIAL, DOMAIN, SIGNAL_RING
from .coordinator import NikoCoordinator
from .entity import device_info
from .sensor import localise

EVENT_RING = "ring"

# A new call in the cloud history within this window of a detected ring is the same press.
_DEDUP_SECONDS = 120
# A call older than this when it first shows up in the history is not a new ring.
_MAX_CALL_AGE = timedelta(minutes=15)
# Snapshots are uploaded a few seconds after the press; refresh history then.
_SNAPSHOT_DELAY = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NikoDoorbellEvent(coordinator, entry.data[CONF_DEVICE_SERIAL], entry.entry_id)])


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

    def __init__(self, coordinator: NikoCoordinator, serial: str, entry_id: str) -> None:
        self._entry_id = entry_id
        self._coordinator = coordinator
        self._ring = coordinator.ring_coordinator
        self._attr_unique_id = f"{serial}_doorbell"
        self._attr_device_info: DeviceInfo = device_info(serial)
        self._was_ringing = self._ring.call_state == CALL_STATE_RINGING
        calls = coordinator.data.calls if coordinator.data else []
        self._seen_ids: set[str] = {c.calling_id for c in calls}
        self._newest = max((t for c in calls if (t := localise(c.calling_datetime))), default=None)
        self._last_fired = 0.0

    @property
    def available(self) -> bool:
        return self._ring.last_update_success or self._coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._ring.async_add_listener(self._on_call_state))
        self.async_on_remove(self._coordinator.async_add_listener(self._on_history))

    @callback
    def _fire(self, call: CallingInfo | None = None) -> None:
        self._last_fired = time.monotonic()
        attrs = {"calling_id": call.calling_id, "status": call.status_label} if call else None
        self._trigger_event(EVENT_RING, attrs)
        self.async_write_ha_state()
        # Notifications (notifications.py) listen for this.
        async_dispatcher_send(self.hass, SIGNAL_RING.format(entry_id=self._entry_id), call)

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
        """Fire for a call that is genuinely new: unseen, newer than anything
        seen so far, and recent.

        The history is merged from two cloud queries (unread / read). When one
        of them fails, or a call moves between them, the "newest" call can
        change to an older one and back. That must not count as a ring.
        """
        last = self._coordinator.data.last_call if self._coordinator.data else None
        if not last or last.calling_id in self._seen_ids:
            return
        self._seen_ids.add(last.calling_id)
        when = localise(last.calling_datetime)
        if when is None or (self._newest is not None and when <= self._newest):
            return
        self._newest = when
        if dt_util.now() - when > _MAX_CALL_AGE:
            return
        if time.monotonic() - self._last_fired > _DEDUP_SECONDS:
            self._fire(last)
