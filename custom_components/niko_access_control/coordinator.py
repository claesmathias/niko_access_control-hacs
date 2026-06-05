"""DataUpdateCoordinator for Niko Access Control."""
from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_T = TypeVar("_T")

from .api import CallingInfo, DeviceInfo, HikConnectAPI, HikConnectAuthError, HikConnectError, LocalISAPIClient
from .const import CONF_DEVICE_SERIAL, DEFAULT_SCAN_INTERVAL, DOMAIN, HISTORY_SLOTS

_LOGGER = logging.getLogger(__name__)


@dataclass
class CoordinatorData:
    calls: list[CallingInfo] = field(default_factory=list)
    device_info: DeviceInfo | None = None
    online: bool | None = None
    ringing: bool = False

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
        username: str = "",
        password: str = "",
        local_client: LocalISAPIClient | None = None,
    ) -> None:
        self.api = api
        self.device_serial = device_serial
        self._username = username
        self._password = password
        self.local_client = local_client
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def async_api_call(
        self, coro_factory: Callable[[], Coroutine[Any, Any, _T]]
    ) -> _T:
        """Run coro_factory(), retrying once after session refresh / re-login."""
        try:
            return await coro_factory()
        except HikConnectAuthError:
            pass
        try:
            await self.api.refresh_session()
            return await coro_factory()
        except HikConnectAuthError:
            pass
        if self._username and self._password:
            try:
                await self.api.login(self._username, self._password)
                return await coro_factory()
            except HikConnectAuthError as err:
                raise HomeAssistantError(f"Re-login failed: {err}") from err
        raise HomeAssistantError("Session expired and no stored credentials for re-login")

    async def _async_update_data(self) -> CoordinatorData:
        try:
            return await self._fetch()
        except HikConnectAuthError:
            # 1. Try refreshing the token first
            try:
                await self.api.refresh_session()
                return await self._fetch()
            except HikConnectAuthError:
                pass
            # 2. Refresh token expired — full re-login with stored credentials
            if self._username and self._password:
                try:
                    await self.api.login(self._username, self._password)
                    return await self._fetch()
                except HikConnectAuthError as err:
                    raise ConfigEntryAuthFailed(str(err)) from err
            raise ConfigEntryAuthFailed("Session expired and no credentials stored for re-login")
        except HikConnectError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> CoordinatorData:
        calls = await self.api.get_calls(self.device_serial, count=HISTORY_SLOTS)

        # Device info and online status are best-effort — don't fail the update if unavailable
        device_info: DeviceInfo | None = None
        online: bool | None = None

        try:
            device_info = await self.api.get_device_info(self.device_serial)
        except Exception as err:
            _LOGGER.debug("Device info unavailable: %s", err)

        ringing = False
        try:
            status = await self.api.get_call_status(self.device_serial)
            online = status.get("rc") == 1
            ringing = status.get("callStatus") == 1
        except Exception as err:
            _LOGGER.debug("Call status unavailable: %s", err)

        return CoordinatorData(calls=calls, device_info=device_info, online=online, ringing=ringing)


class NikoCallStatusCoordinator(DataUpdateCoordinator[dict]):
    """Fast-polling coordinator for ringing detection (5 s interval)."""

    def __init__(self, hass: HomeAssistant, api: HikConnectAPI, device_serial: str) -> None:
        self.api = api
        self.device_serial = device_serial
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_ring",
            update_interval=timedelta(seconds=5),
        )

    async def _async_update_data(self) -> dict:
        try:
            return await self.api.get_call_status(self.device_serial)
        except HikConnectAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HikConnectError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(str(err)) from err
