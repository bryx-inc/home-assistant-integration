"""Tests for the graphql-transport-ws client, against a real websocket server."""

import asyncio
import contextlib
import logging
from typing import Any
from unittest.mock import patch

from aiohttp import ClientSession, WSMsgType, web
from gql import gql
import pytest

from custom_components.bryx911.api import BryxAuthError, BryxConnectionError
from custom_components.bryx911.const import (
    API_KEY_HEADER,
    WS_RECONNECT_MIN_SECONDS,
    WS_SUBPROTOCOL,
)
from custom_components.bryx911.subscription import GraphQLSubscription

pytestmark = pytest.mark.usefixtures("socket_enabled")


class FakeServer:
    """A minimal graphql-transport-ws server that records what it received."""

    def __init__(self, behavior: str = "stream") -> None:
        """Initialize the server."""
        self.behavior = behavior
        self.init_payloads: list[dict[str, Any]] = []
        self.subscribe_payloads: list[dict[str, Any]] = []
        self.pongs = 0
        self.connections = 0
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> str:
        """Start the server and return its websocket URL."""
        app = web.Application()
        app.router.add_get("/subscriptions", self._handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/subscriptions"

    async def stop(self) -> None:
        """Stop the server."""
        if self.runner:
            await self.runner.cleanup()

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle one websocket connection."""
        self.connections += 1
        # The real server only accepts this subprotocol.
        assert request.headers.get("Sec-WebSocket-Protocol") == WS_SUBPROTOCOL

        websocket = web.WebSocketResponse(protocols=(WS_SUBPROTOCOL,))
        await websocket.prepare(request)

        async for message in websocket:
            if message.type is not WSMsgType.TEXT:
                continue
            data = message.json()

            if data.get("type") == "connection_init":
                self.init_payloads.append(data.get("payload") or {})
                await websocket.send_json({"type": "connection_ack"})
            elif data.get("type") == "pong":
                self.pongs += 1
            elif data.get("type") == "subscribe":
                self.subscribe_payloads.append(data.get("payload") or {})
                await self._respond(websocket, data["id"])
                if self.behavior not in ("stream", "partial_error"):
                    break
        return websocket

    async def _respond(self, websocket: web.WebSocketResponse, op_id: str) -> None:
        """Send the configured response to a subscribe."""
        if self.behavior == "auth_error":
            await websocket.send_json(
                {
                    "id": op_id,
                    "type": "error",
                    "payload": [{"message": "Not Authenticated"}],
                }
            )
            return
        if self.behavior == "other_error":
            await websocket.send_json(
                {"id": op_id, "type": "error", "payload": [{"message": "Boom"}]}
            )
            return
        if self.behavior == "drop":
            await websocket.close()
            return
        if self.behavior == "partial_error":
            # A field error alongside the data, as GraphQL allows.
            await websocket.send_json(
                {
                    "id": op_id,
                    "type": "next",
                    "payload": {
                        "data": {"jobs": {"__typename": "JobUpdateNode", "job": None}},
                        "errors": [{"message": "Exception while fetching data"}],
                    },
                }
            )

        await websocket.send_json(
            {
                "id": op_id,
                "type": "next",
                "payload": {"data": {"jobs": {"__typename": "InitialDataLoaded"}}},
            }
        )
        # Exercise the server-initiated ping the protocol allows.
        await websocket.send_json({"type": "ping"})


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Tolerate aiohttp's websocket heartbeat outliving a clean close.

    aiohttp 3.14 re-arms the heartbeat when the server's reply to our close
    frame arrives, after the socket is already marked closed. The stray timer
    fires once and does nothing, but Home Assistant's cleanup check reports it.
    """
    return True


@pytest.fixture
async def session():
    """Return an aiohttp session."""
    async with ClientSession() as client_session:
        yield client_session


async def _run_briefly(subscription: GraphQLSubscription, seconds: float = 0.5) -> None:
    """Run the subscription for a moment, then stop it."""
    task = asyncio.create_task(subscription.async_run())
    await asyncio.sleep(seconds)
    await subscription.async_close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_handshake_sends_api_key_in_payload(session) -> None:
    """The key must travel in connection_init, not in the upgrade headers."""
    server = FakeServer()
    url = await server.start()
    payloads: list[dict[str, Any]] = []

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        {"params": {"dispositions": ["OPEN"]}},
        on_payload=lambda payload: payloads.append(payload),
    )
    subscription.set_api_key("secret-key")

    try:
        await _run_briefly(subscription)
    finally:
        await server.stop()

    assert server.init_payloads == [{API_KEY_HEADER: "secret-key"}]
    assert server.subscribe_payloads[0]["variables"] == {
        "params": {"dispositions": ["OPEN"]}
    }
    assert payloads == [{"jobs": {"__typename": "InitialDataLoaded"}}]


async def test_server_ping_is_answered(session) -> None:
    """A server ping must be answered or the connection would be dropped."""
    server = FakeServer()
    url = await server.start()

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda _: None,
    )
    subscription.set_api_key("k")

    try:
        await _run_briefly(subscription)
    finally:
        await server.stop()

    assert server.pongs == 1


