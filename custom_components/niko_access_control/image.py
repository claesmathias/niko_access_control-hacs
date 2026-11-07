"""Image platform for Niko Access Control – snapshot of the last call.

Older snapshots are browsable in the Media panel (see media_source.py).
"""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CallingInfo
from .const import CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCoordinator
from .entity import NikoEntity
from .sensor import localise

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NikoLastCallImage(coordinator, entry.data[CONF_DEVICE_SERIAL])])


class NikoLastCallImage(NikoEntity, ImageEntity):
    """Snapshot captured at the most recent ring."""

    coordinator: NikoCoordinator
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        NikoEntity.__init__(self, coordinator, serial, "last_call")
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_unique_id = f"{serial}_latest_call_snapshot"

    def _call(self) -> CallingInfo | None:
        data = self.coordinator.data
        return data.last_call if data else None

    @property
    def image_last_updated(self) -> datetime | None:
        """Timestamp shown by HA as the entity state."""
        call = self._call()
        return localise(call.calling_datetime) if call else None

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

    async def async_image(self) -> bytes | None:
        call = self._call()
        if not call:
            return None
        try:
            return await self.coordinator.async_get_picture(call)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to download snapshot: %s", err)
            return None
