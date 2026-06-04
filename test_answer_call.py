"""
Integration + unit tests for doorbell call answering and local ISAPI audio.

Integration tests (real device, needs .env):
    .venv/bin/python3 test_answer_call.py           # cloud call-answer test
    .venv/bin/python3 test_answer_call.py --audio    # local ISAPI audio test (no call needed)

Unit tests (mocked, no network):
    .venv/bin/python3 -m pytest test_answer_call.py -v

.env keys:
    NIKO_USERNAME, NIKO_PASSWORD, NIKO_DEVICE_SERIAL   – HikConnect cloud
    NIKO_LOCAL_HOST       – doorbell LAN IP (default: 192.168.11.83)
    NIKO_LOCAL_USERNAME   – device admin username (default: admin)
    NIKO_LOCAL_PASSWORD   – device admin password
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import struct
import sys
import uuid
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import importlib.util
import types

import pytest

# tell pytest-asyncio to handle all async tests automatically
pytest_plugins = ("anyio",)

_PKG = "niko_access_control"
_PKG_ROOT = Path(__file__).parent / "custom_components" / _PKG


def _load_api_module():
    """Load api.py directly, bypassing __init__.py (which requires homeassistant).

    We manually register stub package + const submodule in sys.modules so that
    the relative import `from .const import ...` inside api.py resolves correctly.
    """
    import sys

    # Stub the package itself so relative imports know their parent
    if _PKG not in sys.modules:
        pkg_stub = types.ModuleType(_PKG)
        pkg_stub.__path__ = [str(_PKG_ROOT)]
        pkg_stub.__package__ = _PKG
        sys.modules[_PKG] = pkg_stub

    # Load const.py (no HA deps) so `from .const import ...` succeeds
    const_key = f"{_PKG}.const"
    if const_key not in sys.modules:
        spec = importlib.util.spec_from_file_location(const_key, _PKG_ROOT / "const.py")
        const_mod = importlib.util.module_from_spec(spec)
        const_mod.__package__ = _PKG
        sys.modules[const_key] = const_mod
        spec.loader.exec_module(const_mod)

    # Load api.py
    api_key = f"{_PKG}.api"
    spec = importlib.util.spec_from_file_location(api_key, _PKG_ROOT / "api.py")
    api_mod = importlib.util.module_from_spec(spec)
    api_mod.__package__ = _PKG
    sys.modules[api_key] = api_mod
    spec.loader.exec_module(api_mod)
    return api_mod


# ── env helpers ───────────────────────────────────────────────────────────────

def load_env() -> None:
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

load_env()

USERNAME      = os.environ.get("NIKO_USERNAME", "")
PASSWORD      = os.environ.get("NIKO_PASSWORD", "")
DEVICE_SERIAL = os.environ.get("NIKO_DEVICE_SERIAL", "")
LOCAL_HOST     = os.environ.get("NIKO_LOCAL_HOST", "192.168.11.83")
LOCAL_USERNAME = os.environ.get("NIKO_LOCAL_USERNAME", "admin")
LOCAL_PASSWORD = os.environ.get("NIKO_LOCAL_PASSWORD", "")
SEP = "=" * 70


# ── unit tests (pytest, no network) ──────────────────────────────────────────

class TestAnswerCall:
    """Mock-based unit tests for call control methods."""

    def _make_api(self):
        """Return a HikConnectAPI wired to a fake aiohttp session."""
        HikConnectAPI = _load_api_module().HikConnectAPI
        session = MagicMock()
        api = HikConnectAPI(session)
        api._base_url = "https://api.example.com"
        api._session_id = "fake-session"
        api._area_id = "1"
        return api

    def _ok_resp(self, extra: dict | None = None) -> dict:
        body = {"meta": {"code": "200"}}
        if extra:
            body.update(extra)
        return body

    # ── _isapi_call_signal ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_isapi_answer_success(self):
        api = self._make_api()
        isapi_resp = json.dumps({"ResponseStatus": {"statusCode": 1}})
        with patch.object(api, "_post", new=AsyncMock(return_value={"data": isapi_resp})):
            result = await api._isapi_call_signal(DEVICE_SERIAL or "TEST", "answer")
        assert result is True

    @pytest.mark.asyncio
    async def test_isapi_reject_success(self):
        api = self._make_api()
        isapi_resp = json.dumps({"ResponseStatus": {"statusCode": 1}})
        with patch.object(api, "_post", new=AsyncMock(return_value={"data": isapi_resp})):
            result = await api._isapi_call_signal(DEVICE_SERIAL or "TEST", "reject")
        assert result is True

    @pytest.mark.asyncio
    async def test_isapi_answer_failure_returns_false(self):
        api = self._make_api()
        isapi_resp = json.dumps({"ResponseStatus": {"statusCode": 0, "errorCode": 5}})
        with patch.object(api, "_post", new=AsyncMock(return_value={"data": isapi_resp})):
            result = await api._isapi_call_signal(DEVICE_SERIAL or "TEST", "answer")
        assert result is False

    @pytest.mark.asyncio
    async def test_isapi_network_error_returns_false(self):
        api = self._make_api()
        with patch.object(api, "_post", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await api._isapi_call_signal(DEVICE_SERIAL or "TEST", "answer")
        assert result is False

    # ── _call_operation ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_call_operation_answer_cmdid_2(self):
        api = self._make_api()
        captured = {}

        def fake_put(url, params, headers, timeout):
            captured["url"] = url
            captured["params"] = params
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=MagicMock(
                json=AsyncMock(return_value={"data": {"rc": 1}}),
            ))
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        api._session.put = fake_put
        result = await api._call_operation(DEVICE_SERIAL or "TEST", cmd_id=2)

        assert result is True
        assert captured["params"]["cmdId"] == 2

    @pytest.mark.asyncio
    async def test_call_operation_reject_cmdid_3(self):
        api = self._make_api()

        def fake_put(url, params, headers, timeout):
            assert params["cmdId"] == 3
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=MagicMock(
                json=AsyncMock(return_value={"data": {"rc": 1}}),
            ))
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        api._session.put = fake_put
        result = await api._call_operation(DEVICE_SERIAL or "TEST", cmd_id=3)
        assert result is True

    @pytest.mark.asyncio
    async def test_call_operation_hangup_cmdid_5(self):
        api = self._make_api()

        def fake_put(url, params, headers, timeout):
            assert params["cmdId"] == 5
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=MagicMock(
                json=AsyncMock(return_value={"data": {"rc": 1}}),
            ))
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

        api._session.put = fake_put
        result = await api._call_operation(DEVICE_SERIAL or "TEST", cmd_id=5)
        assert result is True

    # ── answer_call: ISAPI path ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_answer_call_uses_isapi_when_available(self):
        """answer_call should succeed via ISAPI and NOT fall back to callOperation."""
        api = self._make_api()
        isapi_resp = json.dumps({"ResponseStatus": {"statusCode": 1}})
        call_op_called = []

        with patch.object(api, "_post", new=AsyncMock(return_value={"data": isapi_resp})):
            with patch.object(api, "_call_operation", new=AsyncMock(side_effect=lambda *a, **k: call_op_called.append(1))) as mock_op:
                result = await api.answer_call(DEVICE_SERIAL or "TEST")

        assert result is True
        mock_op.assert_not_called()

    # ── answer_call: fallback path ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_answer_call_falls_back_to_call_operation(self):
        """When ISAPI fails, answer_call falls back to callOperation cmdId=2."""
        api = self._make_api()
        isapi_resp = json.dumps({"ResponseStatus": {"statusCode": 0}})

        with patch.object(api, "_post", new=AsyncMock(return_value={"data": isapi_resp})):
            with patch.object(api, "_call_operation", new=AsyncMock(return_value=True)) as mock_op:
                result = await api.answer_call(DEVICE_SERIAL or "TEST")

        assert result is True
        mock_op.assert_called_once_with(DEVICE_SERIAL or "TEST", cmd_id=2)

    @pytest.mark.asyncio
    async def test_reject_call_falls_back_to_cmdid_3(self):
        api = self._make_api()
        isapi_resp = json.dumps({"ResponseStatus": {"statusCode": 0}})

        with patch.object(api, "_post", new=AsyncMock(return_value={"data": isapi_resp})):
            with patch.object(api, "_call_operation", new=AsyncMock(return_value=True)) as mock_op:
                result = await api.reject_call(DEVICE_SERIAL or "TEST")

        assert result is True
        mock_op.assert_called_once_with(DEVICE_SERIAL or "TEST", cmd_id=3)

    @pytest.mark.asyncio
    async def test_hangup_call_uses_cmdid_5(self):
        api = self._make_api()
        with patch.object(api, "_call_operation", new=AsyncMock(return_value=True)) as mock_op:
            result = await api.hangup_call(DEVICE_SERIAL or "TEST")
        assert result is True
        mock_op.assert_called_once_with(DEVICE_SERIAL or "TEST", cmd_id=5)


# ── integration test (real API) ───────────────────────────────────────────────

async def integration_test() -> None:
    """
    Connects to the real HikConnect API, checks for an active/incoming call,
    and attempts to answer it.

    Run this WHILE the doorbell is being pressed for a real end-to-end test.
    """
    import aiohttp
    HikConnectAPI = _load_api_module().HikConnectAPI

    if not USERNAME or not PASSWORD or not DEVICE_SERIAL:
        print("❌  Set NIKO_USERNAME / NIKO_PASSWORD / NIKO_DEVICE_SERIAL in .env")
        return

    async with aiohttp.ClientSession() as session:
        api = HikConnectAPI(session)
        print(f"\n{SEP}\nLOGIN\n{SEP}")
        await api.login(USERNAME, PASSWORD)
        print("  ✅ Logged in")

        print(f"\n{SEP}\nCALL STATUS\n{SEP}")
        status = await api.get_call_status(DEVICE_SERIAL)
        print(json.dumps(status, indent=2))

        print(f"\n{SEP}\nCALL INFOS\n{SEP}")
        try:
            resp = await api._get(f"/v3/devconfig/v1/call/{DEVICE_SERIAL}/infos")
            print(json.dumps(resp, indent=2))
        except Exception as e:
            print(f"  call infos failed: {e}")

        print(f"\n{SEP}\nANSWER CALL (cmdId=2 / ISAPI callSignal)\n{SEP}")

        # Answer immediately if a call is already ringing, otherwise poll for up to 60s.
        call_detected = status.get("callStatus") == 1
        if call_detected:
            print("  Active call already detected — answering immediately…")
        else:
            print("  ⚠️  Please press the doorbell now. Waiting up to 60 seconds…")
            for remaining in range(60, 0, -1):
                status = await api.get_call_status(DEVICE_SERIAL)
                if status.get("callStatus") == 1:
                    print(f"\n  Incoming call detected! Answering now…")
                    call_detected = True
                    break
                print(f"  Waiting for incoming call… {remaining}s", end="\r", flush=True)
                await asyncio.sleep(1)
            print()

        if not call_detected:
            print("  ⚠️  No call detected within 60 seconds — aborting.")
            return

        result = await api.answer_call(DEVICE_SERIAL)
        print(f"  answer_call → {'✅ OK' if result else '❌ Failed'}")

        if result:
            print("\n  Hanging up in 5 seconds…")
            await asyncio.sleep(5)
            hung = await api.hangup_call(DEVICE_SERIAL)
            print(f"  hangup_call → {'✅ OK' if hung else '❌ Failed'}")

    print(f"\n{SEP}\nDone.\n{SEP}\n")


# ── local ISAPI audio test (no call needed) ──────────────────────────────────

def _generate_tone_wav(freq: int = 440, duration: float = 10.0, sample_rate: int = 44100) -> bytes:
    """Return a WAV containing a pure sine-wave tone."""
    n = int(sample_rate * duration)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)          # 16-bit
        wf.setframerate(sample_rate)
        for i in range(n):
            v = int(32767 * math.sin(2 * math.pi * freq * i / sample_rate))
            wf.writeframes(struct.pack("<h", v))
    return buf.getvalue()


async def local_audio_test() -> None:
    """Stream a 10-second 440 Hz tone to the doorbell speaker via local ISAPI.

    No active call is required — the test opens the two-way audio channel
    directly.  You should hear a steady tone from the doorbell.

    Requires NIKO_LOCAL_HOST / NIKO_LOCAL_USERNAME / NIKO_LOCAL_PASSWORD in .env.
    """
    api_mod = _load_api_module()
    LocalISAPIClient = api_mod.LocalISAPIClient
    wav_to_mulaw = api_mod.wav_to_mulaw

    if not LOCAL_PASSWORD:
        print("❌  Set NIKO_LOCAL_PASSWORD (and optionally NIKO_LOCAL_HOST / NIKO_LOCAL_USERNAME) in .env")
        return

    print(f"\n{SEP}\nLOCAL ISAPI AUDIO TEST\n{SEP}")
    print(f"  Host     : {LOCAL_HOST}")
    print(f"  Username : {LOCAL_USERNAME}")

    client = LocalISAPIClient(LOCAL_HOST, LOCAL_USERNAME, LOCAL_PASSWORD)

    print("\n  Testing connection…")
    reachable = await client.test_connection()
    if not reachable:
        print("  ❌  Cannot reach doorbell — check IP and credentials.")
        return
    print("  ✅ Connected")

    print("\n  Generating 10 s 440 Hz tone…")
    wav_bytes = _generate_tone_wav(freq=440, duration=10.0)
    print(f"  WAV size : {len(wav_bytes):,} bytes")

    pcm_bytes = wav_to_mulaw(wav_bytes)
    print(f"  µ-law size: {len(pcm_bytes):,} bytes  (8 kHz mono)")

    print("\n  Opening two-way audio channel and streaming — you should hear a tone…")
    ok = await client.speak(pcm_bytes)
    print(f"\n  Stream → {'✅ OK' if ok else '❌ Failed'}")

    print(f"\n{SEP}\nDone.\n{SEP}\n")


if __name__ == "__main__":
    if "--audio" in sys.argv:
        asyncio.run(local_audio_test())
    else:
        asyncio.run(integration_test())
