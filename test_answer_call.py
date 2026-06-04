"""
Integration + unit tests for doorbell call answering.

Integration test (runs against real API, needs .env):
    .venv/bin/python3 test_answer_call.py

Unit tests (mocked, no network):
    .venv/bin/python3 -m pytest test_answer_call.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
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
    sys.path.insert(0, str(Path(__file__).parent / "custom_components"))
    from niko_access_control.api import HikConnectAPI

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
        print("  ⚠️  Please press the doorbell now.")
        print("  Waiting 15 seconds for you to ring before answering…")
        for remaining in range(15, 0, -1):
            print(f"  Answering in {remaining}s…", end="\r", flush=True)
            await asyncio.sleep(1)
        print()

        result = await api.answer_call(DEVICE_SERIAL)
        print(f"  answer_call → {'✅ OK' if result else '❌ Failed'}")

        if result:
            print("\n  Hanging up in 5 seconds…")
            await asyncio.sleep(5)
            hung = await api.hangup_call(DEVICE_SERIAL)
            print(f"  hangup_call → {'✅ OK' if hung else '❌ Failed'}")

    print(f"\n{SEP}\nDone.\n{SEP}\n")


if __name__ == "__main__":
    asyncio.run(integration_test())
