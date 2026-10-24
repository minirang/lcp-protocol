"""Asyncio LCP server."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import ssl as ssl_lib
import warnings
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from .auth import Authenticator
from .constants import DEFAULT_PORT
from .enums import MessageType, Serializer, Status
from .errors import LCPError
from .frame import Frame
from .message import Response, error_body
from .metadata import EMPTY_METADATA
from .router import Handler, Router
from .session import READ_LIMIT, Session, SessionConfig

logger = logging.getLogger("lcp.server")

_H = TypeVar("_H", bound=Callable[..., Any])


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LCPServer:
    """Accepts LCP connections and dispatches ``REQ`` / ``EVT`` frames to handlers.

    >>> server = LCPServer("127.0.0.1", 0)
    >>> @server.route("/v1/echo")
    ... async def echo(request):
    ...     return request.body()

    Handlers receive a :class:`~lcp.message.Request` and return a plain body, an
    :class:`~lcp.codec.Encoded` payload or a :class:`~lcp.message.Reply`; raising
    :class:`~lcp.errors.StatusError` answers with that status.

    Without ``ssl`` the server speaks plain TCP, which the specification only allows for
    development; binding a non-loopback address that way emits a warning.
    """

    def __init__(
        self,
        host: str | Sequence[str] | None = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        ssl: ssl_lib.SSLContext | None = None,
        authenticator: Authenticator | None = None,
        config: SessionConfig | None = None,
        max_connections: int | None = 10_000,
        ssl_handshake_timeout: float = 10.0,
        backlog: int = 1024,
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._authenticator = authenticator
        self.config = config if config is not None else SessionConfig(idle_timeout=120.0)
        self._max_connections = max_connections
        self._ssl_handshake_timeout = ssl_handshake_timeout
        self._backlog = backlog
        self.router = Router()
        self.events = Router()
        self._server: asyncio.Server | None = None
        self._sessions: set[Session] = set()

        if ssl is None:
            hosts = [host] if isinstance(host, str) else list(host) if host else [None]
            if any(h is None or h == "" or not _is_loopback(h) for h in hosts):
                warnings.warn(
                    "LCP server without TLS on a non-loopback address: the specification "
                    "requires TLS 1.3 or later in production (spec 2.1); pass ssl=...",
                    stacklevel=2,
                )

    # -- routing ----------------------------------------------------------------------------

    def add_route(self, pattern: str, handler: Handler) -> None:
        self.router.add(pattern, handler)

    def route(self, pattern: str) -> Callable[[_H], _H]:
        """Register a ``REQ`` handler: exact path, ``/prefix/*`` or ``*``."""
        return self.router.route(pattern)

    def on_event(self, pattern: str = "*") -> Callable[[_H], _H]:
        """Register an ``EVT`` handler (matched on ``TARGET_PATH``; ``*`` is the default)."""
        return self.events.route(pattern)

    # -- lifecycle --------------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The bound port (useful with ``port=0``)."""
        if self._server is not None and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    @property
    def sessions(self) -> frozenset[Session]:
        """Currently connected sessions."""
        return frozenset(self._sessions)

    async def start(self) -> LCPServer:
        if self._server is not None:
            return self
        extra: dict[str, Any] = {}
        if self._ssl is not None:
            extra["ssl"] = self._ssl
            extra["ssl_handshake_timeout"] = self._ssl_handshake_timeout
        self._server = await asyncio.start_server(
            self._on_connect,
            self._host,
            self._port,
            backlog=self._backlog,
            limit=READ_LIMIT,
            **extra,
        )
        logger.info("LCP server listening on port %d (tls=%s)", self.port, self._ssl is not None)
        return self

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self, *, grace: float = 5.0) -> None:
        """Stop accepting, let running handlers finish for up to ``grace`` seconds, close."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        sessions = list(self._sessions)
        if sessions:
            await asyncio.gather(*(s.close(grace=grace) for s in sessions), return_exceptions=True)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), grace + 5.0)
            except asyncio.TimeoutError:
                logger.warning("server did not finish closing in time")

    async def __aenter__(self) -> LCPServer:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- pushing events ---------------------------------------------------------------------

    async def broadcast_event(
        self,
        path: str,
        body: Any = None,
        *,
        timeout: float | None = None,
    ) -> dict[Session, Response | BaseException]:
        """Send an ``EVT`` to every connected client; returns each ack (or the error)."""
        sessions = list(self._sessions)
        results = await asyncio.gather(
            *(s.send_event(path, body, timeout=timeout) for s in sessions),
            return_exceptions=True,
        )
        return dict(zip(sessions, results, strict=True))

    # -- connections ------------------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._max_connections is not None and len(self._sessions) >= self._max_connections:
            await self._refuse(writer)
            return
        session = Session(
            reader,
            writer,
            config=self.config,
            router=self.router,
            events=self.events,
            authenticator=self._authenticator,
        )
        self._sessions.add(session)
        try:
            await session.serve()
        except LCPError:
            logger.exception("%s: session failed", session)
        finally:
            self._sessions.discard(session)

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter) -> None:
        """Too many connections: tell the peer (BUSY, correlation id 0) and hang up."""
        logger.warning("connection refused: max_connections reached")
        frame = Frame(
            MessageType.RES,
            0,
            Status.BUSY,
            Serializer.JSON,
            EMPTY_METADATA,
            error_body(Status.BUSY, "too many connections", retry_after=1.0),
        )
        try:
            writer.write(frame.encode())
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        except (OSError, asyncio.TimeoutError):
            pass
