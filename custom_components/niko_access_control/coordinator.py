"""DataUpdateCoordinator for Niko Access Control."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CallingInfo, DeviceInfo, HikConnectAPI, HikConnectAuthError, HikConnectError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class CoordinatorData:
    calls: list[CallingInfo] = field(default_factory=list)
    device_info: DeviceInfo | None = None
    online: bool | None = None

    @property
    def last_call(self) -> CallingInfo | None:
        return self.calls[0] if self.calls else None


class NikoCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Polls the HikConnect API for doorbell data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: HikConnectAPI,
        device_serial: str,
    ) -> None:
        self.api = api
        self.device_serial = device_serial
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> CoordinatorData:
        try:
            return await self._fetch()
        except HikConnectAuthError:
            try:
                await self.api.refresh_session()
                return await self._fetch()
            except HikConnectAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
        except HikConnectError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> CoordinatorData:
        calls = await self.api.get_calls(self.device_serial, count=10)

        # Device info and online status are best-effort — don't fail the update if unavailable
        device_info: DeviceInfo | None = None
        online: bool | None = None

        try:
            device_info = await self.api.get_device_info(self.device_serial)
        except Exception as err:
            _LOGGER.debug("Device info unavailable: %s", err)

        try:
            status = await self.api.get_call_status(self.device_serial)
            if status:
                online = True
        except Exception as err:
            _LOGGER.debug("Call status unavailable: %s", err)

        return CoordinatorData(calls=calls, device_info=device_info, online=online)
