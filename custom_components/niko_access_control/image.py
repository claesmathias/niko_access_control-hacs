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

HISTORY_SLOTS = 20  # one image entity per slot


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]

    # slot 0 keeps unique_id "last_call_snapshot" for backward compatibility
    entities = [NikoCallImage(coordinator, serial, 0)]
    for slot in range(1, HISTORY_SLOTS):
        entities.append(NikoCallImage(coordinator, serial, slot))

    async_add_entities(entities)


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
    """Image entity for a single position in the call history ring buffer."""

    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: NikoCoordinator, serial: str, slot: int) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._serial = serial
        self._slot = slot
        # slot 0 reuses the existing "last_call_snapshot" registry entry
        self._attr_unique_id = (
            f"{serial}_last_call_snapshot" if slot == 0
            else f"{serial}_call_history_{slot}"
        )
        self._attr_device_info = _device_info(serial)
        self._pic_url: str | None = None
        self._cached: bytes | None = None

    # ── dynamic name ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        call = self._call()
        if not call:
            return "Last Call Snapshot" if self._slot == 0 else f"Call {self._slot + 1}"
        dt = _localise(call.calling_datetime)
        date_str = dt.strftime("%-d %b  %H:%M") if dt else call.calling_time[-8:-3]
        icon = "✅" if call.is_answered else "❌"
        return f"{icon}  {date_str}"

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
            "message": call.calling_message,
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