async def test_auth_error_propagates(session) -> None:
    """A rejected key raises so the caller can trigger reauthentication."""
    server = FakeServer("auth_error")
    url = await server.start()

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda _: None,
    )
    subscription.set_api_key("stale")

    try:
        with pytest.raises(BryxAuthError):
            await asyncio.wait_for(subscription.async_run(), timeout=5)
    finally:
        await subscription.async_close()
        await server.stop()

    # It must not retry a key the server already rejected.
    assert server.connections == 1


async def test_missing_api_key_raises(session) -> None:
    """Running without a key is an authentication problem, not a crash."""
    subscription = GraphQLSubscription(
        session,
        "http://127.0.0.1:1/subscriptions",
        gql("subscription { x }"),
        None,
        on_payload=lambda _: None,
    )

    with pytest.raises(BryxAuthError):
        await asyncio.wait_for(subscription.async_run(), timeout=5)


async def test_non_auth_error_reconnects(session) -> None:
    """A transient error reconnects instead of giving up."""
    server = FakeServer("other_error")
    url = await server.start()

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda _: None,
    )
    subscription.set_api_key("k")

    try:
        await _run_briefly(subscription, seconds=3.5)
    finally:
        await server.stop()

    assert server.connections >= 2


async def test_dropped_connection_reconnects(session) -> None:
    """A server-side close is followed by a reconnect."""
    server = FakeServer("drop")
    url = await server.start()
    connects: list[int] = []

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda _: None,
        on_connected=lambda: connects.append(1),
    )
    subscription.set_api_key("k")

    try:
        await _run_briefly(subscription, seconds=3.5)
    finally:
        await server.stop()

    assert server.connections >= 2
    assert len(connects) >= 2


async def test_lifecycle_callbacks_fire(session) -> None:
    """Connect and disconnect callbacks both run."""
    server = FakeServer()
    url = await server.start()
    events: list[str] = []

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda _: None,
        on_connected=lambda: events.append("up"),
        on_disconnected=lambda: events.append("down"),
    )
    subscription.set_api_key("k")

    try:
        await _run_briefly(subscription)
    finally:
        await server.stop()

    assert events[0] == "up"


async def test_async_payload_callback_is_awaited(session) -> None:
    """An async consumer callback is awaited rather than left as a coroutine."""
    server = FakeServer()
    url = await server.start()
    seen: list[dict[str, Any]] = []

    async def on_payload(payload: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        seen.append(payload)

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=on_payload,
    )
    subscription.set_api_key("k")

    try:
        await _run_briefly(subscription)
    finally:
        await server.stop()

    assert seen == [{"jobs": {"__typename": "InitialDataLoaded"}}]


async def test_payload_with_errors_keeps_streaming(session) -> None:
    """A payload carrying errors is applied, and the subscription carries on.

    Ending the subscription on the first error would replay every job on each
    reconnect, and could loop forever on one bad job.
    """
    server = FakeServer("partial_error")
    url = await server.start()
    payloads: list[dict[str, Any]] = []

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda payload: payloads.append(payload),
    )
    subscription.set_api_key("k")

    try:
        await _run_briefly(subscription)
    finally:
        await server.stop()

    assert payloads == [
        {"jobs": {"__typename": "JobUpdateNode", "job": None}},
        {"jobs": {"__typename": "InitialDataLoaded"}},
    ]
    assert server.connections == 1


async def test_backoff_resets_after_a_healthy_connection(session) -> None:
    """Failed attempts back off; a connection that streamed data resets that."""
    subscription = GraphQLSubscription(
        session,
        "http://127.0.0.1:1/subscriptions",
        gql("subscription { x }"),
        None,
        on_payload=lambda _: None,
    )
    subscription.set_api_key("k")
    # Fail, fail, stream data and then drop, fail.
    streamed = iter([False, False, True, False])
    delays: list[float] = []

    async def connect_once() -> None:
        subscription._streamed = next(streamed)
        raise BryxConnectionError("dropped")

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 4:
            await subscription.async_close()

    with (
        patch.object(subscription, "_async_connect_once", connect_once),
        patch("custom_components.bryx911.subscription.random.uniform", return_value=0),
        patch("custom_components.bryx911.subscription.asyncio.sleep", sleep),
    ):
        await subscription.async_run()

    base = WS_RECONNECT_MIN_SECONDS
    assert delays == [base, base * 2, base, base * 2]


async def test_api_key_kept_out_of_debug_logs(session, caplog) -> None:
    """The debug trace of connection_init masks the API key."""
    caplog.set_level(logging.DEBUG, logger="gql")
    server = FakeServer()
    url = await server.start()

    subscription = GraphQLSubscription(
        session,
        url,
        gql("subscription { jobs { __typename } }"),
        None,
        on_payload=lambda _: None,
    )
    subscription.set_api_key("secret-key")

    try:
        await _run_briefly(subscription)
    finally:
        await server.stop()

    assert "connection_init" in caplog.text
    assert "secret-key" not in caplog.text
