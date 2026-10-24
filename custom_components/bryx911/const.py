"""Constants for the Bryx 911 integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "bryx911"

# Config entry keys.
CONF_API_KEY: Final = "api_key"
CONF_CLIENT_ID: Final = "client_id"
CONF_DEVICE_ID: Final = "device_id"
CONF_HARDWARE_ID: Final = "hardware_id"

# The config flow's collapsed section that holds the server URL.
CONF_ADVANCED: Final = "advanced"

# Options keys.
CONF_AGENCIES: Final = "agencies"
# Units cleared in the options. Every other unit in a tracked agency gets a
# device, including units the member joins later.
CONF_EXCLUDED_UNITS: Final = "excluded_units"
# The options flow's unit picker. It shows the units being kept, and the flow
# saves the others as CONF_EXCLUDED_UNITS.
CONF_UNITS: Final = "units"
CONF_EVENT_DURATION: Final = "event_duration"

# The production API server. An entry stores the server it signed in to as a
# base URL, and the GraphQL and websocket endpoints are built from it.
DEFAULT_URL: Final = "https://graph.bryx911.com"

# The API authenticates both HTTP requests and the websocket with this key. Over
# the websocket it must travel in the connection_init payload; the server ignores
# it on the upgrade request's headers.
API_KEY_HEADER: Final = "X-API-KEY"

# graphql-transport-ws is the only subprotocol the server accepts. Connecting
# without one, or with the legacy "graphql-ws", is rejected with HTTP 400.
WS_SUBPROTOCOL: Final = "graphql-transport-ws"

# The device record this integration registers against the Bryx account.
DEVICE_TYPE: Final = "HOME_ASSISTANT"
DEVICE_NAME: Final = "Home Assistant"

# Reported to the API as the client application version. Keep in step with the
# version in manifest.json.
APP_VERSION: Final = "2.0.0"

# Every disposition, so a job can never leave the subscription's filter set. The
# server uses DELETED to mean "left the filter", not "job removed", so tracking
# all four means every change arrives as UPDATED and nothing is lost.
TRACKED_DISPOSITIONS: Final = ["OPEN", "OLD", "CLOSED", "TIMED_OUT"]

# A job is still an active call while it is OPEN or OLD; CLOSED and TIMED_OUT
# are both finished. OLD is an open job that has aged, not a closed one.
OPEN_DISPOSITIONS: Final = frozenset({"OPEN", "OLD"})

# The API rejects a page size outside 1..50.
PAGE_SIZE: Final = 50

# How long one HTTP request may run before it counts as a connection failure.
REQUEST_TIMEOUT_SECONDS: Final = 10

# How many agencies' unit memberships to fetch at once during setup.
UNIT_FETCH_CONCURRENCY: Final = 5

# Job updates arrive in bursts: the same job is pushed several times in a row.
# Coalesce them so entity state is not rewritten once per message.
UPDATE_DEBOUNCE_SECONDS: Final = 0.5

# There is no subscription for the signed-in client, so duty status is polled.
POLL_INTERVAL: Final = timedelta(minutes=5)

# How often to call updateDevice after the call at setup. Each call pushes back
# the date Bryx deletes the device as unused.
DEVICE_KEEPALIVE_INTERVAL: Final = timedelta(hours=1)

# Websocket keepalive. The server never sends graphql-transport-ws pings, so the
# connection is kept alive with websocket-level ping frames instead.
WS_HEARTBEAT_SECONDS: Final = 20
WS_ACK_TIMEOUT_SECONDS: Final = 15
WS_RECONNECT_MIN_SECONDS: Final = 2
WS_RECONNECT_MAX_SECONDS: Final = 300

# Home Assistant rejects a state longer than 255 characters. Real synopsis values
# have been observed at 248 characters, so they are truncated defensively.
MAX_STATE_LENGTH: Final = 255

# How many jobs to keep in memory for the calendar and history attributes.
MAX_TRACKED_JOBS: Final = 250

DEFAULT_EVENT_DURATION_MINUTES: Final = 60
