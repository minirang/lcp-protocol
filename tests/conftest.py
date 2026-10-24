from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest_asyncio

from lcp import Frame, FrameDecoder, LCPClient, LCPServer, SessionConfig


class RawConn:
    """A bare TCP connection that speaks (or violates) the wire protocol by hand."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.decoder = FrameDecoder()

    async def send(self, data: bytes | Frame) -> None:
        self.writer.write(data.encode() if isinstance(data, Frame) else data)
        await self.writer.drain()

    async def recv(self, timeout: float = 3.0) -> Frame:
        while True:
            frame = self.decoder.next_frame()
            if frame is not None:
                return frame
            chunk = await asyncio.wait_for(self.reader.read(65536), timeout)
            if not chunk:
                raise EOFError("connection closed by peer")
            self.decoder.feed(chunk)

    async def eof(self, timeout: float = 3.0) -> bytes:
        """Everything the peer sends until it closes the connection."""
        try:
            return await asyncio.wait_for(self.reader.read(), timeout)
        except ConnectionError:
            return b""

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.wait_closed()


ServerFactory = Callable[..., Awaitable[LCPServer]]
ClientFactory = Callable[..., Awaitable[LCPClient]]
RawFactory = Callable[[LCPServer], Awaitable[RawConn]]


@pytest_asyncio.fixture
async def server_factory() -> AsyncIterator[ServerFactory]:
    servers: list[LCPServer] = []

    async def make(**kwargs: Any) -> LCPServer:
        kwargs.setdefault("host", "127.0.0.1")
        kwargs.setdefault("port", 0)
        config = kwargs.pop("config", None) or SessionConfig(idle_timeout=30.0, linger_timeout=0.2)
        server = LCPServer(config=config, **kwargs)
        await server.start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        await server.close(grace=0.2)


@pytest_asyncio.fixture
async def client_factory() -> AsyncIterator[ClientFactory]:
    clients: list[LCPClient] = []

    async def make(server: LCPServer, **kwargs: Any) -> LCPClient:
        kwargs.setdefault("config", SessionConfig(keepalive_interval=None))
        client = LCPClient("127.0.0.1", server.port, **kwargs)
        await client.connect()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


@pytest_asyncio.fixture
async def raw_factory() -> AsyncIterator[RawFactory]:
    conns: list[RawConn] = []

    async def make(server: LCPServer) -> RawConn:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        conn = RawConn(reader, writer)
        conns.append(conn)
        return conn

    yield make
    for conn in conns:
        await conn.close()


@pytest_asyncio.fixture
async def server(server_factory: ServerFactory) -> LCPServer:
    """A plain server with an ``/echo`` route."""
    srv = await server_factory()

    @srv.route("/echo")
    async def echo(request: Any) -> Any:
        return request.body()

    return srv


@pytest_asyncio.fixture
async def client(server: LCPServer, client_factory: ClientFactory) -> LCPClient:
    return await client_factory(server)
