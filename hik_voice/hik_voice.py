"""
hik_voice.py — Python ctypes wrapper for Hikvision CASClient voice talk.

Uses the arm64 libezstreamclient.so extracted from the Niko Access Control
APK.  Authentication goes through the HikConnect cloud VTM relay; no local
device admin password is required.

Setup (Raspberry Pi):
    ./hik_voice/setup_libs.sh /path/to/AccessControl.apk
    export HIK_LIB_DIR=~/hik_voice_libs    # or set in .env

Standalone test:
    python3 hik_voice/hik_voice.py          # streams a 5s tone (needs .env)
"""
from __future__ import annotations

import asyncio
import ctypes
import io
import logging
import math
import os
import struct
import uuid
import wave
from pathlib import Path
from typing import Callable

_LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# C struct definitions (from APK JNA / JNI source analysis)
# ─────────────────────────────────────────────────────────────────────────────

# Field sizes — conservative upper bounds; see analysis notes below.
#   szOperationCode / szKey can be long base64 JWT tokens (~400 chars).
_SZ_IP    = 128
_SZ_SERIAL = 64
_SZ_SESSION = 128
_SZ_KEY   = 512
_SZ_CODE  = 512


class ST_SERVER_INFO(ctypes.Structure):
    """VTM relay server coordinates.  From ST_SERVER_INFO.java (2 fields)."""
    _fields_ = [
        ("nServerPort", ctypes.c_int),
        ("szServerIP",  ctypes.c_char * _SZ_IP),
    ]


class ST_DEV_INFO(ctypes.Structure):
    """Per-device credentials returned by GetDevOperationCodeEx.
    From ST_DEV_INFO.java (4 fields; field order matches Java declaration).

    NOTE: szKey and szOperationCode sizes are guesses — adjust if the call
    returns LIBC_ERR or garbage.  The actual sizes are in the native header
    which we don't have.  512 bytes each is a safe upper bound.
    """
    _fields_ = [
        ("enEncryptType",   ctypes.c_int),
        ("szDevSerial",     ctypes.c_char * _SZ_SERIAL),
        ("szKey",           ctypes.c_char * _SZ_KEY),
        ("szOperationCode", ctypes.c_char * _SZ_CODE),
    ]


class ST_STREAM_INFO(ctypes.Structure):
    """Stream session parameters for VoiceTalkStartEx.
    From ST_STREAM_INFO.java (15 fields; field order matches Java declaration).
    """
    _fields_ = [
        ("enEncryptType",   ctypes.c_int),
        ("iChannel",        ctypes.c_int),
        ("iDevCmdPort",     ctypes.c_int),
        ("iDevStreamPort",  ctypes.c_int),
        ("iServerPort",     ctypes.c_int),
        ("iStreamType",     ctypes.c_int),
        ("iStunPort",       ctypes.c_int),
        ("szClientSession", ctypes.c_char * _SZ_SESSION),
        ("szDevIP",         ctypes.c_char * _SZ_IP),
        ("szDevSerial",     ctypes.c_char * _SZ_SERIAL),
        ("szKey",           ctypes.c_char * _SZ_KEY),
        ("szOperationCode", ctypes.c_char * _SZ_CODE),
        ("szPermanetkey",   ctypes.c_char * _SZ_KEY),
        ("szServerIP",      ctypes.c_char * _SZ_IP),
        ("szStunIP",        ctypes.c_char * _SZ_IP),
    ]


# Callback types used by CASClient_VoiceTalkStartEx
# int callback(int msgType, int iError, void* pUser, void* pData, int dataLen, void* pUser2)
_CASMsgCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
)

