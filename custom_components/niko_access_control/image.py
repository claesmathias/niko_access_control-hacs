"""Image platform for Niko Access Control – doorbell snapshots."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import CallingInfo
from .const import CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]

    # Stable entity that always shows the latest call — registered once, never removed
    async_add_entities([NikoLatestCallImage(coordinator, serial)])

    added_slots: set[int] = set()

    def _add_new_slots() -> None:
        calls = coordinator.data.calls if coordinator.data else []
        new_entities = [
            NikoCallImage(coordinator, serial, slot)
            for slot in range(len(calls))
            if slot not in added_slots
        ]
        for e in new_entities:
            added_slots.add(e._slot)
        if new_entities:
            async_add_entities(new_entities)

    _add_new_slots()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_slots))


def _localise(naive: datetime | None) -> datetime | None:
    if naive is None:
        return None
    tz = dt_util.DEFAULT_TIME_ZONE
    if hasattr(tz, "localize"):
        return tz.localize(naive)
    return naive.replace(tzinfo=tz)


def _device_info(serial: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"Niko Doorbell {serial}",
        manufacturer="Niko / Hikvision",
        model="Access Control Doorbell",
    )


class NikoCallImage(CoordinatorEntity[NikoCoordinator], ImageEntity):
    """Image entity for a single position in the call history (slot 0 = most recent)."""

    _attr_has_entity_name = True
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: NikoCoordinator, serial: str, slot: int) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._serial = serial
        self._slot = slot

        # Slot 0 uses unpadded id to resurrect existing registry entry; 1-19 are zero-padded
        if slot == 0:
            self._attr_unique_id = f"{serial}_call_history_0"
        else:
            self._attr_unique_id = f"{serial}_call_history_{slot:02d}"

        self._attr_device_info = _device_info(serial)
        self._pic_url: str | None = None
        self._cached: bytes | None = None

    @property
    def name(self) -> str:
        call = self._call()
        if not call:
            return "Last Call Snapshot" if self._slot == 0 else f"Call {self._slot + 1}"
        return "Answered" if call.is_answered else "Missed"

    @property
    def icon(self) -> str:
        call = self._call()
        if not call:
            return "mdi:doorbell-video"
        return "mdi:phone-check" if call.is_answered else "mdi:phone-missed"

    # ── data helpers ──────────────────────────────────────────────────────────

    def _call(self) -> CallingInfo | None:
        data = self.coordinator.data
        if not data or self._slot >= len(data.calls):
            return None
        return data.calls[self._slot]

    @property
    def image_last_updated(self) -> datetime | None:
        """Timestamp shown by HA as the entity state ("X ago")."""
        return _localise(self._call().calling_datetime) if self._call() else None

    @property
    def extra_state_attributes(self) -> dict:
        call = self._call()
        if not call:
            return {}
        return {
            "calling_time": call.calling_time,
            "status": call.status_label,
            "calling_id": call.calling_id,
        }

    # ── image fetch ───────────────────────────────────────────────────────────

    def _handle_coordinator_update(self) -> None:
        call = self._call()
        new_url = call.pic_url if call else None
        if new_url != self._pic_url:
            self._pic_url = new_url
            self._cached = None
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        url = self._pic_url
        if not url:
            return None
        if self._cached is not None:
            return self._cached
        try:
            self._cached = await self.coordinator.api.get_picture(url)
        except Exception as err:
            _LOGGER.warning("Slot %d: failed to download snapshot: %s", self._slot, err)
            return None
        return self._cached


class NikoLatestCallImage(CoordinatorEntity[NikoCoordinator], ImageEntity):
    """Stable image entity that always shows the most recent call snapshot."""

    _attr_has_entity_name = True
    _attr_content_type = "image/jpeg"
    _attr_name = "Last Call"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_unique_id = f"{serial}_latest_call_snapshot"
        self._attr_device_info = _device_info(serial)
        self._pic_url: str | None = None
        self._cached: bytes | None = None

    def _call(self) -> CallingInfo | None:
        data = self.coordinator.data
        return data.calls[0] if data and data.calls else None

    @property
    def icon(self) -> str:
        call = self._call()
        if not call:
            return "mdi:doorbell-video"
        return "mdi:phone-check" if call.is_answered else "mdi:phone-missed"

    @property
    def image_last_updated(self) -> datetime | None:
        call = self._call()
        return _localise(call.calling_datetime) if call else None

    @property
    def extra_state_attributes(self) -> dict:
        call = self._call()
        if not call:
            return {}
        return {
            "calling_time": call.calling_time,
            "status": call.status_label,
            "calling_id": call.calling_id,
        }

    def _handle_coordinator_update(self) -> None:
        call = self._call()
        new_url = call.pic_url if call else None
        if new_url != self._pic_url:
            self._pic_url = new_url
            self._cached = None
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        url = self._pic_url
        if not url:
            return None
        if self._cached is not None:
            return self._cached
        try:
            self._cached = await self.coordinator.api.get_picture(url)
        except Exception as err:
            _LOGGER.warning("Latest call: failed to download snapshot: %s", err)
            return None
        return self._cached
