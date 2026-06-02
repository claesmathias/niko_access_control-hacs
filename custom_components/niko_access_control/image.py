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

HISTORY_SLOTS = 5  # number of per-call image entities to expose


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]

    entities: list[ImageEntity] = [
        NikoDoorbellImage(coordinator, serial, 0, latest=True),
    ]
    for slot in range(HISTORY_SLOTS):
        entities.append(NikoDoorbellImage(coordinator, serial, slot, latest=False))

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


class NikoDoorbellImage(CoordinatorEntity[NikoCoordinator], ImageEntity):
    """Image entity for a single call slot in the call history."""

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: NikoCoordinator,
        serial: str,
        slot: int,
        latest: bool,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._serial = serial
        self._slot = slot
        self._latest = latest

        if latest:
            self._attr_name = "Last Call Snapshot"
            self._attr_unique_id = f"{serial}_last_call_snapshot"
            self._attr_icon = "mdi:doorbell-video"
        else:
            self._attr_name = f"Call History {slot + 1}"
            self._attr_unique_id = f"{serial}_call_history_{slot}"
            self._attr_icon = "mdi:history"

        self._attr_device_info = _device_info(serial)
        self._pic_url: str | None = None
        self._cached: bytes | None = None

    def _call(self) -> CallingInfo | None:
        data = self.coordinator.data
        if not data or not data.calls:
            return None
        idx = 0 if self._latest else self._slot
        return data.calls[idx] if idx < len(data.calls) else None

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
            _LOGGER.warning("Slot %d: failed to download snapshot: %s", self._slot, err)
            return None
        return self._cached