# ─────────────────────────────────────────────────────────────────────────────
# Library loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_libs(lib_dir: str | None = None) -> ctypes.CDLL:
    """Load libezstreamclient.so and its dependency chain from lib_dir."""
    d = lib_dir or os.environ.get("HIK_LIB_DIR") or str(Path.home() / "hik_voice_libs")
    d = os.path.expanduser(d)

    if not os.path.isdir(d):
        raise RuntimeError(
            f"Library directory not found: {d}\n"
            "Run  hik_voice/setup_libs.sh /path/to/APK  on the Raspberry Pi first."
        )

    # Set LD_LIBRARY_PATH so dlopen can find all sibling libs
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{d}:{existing}" if existing else d

    # Load dependency chain explicitly (avoids dlopen search order issues)
    _rtld = ctypes.RTLD_GLOBAL
    for name in (
        "libhpr.so", "libmbedtls.so", "libssl.so", "libcrypto.so",
        "libopensslwrap.so", "libencryptprotect.so",
        "libHCCore.so", "libHCNetUtils.so",
        "libFormatConversion.so", "libSystemTransform.so",
        "libMediaVDecode.so", "libMediaVEncode.so", "libMediaACodec.so",
        "libMediaAssistant.so", "libMediaExtractor.so",
        "libMediaMuxer.so", "libMediaPostProc.so",
        "libHCPlayBack.so", "libHCPreview.so", "libHCVoiceTalk.so",
        "libNPQos.so", "libBavClient.so",
        "libhcnetsdk.so",
    ):
        path = os.path.join(d, name)
        if os.path.exists(path):
            try:
                ctypes.CDLL(path, mode=_rtld)
            except OSError as e:
                _LOGGER.debug("Optional lib %s failed to load: %s", name, e)

    main = os.path.join(d, "libezstreamclient.so")
    lib = ctypes.CDLL(main)
    _LOGGER.debug("Loaded libezstreamclient from %s", main)
    return lib


# ─────────────────────────────────────────────────────────────────────────────
# CASClient wrapper
# ─────────────────────────────────────────────────────────────────────────────

