"""
Live API test script for Niko Access Control (HikConnect).
Reads credentials from .env in the same directory.
"""
import asyncio, hashlib, json, os, sys, uuid
from pathlib import Path
import aiohttp

def load_env():
    p = Path(__file__).parent / ".env"
    if not p.exists(): return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

load_env()

CLIENT_NO = str(uuid.uuid4())

def md5(s): return hashlib.md5(s.encode()).hexdigest()
def pretty(d): return json.dumps(d, indent=2, ensure_ascii=False)

def get_code(body):
    if "meta" in body: return str(body["meta"].get("code", ""))
    return str(body.get("code", ""))

# All candidate base URLs (guardingvision.com is the SDK default for COMMON)
# (base_url, use_oem_headers)
CANDIDATES = [
    ("https://api.guardingvision.com",    True),
    ("https://api.guardingvision.com",    False),   # without OEM headers
    ("https://api.hik-connect.com",       False),
    ("https://apiieu.guardingvision.com", True),
    ("https://apiieu.guardingvision.com", False),
]

# Minimal headers - no OEM-specific ones to start
def base_headers(extra=None, oem=True):
    h = {
        "clientType": "378",   # BuildConfig.APP_TYPE — the real Niko client type
        "clientVersion": "3.0.0",
        "appChannel": "APPSTORE",
        "clientNo": CLIENT_NO,
        "lang": "en-US",
    }
    if oem:
        h["appId"] = "NIKO"
        h["customno"] = "3000078"
    if extra: h.update(extra)
    return h


async def try_login(session, base_url, username, password, oem=True):
    hashed = md5(password)
    data = {
        "account": username,
        "password": hashed,
        "featureCode": CLIENT_NO,
        "clientType": "378",
        "cuName": "NIKO_HA",
        "imageCode": "",
        "smsCode": "",
        "bizType": "",
        "smsToken": "",
    }
    for path in ["/v3/users/login/v5", "/v3/users/login/v2"]:
        url = base_url + path
        label = f"(oem={oem})"
        print(f"  POST {url} {label}")
        try:
            async with session.post(url, data=data, headers=base_headers(oem=oem),
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
                code = get_code(body)
                msg = body.get("msg") or body.get("meta", {}).get("message", "")
                print(f"    → HTTP {resp.status}  code={code}  msg={msg}")
                if code == "200":
                    return body, base_url
        except Exception as e:
            print(f"    → Exception: {e}")
    return None, None


async def test_devices(session, session_id, area_id, base_url):
    print(f"\n{'='*60}\nTEST: List devices")
    headers = base_headers({"sessionId": session_id, "areaId": area_id})
    for path in ["/v3/userdevices/v1/devices", "/v3/devices", "/v3/userdevices"]:
        url = base_url + path
        print(f"  GET {url}")
        try:
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
                code = get_code(body)
                print(f"    → HTTP {resp.status}  code={code}")
                if code == "200":
                    print(pretty(body)[:1000])
                    return
                else:
                    print(f"    msg={body.get('msg') or body.get('meta',{}).get('message','')}")
        except Exception as e:
            print(f"    → Exception: {e}")


async def test_calling(session, session_id, area_id, base_url, device_serial):
    print(f"\n{'='*60}\nTEST: Calling list for {device_serial}")
    headers = base_headers({"sessionId": session_id, "areaId": area_id})
    url = f"{base_url}/v3/calling/{device_serial}/list"
    params = {"msgStatus": 0, "pageSize": 5}
    print(f"  GET {url}  params={params}")
    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
            code = get_code(body)
            print(f"  HTTP {resp.status}  code={code}")
            print(pretty(body))
            if code == "200":
                data = body.get("data", body)
                items = data.get("callingList", [])
                print(f"\n  ✅ Got {len(items)} call(s)")
                if items:
                    c = items[0]
                    print(f"    callingTime: {c.get('callingTime')}")
                    print(f"    status:      {c.get('callingStatus')} (1=answered,0=missed)")
                    print(f"    picUrl:      {c.get('picUrl')}")
    except Exception as e:
        print(f"  ❌ Exception: {e}")


async def test_user_calling(session, session_id, area_id, base_url):
    print(f"\n{'='*60}\nTEST: Calling list (user-wide)")
    headers = base_headers({"sessionId": session_id, "areaId": area_id})
    url = f"{base_url}/v3/calling/listByUser"
    params = {"msgStatus": 0, "pageSize": 5}
    print(f"  GET {url}")
    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
            code = get_code(body)
            print(f"  HTTP {resp.status}  code={code}")
            print(pretty(body)[:1000])
    except Exception as e:
        print(f"  ❌ Exception: {e}")


async def main():
    username = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NIKO_USERNAME", "")
    password = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("NIKO_PASSWORD", "")
    device_serial = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("NIKO_DEVICE_SERIAL")

    if not username or not password:
        sys.exit("ERROR: set NIKO_USERNAME and NIKO_PASSWORD in .env")

    hashed = md5(password)
    print(f"account:  {username}")
    print(f"MD5 pass: {hashed}")
    print(f"clientNo: {CLIENT_NO}")

    login_resp = None
    base_url = None

    async with aiohttp.ClientSession() as session:
        print(f"\n{'='*60}\nTEST: Login (trying all candidate URLs)")
        for candidate, oem in CANDIDATES:
            login_resp, base_url = await try_login(session, candidate, username, password, oem)
            if login_resp: break

        if not login_resp:
            sys.exit("\n⛔ Login failed on all candidate URLs.")

        print(f"\n✅ Login OK!  base_url={base_url}")
        data = login_resp.get("data", login_resp)
        login_session = data.get("loginSession", {})
        session_id = login_session.get("sessionId", "")
        area_id = str(data.get("loginArea", {}).get("id", "0"))
        api_domain = data.get("loginArea", {}).get("apiDomain", "")
        if api_domain:
            base_url = f"https://{api_domain}"
        print(f"  sessionId: {session_id[:20]}...")
        print(f"  areaId:    {area_id}")
        print(f"  effective base_url: {base_url}")

        await test_devices(session, session_id, area_id, base_url)
        await test_user_calling(session, session_id, area_id, base_url)
        if device_serial:
            await test_calling(session, session_id, area_id, base_url, device_serial)

    print("\nDone.")

asyncio.run(main())
