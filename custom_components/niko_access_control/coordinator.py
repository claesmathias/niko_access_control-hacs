"""DataUpdateCoordinator for Niko Access Control."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CallingInfo, DeviceInfo, HikConnectAPI, HikConnectAuthError, HikConnectError
from .const import DEFAULT_SCAN_INTERVAL, DEVICE_LIST_INTERVAL, DOMAIN, HISTORY_SLOTS, LIVE_STATE_INTERVAL

_T = TypeVar("_T")

_PICTURE_CACHE_SIZE = 60  # ~100 kB each; covers the full 50-call history

# A connection problem must last this long before it is raised in Settings →
# Repairs, so short cloud hiccups don't produce an issue.
ISSUE_DELAY = 300  # seconds
ISSUE_CLOUD_UNREACHABLE = "cloud_unreachable_{serial}"
ISSUE_DOORBELL_OFFLINE = "doorbell_offline_{serial}"

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
        title: str = "",
    ) -> None:
        self.api = api
        self.device_serial = device_serial
        self.title = title or f"Niko {device_serial}"
        self._unreachable_since: float | None = None
        self._offline_since: float | None = None
        self._username = username
        self._password = password
        self.ring_coordinator: NikoCallStatusCoordinator | None = None
        self.notifier = None  # NikoNotifier, set in __init__.async_setup_entry
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
            return await self._api_call_with_reauth(coro_factory)
        except HikConnectAuthError as err:
            raise HomeAssistantError(f"Re-login failed: {err}") from err
        except HikConnectError as err:
            raise HomeAssistantError(f"Hik-Connect request failed: {err}") from err

    async def _api_call_with_reauth(
        self, coro_factory: Callable[[], Coroutine[Any, Any, _T]]
    ) -> _T:
        try:
            return await coro_factory()
        except HikConnectAuthError:
            pass
        try:
            await self.api.refresh_session()
            return await coro_factory()
        except HikConnectAuthError:
            pass
        if not (self._username and self._password):
            raise HikConnectAuthError("Session expired and no stored credentials for re-login")
        await self.api.login(self._username, self._password)
        return await coro_factory()

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
            data = await self._fetch_with_reauth()
        except HikConnectError as err:
            self._unreachable_since = self._track_issue(
                ISSUE_CLOUD_UNREACHABLE, self._unreachable_since, True, error=str(err)
            )
            raise UpdateFailed(str(err)) from err
        self._unreachable_since = self._track_issue(
            ISSUE_CLOUD_UNREACHABLE, self._unreachable_since, False
        )
        if data.online is not None:  # unknown: leave the offline issue as it is
            self._offline_since = self._track_issue(
                ISSUE_DOORBELL_OFFLINE, self._offline_since, not data.online
            )
        return data

    def _track_issue(
        self, issue: str, since: float | None, failing: bool, **placeholders: str
    ) -> float | None:
        """Raise a Repairs issue once a problem has lasted ISSUE_DELAY; clear it on recovery.

        Returns the new "failing since" timestamp.
        """
        issue_id = issue.format(serial=self.device_serial)
        if not failing:
            if since is not None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return None
        now = time.monotonic()
        if since is None:
            since = now
        if now - since >= ISSUE_DELAY:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue.removesuffix("_{serial}"),
                translation_placeholders={
                    "name": self.title,
                    "serial": self.device_serial,
                    "minutes": str(int((now - since) // 60)),
                    **placeholders,
                },
            )
        return since

    @callback
    def async_clear_issues(self) -> None:
        """Remove this doorbell's Repairs issues (entry unloaded or removed)."""
        for issue in (ISSUE_CLOUD_UNREACHABLE, ISSUE_DOORBELL_OFFLINE):
            ir.async_delete_issue(self.hass, DOMAIN, issue.format(serial=self.device_serial))

    async def _fetch_with_reauth(self) -> CoordinatorData:
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
            raise ConfigEntryAuthFailed("Session expired and no credentials stored for re-login") from None

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

    Source: the cloud call-status endpoint. Some devices don't report a live
    call state there; see `supported`.
    """

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
        try:
            return await self.api.get_call_status(self.device_serial)
        except Exception as err:
            # Auth errors included: the main coordinator owns session refresh /
            # re-login and raises ConfigEntryAuthFailed when that truly fails.
            raise UpdateFailed(str(err)) from err
