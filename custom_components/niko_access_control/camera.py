"""Camera platform for Niko Access Control – last doorbell snapshot."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.camera import Camera, CameraEntityFeature
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
    async_add_entities([NikoSnapshotCamera(coordinator, entry.data[CONF_DEVICE_SERIAL])])


class NikoSnapshotCamera(CoordinatorEntity[NikoCoordinator], Camera):
    """Camera entity that serves the snapshot captured at the last doorbell ring."""

    _attr_name = "Last Call Snapshot"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature(0)

    def __init__(self, coordinator: NikoCoordinator, serial: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._serial = serial
        self._attr_unique_id = f"{serial}_snapshot_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"Niko Doorbell {serial}",
            manufacturer="Niko / Hikvision",
            model="Access Control Doorbell",
        )
        self._cached_url: str | None = None
        self._cached_image: bytes | None = None

    @property
    def frame_interval(self) -> float:
        return 30.0

    def _handle_coordinator_update(self) -> None:
        call = self.coordinator.data.last_call if self.coordinator.data else None
        new_url = call.pic_url if call else None
        if new_url != self._cached_url:
            self._cached_url = new_url
            self._cached_image = None
        super()._handle_coordinator_update()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        url = self._cached_url
        if not url:
            return None
        if self._cached_image:
            return self._cached_image
        try:
            self._cached_image = await self.coordinator.api.get_picture(url)
        except Exception as err:
            _LOGGER.warning("Failed to fetch doorbell snapshot: %s", err)
            return None
        return self._cached_image

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if not data or not data.last_call:
            return {}
        call = data.last_call
        return {
            "calling_time": call.calling_time,
            "calling_status": call.status_label,
            "calling_id": call.calling_id,
            "pic_url": call.pic_url,
        }
