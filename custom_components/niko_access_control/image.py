"""Image platform for Niko Access Control – last doorbell snapshot (static)."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.util import dt as dt_util

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NikoDoorbellImage(coordinator, entry.data[CONF_DEVICE_SERIAL])])


class NikoDoorbellImage(CoordinatorEntity[NikoCoordinator], ImageEntity):
    """Image entity serving the latest doorbell snapshot."""

    _attr_name = "Last Call Snapshot"
    _attr_icon = "mdi:image"
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._serial = serial
        self._attr_unique_id = f"{serial}_last_call_snapshot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"Niko Doorbell {serial}",
            manufacturer="Niko / Hikvision",
            model="Access Control Doorbell",
        )
        self._current_pic_url: str | None = None
        self._cached_image: bytes | None = None

    @property
    def image_last_updated(self) -> datetime | None:
        call = self.coordinator.data.last_call if self.coordinator.data else None
        if not call:
            return None
        dt = call.calling_datetime
        if dt is None:
            return None
        tz = dt_util.DEFAULT_TIME_ZONE
        if hasattr(tz, "localize"):
            return tz.localize(dt)
        return dt.replace(tzinfo=tz)

    def _handle_coordinator_update(self) -> None:
        call = self.coordinator.data.last_call if self.coordinator.data else None
        new_url = call.pic_url if call else None
        if new_url != self._current_pic_url:
            self._current_pic_url = new_url
            self._cached_image = None
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        url = self._current_pic_url
        if not url:
            return None
        if self._cached_image is not None:
            return self._cached_image
        try:
            self._cached_image = await self.coordinator.api.get_picture(url)
        except Exception as err:
            _LOGGER.warning("Failed to download doorbell snapshot: %s", err)
            return None
        return self._cached_image