class CASVoiceTalk:
    """Streams G.711 µ-law audio to a Hikvision doorbell via the HikConnect
    VTM cloud relay.  No local device credentials required.

    Usage::

        vtm_ip, vtm_port = await get_vtm_info(session, api, serial)
        op_code, key = await get_operation_code(session, api, serial)

        async with CASVoiceTalk(vtm_ip, vtm_port, serial, session_id,
                                 client_no, op_code, key) as talk:
            await talk.send(pcm_mulaw_8k_bytes)
    """

    # iStreamType value for talkback (from EZ_STREAM_SOURCE_TALKBACK = 6)
    STREAM_TYPE_TALK = 6
    # Audio codec type: 0 = G.711 µ-law (PCMU)
    AUDIO_TYPE_MULAW = 0

    def __init__(
        self,
        vtm_ip: str,
        vtm_port: int,
        device_serial: str,
        session_id: str,
        client_no: str,
        operation_code: str,
        key: str,
        channel: int = 1,
        lib_dir: str | None = None,
    ) -> None:
        self._vtm_ip = vtm_ip
        self._vtm_port = vtm_port
        self._serial = device_serial
        self._session_id = session_id
        self._client_no = client_no
        self._op_code = operation_code
        self._key = key
        self._channel = channel
        self._lib_dir = lib_dir
        self._lib: ctypes.CDLL | None = None
        self._handle: int = 0

    # ── context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "CASVoiceTalk":
        await asyncio.get_event_loop().run_in_executor(None, self._start)
        return self

    async def __aexit__(self, *_) -> None:
        await asyncio.get_event_loop().run_in_executor(None, self._stop)

    # ── public API ────────────────────────────────────────────────────────────

    async def send(self, pcm_mulaw_bytes: bytes) -> bool:
        """Stream raw G.711 µ-law audio to the doorbell speaker."""
        if not self._handle:
            _LOGGER.error("VoiceTalk: not started")
            return False
        return await asyncio.get_event_loop().run_in_executor(
            None, self._send_sync, pcm_mulaw_bytes
        )

    # ── internal (runs in executor thread) ───────────────────────────────────

    def _start(self) -> None:
        self._lib = _load_libs(self._lib_dir)
        lib = self._lib

        # CASClient_InitLib(int logLevel, const char* logPath)
        try:
            lib.CASClient_InitLib(1, b"")
        except Exception as e:
            _LOGGER.debug("CASClient_InitLib: %s (may be fine)", e)

        # Build ST_STREAM_INFO
        info = ST_STREAM_INFO()
        info.iChannel       = self._channel
        info.iStreamType    = self.STREAM_TYPE_TALK
        info.iServerPort    = self._vtm_port
        info.szServerIP     = self._vtm_ip.encode()
        info.szDevSerial    = self._serial.encode()
        info.szClientSession = self._session_id.encode()
        info.szOperationCode = self._op_code.encode()
        info.szKey           = self._key.encode()
        info.enEncryptType   = 0

        # int CASClient_VoiceTalkStartEx(ST_STREAM_INFO*, int audioType,
        #                                int flag, CASMsgCallback, void* user)
        lib.CASClient_VoiceTalkStartEx.restype = ctypes.c_int
        lib.CASClient_VoiceTalkStartEx.argtypes = [
            ctypes.POINTER(ST_STREAM_INFO),
            ctypes.c_int,  # audioType
            ctypes.c_int,  # flag
            ctypes.c_void_p,  # callback (NULL = no callback)
            ctypes.c_void_p,  # user data
        ]

        handle = lib.CASClient_VoiceTalkStartEx(
            ctypes.byref(info),
            self.AUDIO_TYPE_MULAW,
            0,
            None,
            None,
        )
        if handle <= 0:
            err = lib.CASClient_GetLastError() if hasattr(lib, "CASClient_GetLastError") else -1
            raise RuntimeError(f"CASClient_VoiceTalkStartEx returned {handle} (err={err})")

        _LOGGER.debug("VoiceTalk started, handle=%d", handle)
        self._handle = handle

    def _send_sync(self, data: bytes) -> bool:
        lib = self._lib
        # bool CASClient_VoiceTalkInputDataEx(int handle, const char* buf,
        #                                     int size, int audioType)
        lib.CASClient_VoiceTalkInputDataEx.restype  = ctypes.c_bool
        lib.CASClient_VoiceTalkInputDataEx.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
        ]
        ok = lib.CASClient_VoiceTalkInputDataEx(
            self._handle, data, len(data), self.AUDIO_TYPE_MULAW
        )
        return bool(ok)

    def _stop(self) -> None:
        if self._lib and self._handle:
            try:
                self._lib.CASClient_VoiceTalkStop(self._handle)
            except Exception as e:
                _LOGGER.debug("VoiceTalkStop: %s", e)
        self._handle = 0


# ─────────────────────────────────────────────────────────────────────────────
# GetDevOperationCodeEx helper
# ─────────────────────────────────────────────────────────────────────────────

def get_operation_code_sync(
    lib: ctypes.CDLL,
    vtm_ip: str,
    vtm_port: int,
    session_id: str,
    client_no: str,
    device_serial: str,
) -> tuple[str, str]:
    """Call CASClient_GetDevOperationCodeEx and return (operation_code, key).

    This function connects to the VTM relay server using the cloud session
    and retrieves a per-session operation code for the given device.

    Signature (reconstructed from JNI analysis):
        bool CASClient_GetDevOperationCodeEx(
            ST_SERVER_INFO* server,
            const char*     sessionId,
            const char*     userId,       // == client_no (hardware code)
            const char**    serials,
            int             count,
            ST_DEV_INFO*    outInfos      // array, count elements
        )
    """
    lib.CASClient_GetDevOperationCodeEx.restype  = ctypes.c_bool
    lib.CASClient_GetDevOperationCodeEx.argtypes = [
        ctypes.POINTER(ST_SERVER_INFO),
        ctypes.c_char_p,   # sessionId
        ctypes.c_char_p,   # userId / hardwareCode
        ctypes.POINTER(ctypes.c_char_p),  # serial list
        ctypes.c_int,      # count
        ctypes.POINTER(ST_DEV_INFO),      # out
    ]

    server = ST_SERVER_INFO()
    server.nServerPort = vtm_port
    server.szServerIP  = vtm_ip.encode()

    serial_buf = ctypes.c_char_p(device_serial.encode())
    serial_arr = (ctypes.c_char_p * 1)(serial_buf)
    out_dev    = (ST_DEV_INFO * 1)()

    ok = lib.CASClient_GetDevOperationCodeEx(
        ctypes.byref(server),
        session_id.encode(),
        client_no.encode(),
        serial_arr,
        1,
        out_dev,
    )

    if not ok:
        err = getattr(lib, "CASClient_GetLastError", lambda: -1)()
        raise RuntimeError(f"GetDevOperationCodeEx failed (err={err})")

    dev = out_dev[0]
    op_code = dev.szOperationCode.decode(errors="replace")
    key     = dev.szKey.decode(errors="replace")
    _LOGGER.debug("GetDevOperationCodeEx ok: op_code=%s…", op_code[:20])
    return op_code, key


