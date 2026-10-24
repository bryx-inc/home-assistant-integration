"""Tests for the HTTP client, against a real local GraphQL endpoint."""

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp import ClientSession, web
from homeassistant.const import __version__ as HA_VERSION
import pytest

from custom_components.bryx911.api import (
    BryxApiError,
    BryxAuthError,
    BryxClient,
    BryxConnectionError,
    endpoint_urls,
)
from custom_components.bryx911.const import API_KEY_HEADER, APP_VERSION, PAGE_SIZE

pytestmark = pytest.mark.usefixtures("socket_enabled")

# What the live API returns, with HTTP 200, for a missing or rejected key.
NOT_AUTHENTICATED = "Exception while fetching data (/client) : Not Authenticated"

PROFILE = {
    "id": "client-1",
    "email": "member@example.com",
    "fullName": "Test Member",
    "initials": "TM",
    "onDuty": True,
}

CREATED_DEVICE = {
    "data": {
        "createDevice": {
            "apiKey": "new-key",
            "device": {"id": "device-1", "name": "Home Assistant"},
        }
    }
}

Reply = Callable[[dict[str, Any]], Awaitable[web.Response]]


class FakeGraphQLServer:
    """A GraphQL HTTP endpoint that records requests and answers through a reply."""

    def __init__(self, reply: Reply) -> None:
        """Initialize the server."""
        self.reply = reply
        self.requests: list[tuple[Any, dict[str, Any]]] = []
        self.runner: web.AppRunner | None = None

    async def start(self) -> str:
        """Start the server and return its URL."""
        app = web.Application()
        app.router.add_post("/graphql", self._handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/graphql"

    async def stop(self) -> None:
        """Stop the server."""
        if self.runner:
            await self.runner.cleanup()

    async def _handler(self, request: web.Request) -> web.Response:
        """Record one request and answer it."""
        body = await request.json()
        self.requests.append((request.headers.copy(), body))
        return await self.reply(body)


def respond(payload: dict[str, Any]) -> Reply:
    """Return a reply that always answers with this JSON payload."""

    async def reply(_body: dict[str, Any]) -> web.Response:
        return web.json_response(payload)

    return reply


@pytest.fixture
async def session():
    """Return an aiohttp session standing in for Home Assistant's shared one."""
    async with ClientSession() as client_session:
        yield client_session


@pytest.fixture
async def start_server():
    """Return a function that starts a fake endpoint, stopped after the test."""
    servers: list[FakeGraphQLServer] = []

    async def start(reply: Reply) -> tuple[str, FakeGraphQLServer]:
        server = FakeGraphQLServer(reply)
        servers.append(server)
        return await server.start(), server

    yield start
    for server in servers:
        await server.stop()


async def test_sign_in_then_use_the_new_key(session, start_server) -> None:
    """Sign-in sends no key, and later requests carry the one it returned."""

    async def reply(body: dict[str, Any]) -> web.Response:
        if "createDevice" in body["query"]:
            return web.json_response(CREATED_DEVICE)
        return web.json_response({"data": {"client": PROFILE}})

    url, server = await start_server(reply)
    client = BryxClient(session, url)

    assert await client.async_create_device(
        "member@example.com", "hunter2", "hardware-1"
    ) == ("new-key", "device-1")
    assert await client.async_get_profile() == PROFILE

    (create_headers, create_body), (profile_headers, _) = server.requests
    assert API_KEY_HEADER not in create_headers
    assert create_body["variables"]["params"]["hardwareId"] == "hardware-1"
    assert create_body["variables"]["params"]["deviceType"] == "HOME_ASSISTANT"
    assert profile_headers[API_KEY_HEADER] == "new-key"
    # The session belongs to Home Assistant, so the client must leave it open.
    assert not session.closed


async def test_update_device_sends_the_full_description(session, start_server) -> None:
    """UpdateDevice clears any field it is not sent, so every field goes out."""
    url, server = await start_server(
        respond({"data": {"updateDevice": {"id": "device-1"}}})
    )

    await BryxClient(session, url, "key").async_update_device("device-1")

    ((headers, body),) = server.requests
    assert headers[API_KEY_HEADER] == "key"
    assert body["variables"] == {
        "deviceId": "device-1",
        "params": {
            "name": "Home Assistant",
            "appVersion": APP_VERSION,
            "deviceInfo": {
                "make": "Home Assistant",
                "manufacturer": "Home Assistant",
                "model": "Bryx 911 custom integration",
                "osName": "Home Assistant",
                "osVersion": HA_VERSION,
            },
        },
    }


def test_app_version_matches_the_manifest() -> None:
    """The version reported to Bryx is the version Home Assistant installed."""
    manifest = Path(__file__).parents[1] / "custom_components/bryx911/manifest.json"

    assert json.loads(manifest.read_text())["version"] == APP_VERSION


@pytest.mark.parametrize(
    ("url", "graphql", "subscriptions"),
    [
        (
            "https://graph.bryx911.com",
            "https://graph.bryx911.com/graphql",
            "wss://graph.bryx911.com/subscriptions",
        ),
        (
            "https://example.com:8443/bryx",
            "https://example.com:8443/bryx/graphql",
            "wss://example.com:8443/bryx/subscriptions",
        ),
    ],
)
def test_endpoints_are_built_from_the_base_url(
    url: str, graphql: str, subscriptions: str
) -> None:
    """Queries and the websocket use separate paths under the server's URL."""
    assert endpoint_urls(url) == (graphql, subscriptions)


@pytest.mark.parametrize(
    ("message", "error"),
    [
        (NOT_AUTHENTICATED, BryxAuthError),
        ("Exception while fetching data (/client) : Boom", BryxApiError),
    ],
)
async def test_graphql_errors_are_translated(
    session, start_server, message, error
) -> None:
    """The API reports failures as HTTP 200 with an errors array."""
    url, _ = await start_server(respond({"errors": [{"message": message}]}))

    with pytest.raises(error, match="Exception while fetching data"):
        await BryxClient(session, url, "stale-key").async_get_profile()


async def test_error_page_is_a_connection_error(session, start_server) -> None:
    """A non-GraphQL error page, such as a proxy's, is a connection problem."""

    async def reply(_body: dict[str, Any]) -> web.Response:
        return web.Response(status=502, text="Bad Gateway", content_type="text/html")

    url, _ = await start_server(reply)

    with pytest.raises(BryxConnectionError):
        await BryxClient(session, url, "key").async_get_profile()


async def test_unreachable_api_is_a_connection_error(session) -> None:
    """A refused connection is a connection problem."""
    client = BryxClient(session, "http://127.0.0.1:1/graphql", "key")

    with pytest.raises(BryxConnectionError):
        await client.async_get_profile()


async def test_slow_reply_is_a_connection_error(session, start_server) -> None:
    """A request that outlives the timeout fails instead of stalling setup."""

    async def reply(_body: dict[str, Any]) -> web.Response:
        await asyncio.sleep(0.5)
        return web.json_response({"data": {"client": PROFILE}})

    url, _ = await start_server(reply)

    with (
        patch("custom_components.bryx911.api.REQUEST_TIMEOUT_SECONDS", 0.05),
        pytest.raises(BryxConnectionError),
    ):
        await BryxClient(session, url, "key").async_get_profile()


async def test_units_follow_pagination(session, start_server) -> None:
    """Every page of the member's units in the requested agency is fetched."""

    async def reply(body: dict[str, Any]) -> web.Response:
        if body["variables"]["after"] is None:
            names = [f"Unit {index:02}" for index in range(PAGE_SIZE)]
            cursor = "page-2"
        else:
            # A unit repeated across pages must not be listed twice.
            names = ["Unit 00", "Unit 50"]
            cursor = None
        edges = [{"node": {"id": f"id-{name}", "name": name}} for name in names]
        connection = {"edges": edges, "pageInfo": {"endCursor": cursor}}
        return web.json_response(
            {"data": {"client": {"id": "client-1", "unitMemberships": connection}}}
        )

    url, server = await start_server(reply)
    units = await BryxClient(session, url, "key").async_get_units("agency-fire")

    assert len(units) == PAGE_SIZE + 1
    assert [unit.name for unit in units] == sorted(unit.name for unit in units)
    assert {unit.agency_id for unit in units} == {"agency-fire"}
    first, second = (body["variables"] for _, body in server.requests)
    assert first == {"agencyId": "agency-fire", "first": PAGE_SIZE, "after": None}
    assert second["after"] == "page-2"


async def test_credentials_kept_out_of_debug_logs(
    session, start_server, caplog
) -> None:
    """Debug traces of request and response bodies mask the password and key."""
    caplog.set_level(logging.DEBUG, logger="gql")
    url, _ = await start_server(respond(CREATED_DEVICE))

    # The embedded quote checks that masking copes with JSON escapes.
    await BryxClient(session, url).async_create_device(
        "member@example.com", 's3cr3t"pa55', "hardware-1"
    )

    assert "createDevice" in caplog.text
    assert "**REDACTED**" in caplog.text
    for secret in ("s3cr3t", "pa55", "new-key"):
        assert secret not in caplog.text
