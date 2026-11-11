DOMAIN = "niko_access_control"

CONF_DEVICE_SERIAL = "device_serial"

# HikConnect API base URL (EU region - determined dynamically after login)
API_BASE_URL = "https://api.hik-connect.com"

# API paths
LOGIN_PATH = "/v3/users/login/v5"
REFRESH_SESSION_PATH = "/v3/apigateway/login"
CALLING_LIST_PATH = "/v3/calling/{device_serial}/list"
CALLING_DETAIL_PATH = "/v3/calling/{calling_id}"

# Fixed client headers
CLIENT_TYPE = "378"
CLIENT_VERSION = "3.0.0"
APP_CHANNEL = "APPSTORE"

DEFAULT_SCAN_INTERVAL = 30  # seconds (call history, when live call state is available)
FALLBACK_SCAN_INTERVAL = 10  # seconds (call history, when it is the only way to detect a ring)
LIVE_STATE_INTERVAL = 5  # seconds

HISTORY_SLOTS = 50  # number of calls fetched from the cloud per poll

DEVICE_LIST_INTERVAL = 300  # seconds between device info / device list refreshes

# Live call state from /v3/devconfig/v1/call/{serial}/status.
# Mapping: 1 = idle, 2 = ring, 3 = on call.
CALL_STATE_IDLE = "idle"
CALL_STATE_RINGING = "ringing"
CALL_STATE_ON_CALL = "on_call"
CALL_STATES = [CALL_STATE_IDLE, CALL_STATE_RINGING, CALL_STATE_ON_CALL]
CALL_STATUS_MAP = {1: CALL_STATE_IDLE, 2: CALL_STATE_RINGING, 3: CALL_STATE_ON_CALL}
CALLER_INFO_STATUS_MAP = {"idle": CALL_STATE_IDLE, "ring": CALL_STATE_RINGING, "onCall": CALL_STATE_ON_CALL}

# Calling status values
CALLING_STATUS_ANSWERED = 1
CALLING_STATUS_MISSED = 2  # observed in real API responses
CALL_RESULTS = ["answered", "missed"]

# Notifications (options)
CONF_NOTIFY_MODE = "notify_mode"
CONF_NOTIFY_DEVICES = "notify_devices"
CONF_NOTIFY_TITLE = "notify_title"
CONF_NOTIFY_MESSAGE = "notify_message"
CONF_NOTIFY_TIME_SENSITIVE = "notify_time_sensitive"
NOTIFY_MODE_OFF = "off"
NOTIFY_MODE_ALL = "all"
NOTIFY_MODE_SELECTED = "selected"
DEFAULT_NOTIFY_TITLE = "Doorbell"
DEFAULT_NOTIFY_MESSAGE = "Someone rang at {time} on {date}"

# Dispatcher signal sent by the doorbell event entity on each ring
SIGNAL_RING = "niko_access_control_ring_{entry_id}"
