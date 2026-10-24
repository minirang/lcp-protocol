"""Closed-loop benchmark clients: one connection, one outstanding request at a time."""

from __future__ import annotations

import asyncio
from typing import Protocol

from lcp import (
    Encoded,
    Frame,
    FrameDecoder,
    LCPClient,
    MessageType,
    Metadata,
    Serializer,
    SessionConfig,
)


class Conn(Protocol):
    async def request(self, payload: bytes) -> bytes: ...

    async def close(self) -> None: ...


class LcpConn:
    """Full stack: ``LCPClient`` (correlation, timeouts, back-pressure handling)."""

    def __init__(self, client: LCPClient) -> None:
        self._client = client

    @classmethod
    async def open(cls, port: int) -> LcpConn:
        config = SessionConfig(keepalive_interval=None, request_timeout=60)
        return cls(await LCPClient("127.0.0.1", port, config=config).connect())

    async def request(self, payload: bytes) -> bytes:
        response = await self._client.request("/echo", Encoded(payload, Serializer.RAW))
        return response.payload

    async def close(self) -> None:
        await self._client.close()


class LcpRawConn:
    """Framing only: hand-built frames, ``FrameDecoder`` for replies."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer
        self._decoder = FrameDecoder()
        self._cid = 0
        self._meta = Metadata(target_path="/echo")

    @classmethod
    async def open(cls, port: int) -> LcpRawConn:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=262144)
        return cls(reader, writer)

    async def request(self, payload: bytes) -> bytes:
        self._cid = self._cid % 0xFFFFFFFF + 1
        frame = Frame(
            MessageType.REQ,
            self._cid,
            serializer=Serializer.RAW,
            metadata=self._meta,
            payload=payload,
        )
        self._writer.writelines(frame.encode_parts())
        while (reply := self._decoder.next_frame()) is None:
            data = await self._reader.read(262144)
            if not data:
                raise ConnectionError("closed")
            self._decoder.feed(data)
        return reply.payload

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


class Http1Conn:
    """Minimal HTTP/1.1 keep-alive client (``Content-Length`` framing, JSON body)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer

    @classmethod
    async def open(cls, port: int) -> Http1Conn:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=262144)
        return cls(reader, writer)

    async def request(self, payload: bytes) -> bytes:
        self._writer.write(
            b"POST /echo HTTP/1.1\r\nHost: bench\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % len(payload) + payload
        )
        head = await self._reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
        return await self._reader.readexactly(length) if length else b""

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


TARGETS = {"lcp": LcpConn, "lcp-raw": LcpRawConn, "http1": Http1Conn}
