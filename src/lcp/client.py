"""Asyncio LCP client."""

from __future__ import annotations

import asyncio
import ssl as ssl_lib
from collections.abc import Callable
from typing import Any, TypeVar

from .constants import DEFAULT_PORT
from .errors import ConnectionClosedError, RequestTimeoutError
from .message import Response
from .metadata import TraceIdLike
from .router import Handler, Router
from .session import READ_LIMIT, Session, SessionConfig

_H = TypeVar("_H", bound=Callable[..., Any])


class LCPClient:
    """Connects to an :class:`~lcp.server.LCPServer`.

    >>> async with LCPClient("127.0.0.1", 19523, auth_token="...") as client:
    ...     reply = await client.call("/v1/user/profile", {"id": 7})

    One connection multiplexes any number of concurrent requests (they are matched to
    replies by *Correlation ID*).  Handlers for events pushed by the server are registered
    with :meth:`on_event`.  ``BUSY`` replies are retried automatically with the server's
    ``retry_after`` hint; see ``SessionConfig.busy_retries``.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        ssl: ssl_lib.SSLContext | None = None,
        server_hostname: str | None = None,
        auth_token: str | None = None,
        client_id: str | None = None,
        connect_timeout: float | None = 10.0,
        config: SessionConfig | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = config if config is not None else SessionConfig(keepalive_interval=30.0)
        self.router = Router()
        self.events = Router()
        self._ssl = ssl
        self._server_hostname = server_hostname
        self._auth_token = auth_token
        self._client_id = client_id
        self._connect_timeout = connect_timeout
        self._session: Session | None = None
        self._serve_task: asyncio.Task[None] | None = None

    # -- connection -------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._session is not None and not self._session.closing

    @property
    def session(self) -> Session:
        """The underlying :class:`~lcp.session.Session` (raises when not connected)."""
        if self._session is None:
            raise ConnectionClosedError("client is not connected")
        return self._session

    async def connect(self) -> LCPClient:
        if self.connected:
            return self
        kwargs: dict[str, Any] = {}
        if self._ssl is not None:
            kwargs["ssl"] = self._ssl
            kwargs["server_hostname"] = self._server_hostname
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=READ_LIMIT, **kwargs),
                self._connect_timeout,
            )
        except asyncio.TimeoutError:
            raise RequestTimeoutError(
                f"connecting to {self.host}:{self.port} timed out after {self._connect_timeout}s"
            ) from None
        session = Session(
            reader,
            writer,
            config=self.config,
            router=self.router,
            events=self.events,
            auth_token=self._auth_token,
            client_id=self._client_id,
        )
        self._session = session
        self._serve_task = asyncio.get_running_loop().create_task(session.serve())
        return self

    async def close(self) -> None:
        session, task = self._session, self._serve_task
        self._session = self._serve_task = None
        if session is not None:
            await session.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def __aenter__(self) -> LCPClient:
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- calls ------------------------------------------------------------------------------

    async def request(
        self,
        path: str,
        body: Any = None,
        *,
        serializer: int | None = None,
        timeout: float | None = None,
        trace_id: TraceIdLike | None = None,
        busy_retries: int | None = None,
    ) -> Response:
        """Send a ``REQ`` and return the ``RES`` (check ``.ok`` / ``raise_for_status()``)."""
        return await self.session.request(
            path,
            body,
            serializer=serializer,
            timeout=timeout,
            trace_id=trace_id,
            busy_retries=busy_retries,
        )

    async def call(
        self,
        path: str,
        body: Any = None,
        *,
        response_type: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Like :meth:`request`, but returns the decoded body and raises on non-OK status."""
        response = await self.request(path, body, **kwargs)
        return response.raise_for_status().body(response_type)

    async def send_event(
        self,
        path: str = "",
        body: Any = None,
        *,
        serializer: int | None = None,
        timeout: float | None = None,
        retries: int = 0,
        trace_id: TraceIdLike | None = None,
        busy_retries: int | None = None,
    ) -> Response:
        """Publish an ``EVT`` and wait for the ``EVT_ACK`` (see ``Session.send_event``)."""
        return await self.session.send_event(
            path,
            body,
            serializer=serializer,
            timeout=timeout,
            retries=retries,
            trace_id=trace_id,
            busy_retries=busy_retries,
        )

    async def ping(self, timeout: float | None = None) -> float:
        """Round-trip time of a ``PING``/``PONG`` in seconds."""
        return await self.session.ping(timeout)

    # -- handlers for server-initiated traffic ------------------------------------------------

    def on_event(self, pattern: str = "*") -> Callable[[_H], _H]:
        """Register a handler for ``EVT`` frames pushed by the server."""
        return self.events.route(pattern)

    def add_route(self, pattern: str, handler: Handler) -> None:
        """Serve ``REQ`` frames sent by the server (the protocol is symmetric)."""
        self.router.add(pattern, handler)