# ─────────────────────────────────────────────────────────────────────────────
# Audio helpers
# ─────────────────────────────────────────────────────────────────────────────

def _generate_tone_wav(freq: int = 440, duration: float = 5.0, rate: int = 44100) -> bytes:
    n = int(rate * duration)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        for i in range(n):
            v = int(32767 * math.sin(2 * math.pi * freq * i / rate))
            wf.writeframes(struct.pack("<h", v))
    return buf.getvalue()


def wav_to_mulaw(wav_bytes: bytes) -> bytes:
    """Convert WAV to raw G.711 µ-law 8 kHz mono (stdlib audioop)."""
    try:
        import audioop
    except ImportError:
        raise RuntimeError("audioop not available; use Python < 3.13 or install audioop-lts")

    with wave.open(io.BytesIO(wav_bytes)) as wf:
        channels     = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate    = wf.getframerate()
        pcm          = wf.readframes(wf.getnframes())

    if channels == 2:
        pcm = audioop.tomono(pcm, sample_width, 0.5, 0.5)
    if sample_width != 2:
        pcm = audioop.lin2lin(pcm, sample_width, 2)
        sample_width = 2
    if framerate != 8000:
        pcm, _ = audioop.ratecv(pcm, sample_width, 1, framerate, 8000, None)

    return audioop.lin2ulaw(pcm, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone integration test
# ─────────────────────────────────────────────────────────────────────────────

async def _integration_test() -> None:
    """
    Connects to HikConnect, fetches VTM + operation code, starts a voice
    talk session and streams a 5-second 440 Hz tone to the doorbell.

    Requires .env with:
        NIKO_USERNAME, NIKO_PASSWORD, NIKO_DEVICE_SERIAL
    and the libraries set up by setup_libs.sh on the Pi.
    """
    import importlib.util
    import sys
    import json
    from pathlib import Path

    # ── load env ──────────────────────────────────────────────────────────────
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    username = os.environ.get("NIKO_USERNAME", "")
    password = os.environ.get("NIKO_PASSWORD", "")
    serial   = os.environ.get("NIKO_DEVICE_SERIAL", "")

    if not (username and password and serial):
        print("❌  Set NIKO_USERNAME / NIKO_PASSWORD / NIKO_DEVICE_SERIAL in .env")
        return

    # ── load api module ───────────────────────────────────────────────────────
    pkg_root = Path(__file__).parent.parent / "custom_components" / "niko_access_control"
    import types as _types

    _pkg = "niko_access_control"
    if _pkg not in sys.modules:
        stub = _types.ModuleType(_pkg)
        stub.__path__ = [str(pkg_root)]
        stub.__package__ = _pkg
        sys.modules[_pkg] = stub

    const_key = f"{_pkg}.const"
    if const_key not in sys.modules:
        spec = importlib.util.spec_from_file_location(const_key, pkg_root / "const.py")
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = _pkg
        sys.modules[const_key] = mod
        spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(f"{_pkg}.api", pkg_root / "api.py")
    api_mod = importlib.util.module_from_spec(spec)
    api_mod.__package__ = _pkg
    sys.modules[f"{_pkg}.api"] = api_mod
    spec.loader.exec_module(api_mod)

    import aiohttp
    HikConnectAPI = api_mod.HikConnectAPI
    SEP = "=" * 70

    async with aiohttp.ClientSession() as http:
        api = HikConnectAPI(http)

        # ── login ─────────────────────────────────────────────────────────────
        print(f"\n{SEP}\nLOGIN\n{SEP}")
        await api.login(username, password)
        print(f"  ✅ Logged in  session={api._session_id[:12]}…  client_no={api._client_no[:12]}…")

        # ── fetch VTM server ──────────────────────────────────────────────────
        print(f"\n{SEP}\nVTM SERVER INFO\n{SEP}")
        vtm_resp = await api._get(f"/v3/streaming/vtm/{serial}/1")
        print(json.dumps(vtm_resp, indent=2))

        vtm_data = (vtm_resp.get("data") or {}).get("streamServerConfig") or {}
        vtm_ip   = vtm_data.get("externalIp") or vtm_data.get("internalIp", "")
        vtm_port = int(vtm_data.get("port", 0))

        if not vtm_ip or not vtm_port:
            print(f"❌  Could not parse VTM server from response")
            return
        print(f"\n  VTM: {vtm_ip}:{vtm_port}")

        # ── get operation code ────────────────────────────────────────────────
        print(f"\n{SEP}\nGET OPERATION CODE\n{SEP}")
        lib_dir = os.environ.get("HIK_LIB_DIR") or str(Path.home() / "hik_voice_libs")
        lib = _load_libs(lib_dir)
        print("  ✅ Native library loaded")

        try:
            lib.CASClient_InitLib(1, b"")
            print("  ✅ CASClient_InitLib")
        except Exception as e:
            print(f"  ⚠️  CASClient_InitLib: {e}")

        op_code, key = get_operation_code_sync(
            lib, vtm_ip, vtm_port,
            api._session_id, api._client_no, serial,
        )
        print(f"  ✅ op_code={op_code[:20]}…  key={key[:20]}…")

        # ── generate audio ────────────────────────────────────────────────────
        print(f"\n{SEP}\nGENERATE TONE + CONVERT TO µ-LAW\n{SEP}")
        wav = _generate_tone_wav(freq=440, duration=5.0)
        pcm = wav_to_mulaw(wav)
        print(f"  WAV  : {len(wav):,} bytes")
        print(f"  µ-law: {len(pcm):,} bytes  (8 kHz G.711)")

        # ── answer call first (optional) ──────────────────────────────────────
        print(f"\n{SEP}\nANSWER CALL\n{SEP}")
        ok = await api.answer_call(serial)
        print(f"  answer_call → {'✅ OK' if ok else '⚠️  Failed (may not be ringing)'}")
        await asyncio.sleep(1)

        # ── stream audio ──────────────────────────────────────────────────────
        print(f"\n{SEP}\nSTREAM AUDIO VIA VTM RELAY\n{SEP}")
        print("  You should hear a 440 Hz tone from the doorbell speaker…")

        talk = CASVoiceTalk(
            vtm_ip=vtm_ip, vtm_port=vtm_port,
            device_serial=serial,
            session_id=api._session_id,
            client_no=api._client_no,
            operation_code=op_code,
            key=key,
            lib_dir=lib_dir,
        )
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, talk._start)
            print("  ✅ VoiceTalk session started")
            ok = await talk.send(pcm)
            print(f"  Audio stream → {'✅ OK' if ok else '❌ Failed'}")
        except Exception as e:
            print(f"  ❌ VoiceTalk error: {e}")
            import traceback; traceback.print_exc()
        finally:
            await loop.run_in_executor(None, talk._stop)
            print("  VoiceTalk stopped")

    print(f"\n{SEP}\nDone.\n{SEP}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_integration_test())
