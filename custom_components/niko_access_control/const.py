DOMAIN = "niko_access_control"

CONF_DEVICE_SERIAL = "device_serial"

# HikConnect API base URL (EU region - determined dynamically after login)
API_BASE_URL = "https://api.hik-connect.com"

# API paths
LOGIN_PATH = "/v3/users/login/v5"
REFRESH_SESSION_PATH = "/v3/apigateway/login"
CALLING_LIST_PATH = "/v3/calling/{device_serial}/list"
CALLING_DETAIL_PATH = "/v3/calling/{calling_id}"

# Fixed client headers (mimics the Android app)
CLIENT_TYPE = "378"  # BuildConfig.APP_TYPE for the Niko OEM app
CLIENT_VERSION = "3.0.0"
APP_CHANNEL = "APPSTORE"

DEFAULT_SCAN_INTERVAL = 30  # seconds

HISTORY_SLOTS = 50  # number of call-history image entities (slot 0 = most recent)

# Calling status values
CALLING_STATUS_ANSWERED = 1
CALLING_STATUS_MISSED = 2  # observed in real API responses
