"""graphql-transport-ws subscription client for the Bryx 911 API."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
import logging
import random
from typing import Any

import aiohttp
from gql import Client, GraphQLRequest
from gql.transport.aiohttp_websockets import AIOHTTPWebsocketsTransport
from gql.transport.exceptions import TransportError, TransportQueryError

from .api import BryxAuthError, BryxConnectionError, graphql_error
from .const import (
    API_KEY_HEADER,
    WS_ACK_TIMEOUT_SECONDS,
    WS_HEARTBEAT_SECONDS,
    WS_RECONNECT_MAX_SECONDS,
    WS_RECONNECT_MIN_SECONDS,
    WS_SUBPROTOCOL,
)

_LOGGER = logging.getLogger(__name__)


class GraphQLSubscription:
    """Maintain a single GraphQL subscription, reconnecting as needed.

    gql handles the graphql-transport-ws protocol. This class decides when to
    reconnect, and when a failure means the API key has to be replaced.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        request: GraphQLRequest,
        variables: dict[str, Any] | None,
        on_payload: Callable[[dict[str, Any]], Awaitable[None] | None],
        on_connected: Callable[[], Awaitable[None] | None] | None = None,
        on_disconnected: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        """Initialize the subscription."""
        self._session = session
        self._url = url
        self._request = GraphQLRequest(request, variable_values=variables)
        self._on_payload = on_payload
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._api_key: str | None = None
        self._closing = False
        self._streamed = False

    def set_api_key(self, api_key: str) -> None:
        """Set the key sent with the next connection_init."""
        self._api_key = api_key

    async def async_run(self) -> None:
        """Connect and stay connected until canceled or authentication fails.

        Raises BryxAuthError when the server rejects the API key, so the caller
        can start a reauth flow instead of retrying forever.
        """
        attempt = 0
        while not self._closing:
            self._streamed = False
            try:
                await self._async_connect_once()
            except asyncio.CancelledError:
                raise
            except BryxAuthError:
                raise
            except BryxConnectionError as err:
                _LOGGER.debug("Bryx subscription connection error: %s", err)
            except Exception:
                _LOGGER.exception("Unexpected error in the Bryx subscription")

            if self._closing:
                break

            await self._notify(self._on_disconnected)
            # Back off across attempts that never delivered data. A connection
            # that did was healthy, so whatever ended it, a server-side close or
            # an error, starts the count again and reconnects promptly.
            attempt = 1 if self._streamed else attempt + 1
            delay = min(
                WS_RECONNECT_MIN_SECONDS * 2 ** (attempt - 1),
                WS_RECONNECT_MAX_SECONDS,
            )
            # Jitter keeps many Home Assistant instances from reconnecting in step
            # after a server restart.
            delay += random.uniform(0, delay * 0.25)
            _LOGGER.debug("Reconnecting to the Bryx subscription in %.1fs", delay)
            await asyncio.sleep(delay)

    async def async_close(self) -> None:
        """Stop reconnecting."""
        self._closing = True

    async def _async_connect_once(self) -> None:
        """Open one websocket and stream payloads until it closes."""
        if not self._api_key:
            raise BryxAuthError("No API key available for the subscription")

        transport = AIOHTTPWebsocketsTransport(
            url=self._url,
            session=self._session,
            subprotocols=[WS_SUBPROTOCOL],
            heartbeat=WS_HEARTBEAT_SECONDS,
            ack_timeout=WS_ACK_TIMEOUT_SECONDS,
            init_payload={API_KEY_HEADER: self._api_key},
        )
        try:
            async with Client(transport=transport):
                await self._notify(self._on_connected)
                # The session's subscribe() raises on the first payload that
                # carries errors, which would end the subscription and force a
                # full replay. The transport yields every payload instead, so
                # the data in a partial result is still applied. Like the
                # session, close the generator explicitly: an unclosed one keeps
                # the transport waiting out its close timeout.
                async with aclosing(transport.subscribe(self._request)) as results:
                    async for result in results:
                        if result.errors:
                            _LOGGER.debug(
                                "Bryx subscription payload carried errors: %s",
                                result.errors,
                            )
                        if result.data:
                            self._streamed = True
                            await self._dispatch(result.data)
        except TransportQueryError as err:
            # The server acknowledges connection_init whatever the key, then
            # rejects a bad key with an error message once the subscription
            # starts.
            error = graphql_error(err.errors)
            if isinstance(error, BryxAuthError):
                raise error from err
            raise BryxConnectionError(f"Subscription error: {error}") from err
        except (TransportError, aiohttp.ClientError, OSError) as err:
            # OSError includes the connect and connection_ack timeouts.
            raise BryxConnectionError(f"Subscription connection lost: {err}") from err

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        """Hand a subscription payload to the consumer."""
        result = self._on_payload(payload)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    async def _notify(
        callback: Callable[[], Awaitable[None] | None] | None,
    ) -> None:
        """Invoke an optional lifecycle callback."""
        if callback is None:
            return
        result = callback()
        if asyncio.iscoroutine(result):
            await result
