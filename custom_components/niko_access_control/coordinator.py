"""DataUpdateCoordinator for Niko Access Control."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CallingInfo, DeviceInfo, HikConnectAPI, HikConnectAuthError, HikConnectError, LocalISAPIClient
from .const import DEFAULT_SCAN_INTERVAL, DEVICE_LIST_INTERVAL, DOMAIN, HISTORY_SLOTS, LIVE_STATE_INTERVAL

_T = TypeVar("_T")

_PICTURE_CACHE_SIZE = 60  # ~100 kB each; covers the full 50-call history

_LOGGER = logging.getLogger(__name__)


@dataclass
class CoordinatorData:
    calls: list[CallingInfo] = field(default_factory=list)
    device_info: DeviceInfo | None = None
    cloud_device: dict[str, Any] | None = None  # entry from the account device list
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
        username: str = "",
        password: str = "",
        local_client: LocalISAPIClient | None = None,
    ) -> None:
        self.api = api
        self.device_serial = device_serial
        self._username = username
        self._password = password
        self.local_client = local_client
        self.ring_coordinator: NikoCallStatusCoordinator | None = None
        self._cloud_device: dict[str, Any] | None = None
        self._details_fetched = 0.0
        self._device_info: DeviceInfo | None = None
        self._pictures: OrderedDict[str, bytes] = OrderedDict()
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

    def call_by_id(self, calling_id: str) -> CallingInfo | None:
        calls = self.data.calls if self.data else []
        return next((c for c in calls if c.calling_id == calling_id), None)

    async def async_get_picture(self, call: CallingInfo) -> bytes | None:
        """Snapshot bytes for a call, cached by calling_id (snapshots never change)."""
        if not call.pic_url:
            return None
        if (cached := self._pictures.get(call.calling_id)) is not None:
            self._pictures.move_to_end(call.calling_id)
            return cached
        data = await self.api.get_picture(call.pic_url)
        self._pictures[call.calling_id] = data
        while len(self._pictures) > _PICTURE_CACHE_SIZE:
            self._pictures.popitem(last=False)
        return data

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

        # Everything below is best-effort and slow-changing: refresh every
        # DEVICE_LIST_INTERVAL. The ISAPI relay request can take several seconds.
        if time.monotonic() - self._details_fetched > DEVICE_LIST_INTERVAL:
            self._details_fetched = time.monotonic()
            try:
                self._device_info = await self.api.get_device_info(self.device_serial)
            except Exception as err:
                self._device_info = None
                _LOGGER.debug("Device info unavailable: %s", err)
            try:
                devices = await self.api.get_user_devices()
                self._cloud_device = next(
                    (d for d in devices if d.get("deviceSerial") == self.device_serial), None
                )
            except HikConnectAuthError:
                raise
            except Exception as err:
                _LOGGER.debug("Device list unavailable: %s", err)

        # The doorbell answering an ISAPI request through the cloud relay is the
        # real reachability test. The cloud's own status flag (device list,
        # call-status endpoint) reports some Niko units (510-31001) as offline
        # even while they work, so it is only a fallback.
        online: bool | None = None
        if self._device_info is not None:
            online = True
        elif self._cloud_device and isinstance(self._cloud_device.get("status"), int):
            online = self._cloud_device["status"] == 1

        return CoordinatorData(
            calls=calls,
            device_info=self._device_info,
            cloud_device=self._cloud_device,
            online=online,
        )


class NikoCallStatusCoordinator(DataUpdateCoordinator[dict]):
    """Fast-polling coordinator for live call state (5 s interval).

    Sources, in order: the doorbell's local ISAPI (when local access is set up
    in the options), then the cloud call-status endpoint. Some devices support
    neither; see `supported`.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: HikConnectAPI,
        device_serial: str,
        local_client: LocalISAPIClient | None = None,
    ) -> None:
        self.api = api
        self.device_serial = device_serial
        self.local_client = local_client
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_ring",
            update_interval=timedelta(seconds=LIVE_STATE_INTERVAL),
        )

    @property
    def call_state(self) -> str | None:
        """idle / ringing / on_call, or None when unknown."""
        return HikConnectAPI.parse_call_state(self.data)

    @property
    def supported(self) -> bool:
        """True when a live call state source answered with a usable state."""
        return self.call_state is not None

    async def _async_update_data(self) -> dict:
        if self.local_client is not None:
            status = await self.local_client.get_call_status()
            if HikConnectAPI.parse_call_state(status) is not None:
                return status
        try:
            return await self.api.get_call_status(self.device_serial)
        except Exception as err:
            # Auth errors included: the main coordinator owns session refresh /
            # re-login and raises ConfigEntryAuthFailed when that truly fails.
            raise UpdateFailed(str(err)) from err
