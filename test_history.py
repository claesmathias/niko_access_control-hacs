"""
Full call history + live-view test for Niko Access Control.
Reads credentials from .env — run with:
    .venv/bin/python3 test_history.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

import aiohttp

# ── env ──────────────────────────────────────────────────────────────────────

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

USERNAME      = os.environ["NIKO_USERNAME"]
PASSWORD      = os.environ["NIKO_PASSWORD"]
DEVICE_SERIAL = os.environ["NIKO_DEVICE_SERIAL"]
BASE_URL      = "https://api.guardingvision.com"
CLIENT_NO     = str(uuid.uuid4())

# ── helpers ───────────────────────────────────────────────────────────────────

def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()

def pretty(d) -> str:
    return json.dumps(d, indent=2, ensure_ascii=False)

def code(body: dict) -> str:
    return str(body.get("meta", {}).get("code", body.get("code", "")))

def headers(session_id: str = "", area_id: str = "0") -> dict:
    h = {
        "clientType": "378",
        "clientVersion": "3.0.0",
        "appChannel": "APPSTORE",
        "appId": "NIKO",
        "customno": "3000078",
        "clientNo": CLIENT_NO,
        "lang": "en-US",
        "areaId": area_id,
    }
    if session_id:
        h["sessionId"] = session_id
    return h

SEP = "=" * 70

# ── login ─────────────────────────────────────────────────────────────────────

async def login(session: aiohttp.ClientSession) -> tuple[str, str, str]:
    """Return (session_id, area_id, base_url)."""
    print(f"\n{SEP}\nLOGIN\n{SEP}")
    data = {
        "account": USERNAME,
        "password": md5(PASSWORD),
        "featureCode": CLIENT_NO,
        "clientType": "378",
        "cuName": "NIKO_TEST",
        "imageCode": "",
        "smsCode": "",
        "bizType": "",
        "smsToken": "",
    }
    async with session.post(
        BASE_URL + "/v3/users/login/v5",
        data=data,
        headers=headers(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)

    if code(body) != "200":
        raise RuntimeError(f"Login failed: {body}")

    session_id = body.get("loginSession", {}).get("sessionId", "")
    area       = body.get("loginArea", {})
    area_id    = str(area.get("id", "0"))
    base_url   = f"https://{area['apiDomain']}" if area.get("apiDomain") else BASE_URL
    print(f"  ✅ Logged in   base={base_url}  area={area_id}")
    return session_id, area_id, base_url


# ── call history ──────────────────────────────────────────────────────────────

async def get_all_calls(
    session: aiohttp.ClientSession,
    sid: str,
    area_id: str,
    base_url: str,
) -> list[dict]:
    """Fetch all calls (read + unread) and return merged list."""
    print(f"\n{SEP}\nCALL HISTORY  (device {DEVICE_SERIAL})\n{SEP}")
    h = headers(sid, area_id)
    path = f"{base_url}/v3/calling/{DEVICE_SERIAL}/list"

    combined: dict[str, dict] = {}
    for status in (0, 1):
        params = {"msgStatus": status, "pageSize": 50}
        async with session.get(path, params=params, headers=h,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
        items = body.get("data", []) if code(body) == "200" else []
        for item in (items if isinstance(items, list) else []):
            cid = item.get("callingId", "")
            if cid and cid not in combined:
                combined[cid] = item

    calls = sorted(combined.values(), key=lambda c: c.get("callingTime", ""), reverse=True)
    print(f"\n  Found {len(calls)} call(s) total\n")

    for i, c in enumerate(calls, 1):
        status_int = c.get("callingStatus", 0)
        status_lbl = {1: "✅ answered", 2: "❌ missed"}.get(status_int, f"? ({status_int})")
        pic        = c.get("picUrl") or "—"
        vid        = c.get("videoUrl") or c.get("recordUrl") or c.get("video") or "—"
        custom     = c.get("customInfo") or "{}"
        handler    = ""
        try:
            handler = json.loads(custom.replace('/\"', '"')).get("handler", "")
        except Exception:
            pass

        print(f"  [{i:02d}]  {c.get('callingTime')}  {status_lbl}")
        print(f"         callingId : {c.get('callingId')}")
        print(f"         channel   : {c.get('channelNo')}")
        print(f"         handler   : {handler}")
        print(f"         pic_url   : {pic[:80]}{'…' if len(pic) > 80 else ''}")
        print(f"         video_url : {vid[:80]}{'…' if len(vid) > 80 else ''}")
        print()

    print("  Raw first call (full):")
    if calls:
        print(pretty(calls[0]))

    return calls


# ── calling detail ────────────────────────────────────────────────────────────

async def get_call_detail(
    session: aiohttp.ClientSession,
    sid: str,
    area_id: str,
    base_url: str,
    calling_id: str,
) -> None:
    print(f"\n{SEP}\nCALL DETAIL  (id={calling_id})\n{SEP}")
    h = headers(sid, area_id)
    async with session.get(
        f"{base_url}/v3/calling/{calling_id}",
        headers=h,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)
    print(pretty(body))


# ── live view ─────────────────────────────────────────────────────────────────

async def get_live_view(
    session: aiohttp.ClientSession,
    sid: str,
    area_id: str,
    base_url: str,
) -> None:
    print(f"\n{SEP}\nLIVE VIEW ENDPOINTS\n{SEP}")
    h = headers(sid, area_id)

    # 1. Call infos (may contain stream token/URL)
    print("\n  GET /v3/devconfig/v1/call/{serial}/infos")
    async with session.get(
        f"{base_url}/v3/devconfig/v1/call/{DEVICE_SERIAL}/infos",
        headers=h, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)
    print(f"  HTTP {resp.status}  code={code(body)}")
    print(pretty(body))

    # 2. Call status
    print("\n  GET /v3/devconfig/v1/call/{serial}/status")
    async with session.get(
        f"{base_url}/v3/devconfig/v1/call/{DEVICE_SERIAL}/status",
        headers=h, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)
    print(f"  HTTP {resp.status}  code={code(body)}")
    print(pretty(body))

    # 3. Device channel net info (may expose RTSP URL)
    print("\n  POST /api/device/channel/net")
    async with session.post(
        f"{base_url}/api/device/channel/net",
        data={"subSerial": DEVICE_SERIAL},
        headers=h, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)
    print(f"  HTTP {resp.status}  code={code(body)}")
    print(pretty(body))

    # 4. ISAPI device info via transparent channel
    print("\n  ISAPI GET /ISAPI/System/deviceInfo")
    async with session.post(
        f"{base_url}/api/device/isapi",
        data={
            "subSerial": DEVICE_SERIAL,
            "cmdId": "19713",
            "transmissionData": "GET /ISAPI/System/deviceInfo?format=json\r\n",
        },
        headers=h, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)
    print(f"  HTTP {resp.status}  code={code(body)}")
    raw = body.get("data") or body.get("msg", "")
    try:
        print(pretty(json.loads(raw)))
    except Exception:
        print(raw)

    # 5. Caller info (real-time)
    print("\n  ISAPI GET /ISAPI/VideoIntercom/callerInfo")
    async with session.post(
        f"{base_url}/api/device/isapi",
        data={
            "subSerial": DEVICE_SERIAL,
            "cmdId": "19713",
            "transmissionData": "GET /ISAPI/VideoIntercom/callerInfo?format=json\r\n",
        },
        headers=h, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        body = await resp.json(content_type=None)
    print(f"  HTTP {resp.status}  code={code(body)}")
    raw = body.get("data") or body.get("msg", "")
    try:
        print(pretty(json.loads(raw)))
    except Exception:
        print(raw)


# ── picture download ───────────────────────────────────────────────────────────

async def test_download_picture(
    session: aiohttp.ClientSession,
    sid: str,
    area_id: str,
    pic_url: str,
) -> None:
    print(f"\n{SEP}\nPICTURE DOWNLOAD\n{SEP}")
    print(f"  URL: {pic_url[:100]}…")
    h = headers(sid, area_id)
    async with session.get(pic_url, headers=h,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        print(f"  HTTP {resp.status}  content-type={resp.content_type}")
        if resp.status == 200:
            data = await resp.read()
            out = Path("test_snapshot.jpg")
            out.write_bytes(data)
            print(f"  ✅ Saved {len(data):,} bytes → {out}")
        else:
            print(f"  ❌ Failed: {await resp.text()}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with aiohttp.ClientSession() as session:
        sid, area_id, base_url = await login(session)

        calls = await get_all_calls(session, sid, area_id, base_url)

        if calls:
            await get_call_detail(session, sid, area_id, base_url, calls[0]["callingId"])

            if calls[0].get("picUrl"):
                await test_download_picture(session, sid, area_id, calls[0]["picUrl"])

        await get_live_view(session, sid, area_id, base_url)

    print(f"\n{SEP}\nDone.\n{SEP}\n")


asyncio.run(main())
