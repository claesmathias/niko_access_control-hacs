"""HikConnect API client for Niko Access Control."""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    APP_CHANNEL,
    CALLING_LIST_PATH,
    CLIENT_TYPE,
    CLIENT_VERSION,
    LOGIN_PATH,
    REFRESH_SESSION_PATH,
)

_LOGGER = logging.getLogger(__name__)


class HikConnectAuthError(Exception):
    """Raised when authentication fails."""


class HikConnectError(Exception):
    """Raised on general API errors."""


@dataclass
class CallingInfo:
    """A single doorbell call event."""

    calling_id: str
    calling_time: str
    calling_status: int
    device_serial: str
    channel_no: int
    msg_status: int
    pic_url: str | None = None
    calling_message: str = ""

    @property
    def calling_datetime(self) -> datetime | None:
        """Return a naive datetime; callers must localise with dt_util."""
        try:
            return datetime.strptime(self.calling_time, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    @property
    def is_answered(self) -> bool:
        return self.calling_status == 1  # 1=answered, 2=missed

    @property
    def status_label(self) -> str:
        return "answered" if self.is_answered else "missed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "calling_id": self.calling_id,
            "time": self.calling_time,
            "status": self.status_label,
            "pic_url": self.pic_url,
        }


@dataclass
class DeviceInfo:
    """Device hardware/firmware information from ISAPI."""

    firmware_version: str = ""
    firmware_released_date: str = ""
    hardware_version: str = ""
    model: str = ""
    serial_number: str = ""
    mac_address: str = ""
    ip_address: str = ""
    device_name: str = ""

    @property
    def hardware_version_display(self) -> str:
        """Return hardware version, falling back to firmware build date."""
        return self.hardware_version or self.firmware_released_date or ""


