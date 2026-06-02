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
from .const import CONF_DEVICE_SERIAL, DOMAIN, HISTORY_SLOTS
from .coordinator import NikoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]
    async_add_entities(
        [NikoCallImage(coordinator, serial, slot) for slot in range(HISTORY_SLOTS)]
    )


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

    # ── dynamic name: emoji + date so the name itself is informative without
    #    duplicating the status text that HA shows via the card state label ──

    @property
    def name(self) -> str:
        call = self._call()
        if not call:
            return "Last Call Snapshot" if self._slot == 0 else f"Call {self._slot + 1}"
        icon = "✅" if call.is_answered else "❌"
        dt = call.calling_datetime
        if dt:
            date_str = f"{dt.day} {dt.strftime('%b')} {dt.strftime('%H:%M')}"
        else:
            date_str = (call.calling_time or "")[:16]
        return f"{icon} {date_str}"

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
