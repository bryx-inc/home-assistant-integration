"""HTTP client for the Bryx 911 GraphQL API."""

import logging
import re
from typing import Any

import aiohttp
from gql import Client, GraphQLRequest
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportError, TransportQueryError
from homeassistant.const import __version__ as HA_VERSION
from yarl import URL

from .const import (
    API_KEY_HEADER,
    APP_VERSION,
    DEVICE_NAME,
    DEVICE_TYPE,
    PAGE_SIZE,
    REQUEST_TIMEOUT_SECONDS,
)
from .models import BryxAgency, BryxResponseOption, BryxUnit
from .queries import (
    AGENCY_MEMBERSHIPS,
    CLIENT_PROFILE,
    CREATE_DEVICE,
    SET_DUTY_STATUS,
    UNIT_MEMBERSHIPS,
    UPDATE_DEVICE,
)

_LOGGER = logging.getLogger(__name__)

# The API answers with HTTP 200 and an "errors" array rather than a status code,
# so authentication problems have to be recognized from the message text. The
# websocket reports a rejected key the same way, once the subscription starts.
_AUTH_ERRORS = ("not authenticated", "not authorized")

# gql traces request and websocket bodies at debug level. Those bodies carry the
# password sent to createDevice, the API key it returns and the key sent in
# connection_init, so the values are masked before any log handler sees them.
_GQL_TRACE_LOGGERS = ("gql.transport.aiohttp", "gql.transport.common.base")
_CREDENTIAL = re.compile(
    rf'("(?:password|apiKey|{re.escape(API_KEY_HEADER)})"\s*:\s*)"(?:[^"\\]|\\.)*"'
)


class _RedactCredentials(logging.Filter):
    """Mask credentials in gql's debug traces."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite any string argument that carries a credential."""
        if isinstance(record.args, tuple):
            record.args = tuple(
                _CREDENTIAL.sub(r'\1"**REDACTED**"', arg)
                if isinstance(arg, str)
                else arg
                for arg in record.args
            )
        return True


_REDACT_CREDENTIALS = _RedactCredentials()
for _logger_name in _GQL_TRACE_LOGGERS:
    logging.getLogger(_logger_name).addFilter(_REDACT_CREDENTIALS)


class BryxError(Exception):
    """Base error for the Bryx API."""


class BryxConnectionError(BryxError):
    """Raised when the API cannot be reached."""


class BryxAuthError(BryxError):
    """Raised when credentials or the stored API key are rejected."""


class BryxApiError(BryxError):
    """Raised when the API returns an error unrelated to authentication."""


def graphql_error(errors: list[Any] | None) -> BryxAuthError | BryxApiError:
    """Translate a GraphQL errors array into an exception."""
    messages = [
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
        for error in errors or []
    ]
    joined = "; ".join(messages)
    if any(
        auth_error in message.lower()
        for message in messages
        for auth_error in _AUTH_ERRORS
    ):
        return BryxAuthError(joined)
    return BryxApiError(joined)


def endpoint_urls(url: str) -> tuple[str, str]:
    """Return the GraphQL and websocket URLs for an API server's base URL.

    The websocket lives on /subscriptions; /graphql rejects upgrade requests.
    """
    base = URL(url)
    return str(base / "graphql"), str(base.with_scheme("wss") / "subscriptions")


class _SharedSessionTransport(AIOHTTPTransport):
    """Run gql's HTTP transport on Home Assistant's shared aiohttp session.

    AIOHTTPTransport opens a private session on connect and closes it on close.
    This session belongs to Home Assistant and other integrations use it too, so
    connecting and closing leave it alone.
    """

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        """Initialize the transport."""
        super().__init__(url)
        self.session = session

    async def connect(self) -> None:
        """Keep using the shared session, which is already open."""

    async def close(self) -> None:
        """Leave the shared session open for the rest of Home Assistant."""


