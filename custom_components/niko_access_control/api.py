"""HikConnect API client for Niko Access Control."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

try:
    import audioop as _audioop
    _AUDIOOP_OK = True
except ImportError:
    _AUDIOOP_OK = False  # Python 3.13+

try:
    import httpx as _httpx
    _HTTPX_OK = True
except ImportError:
    _HTTPX_OK = False

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
    # Call control (answer / reject / hangup)
    # ------------------------------------------------------------------

    async def answer_call(self, device_serial: str) -> bool:
        """Answer an incoming doorbell call.

        Tries ISAPI callSignal first (newer devices); falls back to the
        cloud callOperation endpoint (cmdId=2).
        """
        if await self._isapi_call_signal(device_serial, "answer"):
            return True
        return await self._call_operation(device_serial, cmd_id=2)

    async def reject_call(self, device_serial: str) -> bool:
        """Reject an incoming doorbell call (cmdId=3 / cmeType='reject')."""
        if await self._isapi_call_signal(device_serial, "reject"):
            return True
        return await self._call_operation(device_serial, cmd_id=3)

    async def hangup_call(self, device_serial: str) -> bool:
        """Hang up an active call (cmdId=5, no ISAPI equivalent)."""
        return await self._call_operation(device_serial, cmd_id=5)

    async def _isapi_call_signal(self, device_serial: str, cme_type: str) -> bool:
        """Send callSignal via ISAPI transparent channel (newer devices)."""
        import json as _json
        body = _json.dumps({"callSignal": {"cmeType": cme_type, "sessionId": self._client_no}})
        transmission = f"PUT /ISAPI/VideoIntercom/callSignal?format=json\r\n{body}"
        try:
            resp = await self._post(
                "/api/device/isapi",
                data={"subSerial": device_serial, "cmdId": "19713", "transmissionData": transmission},
            )
        except Exception as err:
            _LOGGER.debug("_isapi_call_signal %s failed: %s", cme_type, err)
            return False
        raw = resp.get("data") or resp.get("msg", "")
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                return parsed.get("ResponseStatus", {}).get("statusCode") == 1
            except Exception:
                pass
        return str(resp.get("meta", {}).get("code", "")) == "200"

    async def _call_operation(self, device_serial: str, cmd_id: int) -> bool:
        """PUT /v3/devconfig/v1/call/{serial}/operation?cmdId=N&handler=<user>."""
        url = f"{self._base_url}/v3/devconfig/v1/call/{device_serial}/operation"
        params = {"cmdId": cmd_id, "handler": self._client_no}
        try:
            async with self._session.put(
                url,
                params=params,
                headers=self._common_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (401, 403):
                    raise HikConnectAuthError(f"Session expired (HTTP {resp.status})")
                body = await resp.json(content_type=None)
            _LOGGER.debug("_call_operation cmdId=%d response: %s", cmd_id, body)
            rc = body.get("data", {})
            if isinstance(rc, dict):
                return rc.get("rc", 0) == 1
            meta_code = str(body.get("meta", {}).get("code", ""))
            return meta_code == "200"
        except HikConnectAuthError:
            raise
        except Exception as err:
            _LOGGER.warning("_call_operation cmdId=%d failed: %s", cmd_id, err)
            return False

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


# ---------------------------------------------------------------------------
# Local ISAPI two-way audio (direct LAN access, Digest Auth)
# ---------------------------------------------------------------------------

class LocalISAPIClient:
    """Streams audio to the doorbell via the local ISAPI HTTP interface.

    Uses HTTP Digest Auth (httpx).  The audio must be G.711 µ-law encoded at
    8 kHz mono — use ``wav_to_mulaw`` / ``audio_to_mulaw`` to convert TTS output.
    """

    _CHANNEL = 1

    def __init__(self, host: str, username: str, password: str) -> None:
        if not _HTTPX_OK:
            raise RuntimeError("httpx is required for local ISAPI access (pip install httpx)")
        self._base = f"http://{host}"
        self._username = username
        self._password = password

    def _client(self) -> "_httpx.AsyncClient":
        return _httpx.AsyncClient(
            auth=_httpx.DigestAuth(self._username, self._password),
            timeout=15.0,
        )

    async def test_connection(self) -> bool:
        """Return True if the device is reachable and credentials are valid."""
        try:
            async with self._client() as c:
                resp = await c.get(f"{self._base}/ISAPI/System/deviceInfo")
                return resp.status_code == 200
        except Exception as err:
            _LOGGER.debug("LocalISAPI test_connection failed: %s", err)
            return False

    async def speak(self, pcm_mulaw_bytes: bytes) -> bool:
        """Open the two-way audio channel, stream PCM µ-law data, then close."""
        if not await self._open():
            return False
        ok = await self._send(pcm_mulaw_bytes)
        await self._close()
        return ok

    async def _open(self) -> bool:
        path = f"/ISAPI/System/twoWayAudio/channels/{self._CHANNEL}/open"
        try:
            async with self._client() as c:
                resp = await c.put(
                    self._base + path,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    content=b"",
                )
                _LOGGER.debug("twoWayAudio open → %d", resp.status_code)
                return resp.status_code in (200, 201)
        except Exception as err:
            _LOGGER.debug("twoWayAudio open failed: %s", err)
            return False

    async def _send(self, data: bytes) -> bool:
        path = f"/ISAPI/System/twoWayAudio/channels/{self._CHANNEL}/audioData"
        try:
            async with self._client() as c:
                resp = await c.put(
                    self._base + path,
                    content=data,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=30.0,
                )
                _LOGGER.debug("twoWayAudio send %d bytes → %d", len(data), resp.status_code)
                return resp.status_code in (200, 201, 204)
        except Exception as err:
            _LOGGER.debug("twoWayAudio send failed: %s", err)
            return False

    async def _close(self) -> None:
        path = f"/ISAPI/System/twoWayAudio/channels/{self._CHANNEL}/close"
        try:
            async with self._client() as c:
                await c.put(self._base + path, content=b"")
        except Exception as err:
            _LOGGER.debug("twoWayAudio close failed: %s", err)


# ---------------------------------------------------------------------------
# Audio conversion helpers (WAV/MP3 → G.711 µ-law 8 kHz mono)
# ---------------------------------------------------------------------------

def wav_to_mulaw(wav_bytes: bytes) -> bytes:
    """Convert WAV bytes to raw G.711 µ-law 8 kHz mono using stdlib audioop."""
    if not _AUDIOOP_OK:
        raise RuntimeError("audioop not available; use audio_to_mulaw with ffmpeg instead")
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if channels == 2:
        pcm = _audioop.tomono(pcm, sample_width, 0.5, 0.5)
    if sample_width != 2:
        pcm = _audioop.lin2lin(pcm, sample_width, 2)
        sample_width = 2
    if framerate != 8000:
        pcm, _ = _audioop.ratecv(pcm, sample_width, 1, framerate, 8000, None)
    return _audioop.lin2ulaw(pcm, 2)


async def audio_to_mulaw(audio_bytes: bytes, mime_type: str) -> bytes:
    """Convert arbitrary audio (WAV or MP3) to G.711 µ-law 8 kHz mono.

    Uses audioop for WAV; falls back to an ffmpeg subprocess for other formats.
    """
    is_wav = "wav" in mime_type or audio_bytes[:4] == b"RIFF"
    if is_wav and _AUDIOOP_OK:
        return wav_to_mulaw(audio_bytes)

    # ffmpeg fallback (handles MP3, OGG, etc.)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", "pipe:0",
        "-ar", "8000", "-ac", "1",
        "-acodec", "pcm_mulaw", "-f", "mulaw", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate(audio_bytes)
    if not stdout:
        raise RuntimeError("ffmpeg produced no audio output — is ffmpeg installed?")
    return stdout