class HikConnectAPI:
    """Async client for the HikConnect cloud API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._base_url = API_BASE_URL
        self._session_id: str | None = None
        self._refresh_session_id: str | None = None
        self._area_id: str = "0"
        self._client_no: str = str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------

    def _anon_headers(self) -> dict[str, str]:
        return {
            "clientType": CLIENT_TYPE,
            "clientVersion": CLIENT_VERSION,
            "appChannel": APP_CHANNEL,
            "appId": "NIKO",
            "customno": "3000078",
            "clientNo": self._client_no,
            "lang": "en-US",
        }

    def _common_headers(self) -> dict[str, str]:
        headers = {
            **self._anon_headers(),
            "areaId": self._area_id,
        }
        if self._session_id:
            headers["sessionId"] = self._session_id
        return headers

    @staticmethod
    def _hash_password(password: str) -> str:
        return hashlib.md5(password.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        hashed = self._hash_password(password)
        data = {
            "account": username,
            "password": hashed,
            "featureCode": self._client_no,
            "clientType": CLIENT_TYPE,
            "cuName": "NIKO_HA",
            "imageCode": "",
            "smsCode": "",
            "bizType": "",
            "smsToken": "",
        }
        resp = await self._post(LOGIN_PATH, data=data, authenticated=False)
        meta_code = str(resp.get("meta", {}).get("code", resp.get("code", "")))
        if meta_code != "200":
            msg = resp.get("meta", {}).get("message") or resp.get("msg")
            raise HikConnectAuthError(f"Login failed (code={meta_code}): {msg}")

        session_info = resp.get("loginSession", {})
        self._session_id = session_info.get("sessionId")
        self._refresh_session_id = session_info.get("rfSessionId")

        login_area = resp.get("loginArea", {})
        if login_area.get("apiDomain"):
            self._base_url = f"https://{login_area['apiDomain']}"
        if login_area.get("id") is not None:
            self._area_id = str(login_area["id"])

        if not self._session_id:
            raise HikConnectAuthError("No sessionId in login response")
        _LOGGER.debug("Logged in, base_url=%s areaId=%s", self._base_url, self._area_id)

    async def refresh_session(self) -> None:
        if not self._refresh_session_id:
            raise HikConnectAuthError("No refresh session ID available")
        data = {
            "refreshSessionId": self._refresh_session_id,
            "featureCode": self._client_no,
        }
        resp = await self._put(REFRESH_SESSION_PATH, data=data, authenticated=False)
        meta_code = str(resp.get("meta", {}).get("code", resp.get("code", "")))
        if meta_code != "200":
            raise HikConnectAuthError(f"Session refresh failed (code={meta_code})")
        session_info = resp.get("sessionInfo", {})
        self._session_id = session_info.get("sessionId", self._session_id)
        self._refresh_session_id = session_info.get(
            "refreshSessionId", self._refresh_session_id
        )

    # ------------------------------------------------------------------
    # Calling / doorbell history
    # ------------------------------------------------------------------

    async def get_calls(
        self, device_serial: str, count: int = 10
    ) -> list[CallingInfo]:
        """Fetch call history sorted by date, newest first.

        The API requires msgStatus to be set; query both read (1) and unread (0)
        then merge, deduplicate, and sort by date so the caller gets a unified list.
        """
        path = CALLING_LIST_PATH.format(device_serial=device_serial)
        seen: set[str] = set()
        result: list[CallingInfo] = []

        for msg_status in (0, 1):
            try:
                resp = await self._get(path, params={"msgStatus": msg_status, "pageSize": count})
            except HikConnectAuthError:
                raise
            except Exception as err:
                _LOGGER.debug("get_calls msgStatus=%s failed: %s", msg_status, err)
                continue

            meta_code = str(resp.get("meta", {}).get("code", resp.get("code", "")))
            if meta_code != "200":
                continue

            items = resp.get("data", [])
            if not isinstance(items, list):
                continue

            for r in items:
                cid = r.get("callingId", "")
                if cid and cid not in seen:
                    seen.add(cid)
                    result.append(CallingInfo(
                        calling_id=cid,
                        calling_time=r.get("callingTime", ""),
                        calling_status=r.get("callingStatus", 0),
                        device_serial=r.get("deviceSerial", device_serial),
                        channel_no=r.get("channelNo", 0),
                        msg_status=r.get("msgStatus", 0),
                        pic_url=r.get("picUrl") or None,
                        calling_message=r.get("callingMessage", ""),
                    ))

        result.sort(key=lambda c: c.calling_time, reverse=True)
        return result

    async def get_last_call(self, device_serial: str) -> CallingInfo | None:
        calls = await self.get_calls(device_serial, count=1)
        return calls[0] if calls else None

    # ------------------------------------------------------------------
    # Device info via ISAPI transparent channel
    # ------------------------------------------------------------------

    async def isapi_get(self, device_serial: str, isapi_path: str) -> dict | None:
        """Send an ISAPI GET through the HikConnect cloud transmit channel."""
        transmission_data = f"GET {isapi_path}?format=json\r\n"
        try:
            resp = await self._post(
                "/api/device/isapi",
                data={
                    "subSerial": device_serial,
                    "cmdId": "19713",
                    "transmissionData": transmission_data,
                },
            )
        except Exception as err:
            _LOGGER.debug("isapi_get %s failed: %s", isapi_path, err)
            return None

        # TransmissionResp.data holds the JSON string from the device
        raw_data = resp.get("data") or resp.get("msg")
        if not raw_data:
            return None
        if isinstance(raw_data, dict):
            return raw_data
        try:
            import json
            return json.loads(raw_data)
        except Exception:
            return None

    async def get_device_info(self, device_serial: str) -> DeviceInfo | None:
        data = await self.isapi_get(device_serial, "/ISAPI/System/deviceInfo")
        if not data:
            return None
        try:
            return DeviceInfo(
                firmware_version=data.get("firmwareVersion", ""),
                firmware_released_date=data.get("firmwareReleasedDate", ""),
                hardware_version=data.get("hardwareVersion", ""),
                model=data.get("model", ""),
                serial_number=data.get("serialNumber", ""),
                mac_address=data.get("macAddress", ""),
                ip_address=data.get("ipAddress", ""),
                device_name=data.get("deviceName", ""),
            )
        except Exception as err:
            _LOGGER.debug("Failed to parse DeviceInfo: %s", err)
            return None

    # ------------------------------------------------------------------
    # Online / call status
    # ------------------------------------------------------------------

    async def get_call_status(self, device_serial: str) -> dict[str, Any]:
        """Return parsed call/device status; rc==1 means device is online."""
        import json as _json
        try:
            resp = await self._get(f"/v3/devconfig/v1/call/{device_serial}/status")
            meta_code = str(resp.get("meta", {}).get("code", resp.get("code", "")))
            if meta_code == "200":
                raw = resp.get("data", {})
                if isinstance(raw, str):
                    return _json.loads(raw)
                return raw
        except HikConnectAuthError:
            raise
        except Exception as err:
            _LOGGER.debug("get_call_status failed: %s", err)
        return {}

    # ------------------------------------------------------------------
    # Pictures
    # ------------------------------------------------------------------

    async def get_picture(self, pic_url: str) -> bytes:
        async with self._session.get(
            pic_url,
            headers=self._common_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get(
        self, path: str, params: dict | None = None, authenticated: bool = True
    ) -> dict[str, Any]:
        url = self._base_url + path
        headers = self._common_headers() if authenticated else self._anon_headers()
        try:
            async with self._session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise HikConnectAuthError(str(err)) from err
            raise HikConnectError(str(err)) from err

    async def _post(
        self, path: str, data: dict | None = None, authenticated: bool = True
    ) -> dict[str, Any]:
        url = self._base_url + path
        headers = self._common_headers() if authenticated else self._anon_headers()
        try:
            async with self._session.post(
                url, data=data, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise HikConnectAuthError(str(err)) from err
            raise HikConnectError(str(err)) from err

    async def _put(
        self, path: str, data: dict | None = None, authenticated: bool = True
    ) -> dict[str, Any]:
        url = self._base_url + path
        headers = self._common_headers() if authenticated else self._anon_headers()
        try:
            async with self._session.put(
                url, data=data, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise HikConnectAuthError(str(err)) from err
            raise HikConnectError(str(err)) from err