class BryxClient:
    """Talk to the Bryx 911 GraphQL API over HTTP."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str | None = None,
    ) -> None:
        """Initialize the client."""
        self._url = url
        self._client = Client(
            transport=_SharedSessionTransport(session, url),
            execute_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self.api_key = api_key

    async def async_execute(
        self, request: GraphQLRequest, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a GraphQL document and return its data payload."""
        # Headers go on each request: the transport only applies its own to a
        # session it creates, and the key changes once createDevice returns one.
        headers = {API_KEY_HEADER: self.api_key} if self.api_key else {}
        try:
            async with self._client as session:
                return await session.execute(
                    GraphQLRequest(request, variable_values=variables),
                    extra_args={"headers": headers},
                )
        except TransportQueryError as err:
            raise graphql_error(err.errors) from err
        except (TransportError, TimeoutError) as err:
            raise BryxConnectionError(f"Error talking to {self._url}: {err}") from err

    @staticmethod
    def _device_params(name: str) -> dict[str, Any]:
        """Return the device description both device mutations require.

        updateDevice overwrites every one of these fields, and clears any field
        left out, so each call must send the full description.
        """
        return {
            "name": name,
            "appVersion": APP_VERSION,
            "deviceInfo": {
                "make": "Home Assistant",
                "manufacturer": "Home Assistant",
                "model": "Bryx 911 custom integration",
                "osName": "Home Assistant",
                "osVersion": HA_VERSION,
            },
        }

    async def async_create_device(
        self,
        email: str,
        password: str,
        hardware_id: str,
        name: str = DEVICE_NAME,
    ) -> tuple[str, str]:
        """Sign in and register this Home Assistant instance as a device.

        Returns the API key and the device id. Reusing the same hardware_id on a
        later call refreshes the existing device rather than adding another one.
        """
        data = await self.async_execute(
            CREATE_DEVICE,
            {
                "params": {
                    **self._device_params(name),
                    "email": email,
                    "password": password,
                    "hardwareId": hardware_id,
                    "deviceType": DEVICE_TYPE,
                }
            },
        )
        session = data["createDevice"]
        self.api_key = session["apiKey"]
        return session["apiKey"], session["device"]["id"]

    async def async_update_device(
        self, device_id: str, name: str = DEVICE_NAME
    ) -> None:
        """Send the current device description so Bryx keeps the device.

        Each call pushes back the date Bryx deletes the device as unused. The API
        refuses an unknown device id with "Not Authorized", which surfaces as
        BryxAuthError; the caller treats that as needing reauthentication.
        """
        await self.async_execute(
            UPDATE_DEVICE,
            {"deviceId": device_id, "params": self._device_params(name)},
        )

    async def async_get_profile(self) -> dict[str, Any]:
        """Return the signed-in member's profile."""
        data = await self.async_execute(CLIENT_PROFILE)
        return data["client"]

    async def _async_get_all(
        self,
        request: GraphQLRequest,
        field: str,
        variables: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return every node of a paginated field on the signed-in member."""
        nodes: list[dict[str, Any]] = []
        after: str | None = None

        while True:
            data = await self.async_execute(
                request, {**(variables or {}), "first": PAGE_SIZE, "after": after}
            )
            connection = data["client"][field]
            edges = connection["edges"]
            nodes.extend(edge["node"] for edge in edges)

            cursor = connection["pageInfo"]["endCursor"]
            # Stop on a short page, a missing cursor, or a cursor that did not
            # advance, so a misbehaving server cannot spin this loop forever.
            if len(edges) < PAGE_SIZE or not cursor or cursor == after:
                return nodes
            after = cursor

    async def async_get_agencies(self) -> list[BryxAgency]:
        """Return every agency the signed-in member belongs to."""
        agencies: dict[str, BryxAgency] = {}
        for node in await self._async_get_all(AGENCY_MEMBERSHIPS, "agencyMemberships"):
            agency = node["agency"]
            agencies.setdefault(
                agency["id"],
                BryxAgency(
                    id=agency["id"],
                    name=agency["name"],
                    response_options=[
                        BryxResponseOption(
                            id=option["id"],
                            text=option["text"],
                            is_positive=option["isPositive"],
                        )
                        for option in agency.get("responseOptions") or []
                    ],
                ),
            )
        return sorted(agencies.values(), key=lambda agency: agency.name)

    async def async_get_units(self, agency_id: str) -> list[BryxUnit]:
        """Return the units the signed-in member belongs to in one agency."""
        units: dict[str, BryxUnit] = {}
        for node in await self._async_get_all(
            UNIT_MEMBERSHIPS, "unitMemberships", {"agencyId": agency_id}
        ):
            units.setdefault(
                node["id"],
                BryxUnit(id=node["id"], name=node["name"], agency_id=agency_id),
            )
        return sorted(units.values(), key=lambda unit: unit.name)

    async def async_set_duty_status(self, client_id: str, on_duty: bool) -> bool:
        """Put the signed-in member on or off duty."""
        data = await self.async_execute(
            SET_DUTY_STATUS, {"clientId": client_id, "params": {"onDuty": on_duty}}
        )
        return bool(data["setClientDutyStatus"]["onDuty"])
