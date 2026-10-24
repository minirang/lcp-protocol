"""What the server does when a peer breaks the rules of spec section 3.2 / 6 (raw sockets)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lcp import (
    MAX_PAYLOAD_LENGTH,
    Frame,
    LCPServer,
    MessageType,
    Metadata,
    Response,
    SessionConfig,
    Status,
)

from .conftest import RawFactory, ServerFactory
from .helpers import header


async def alive(conn: Any, cid: int = 99) -> bool:
    """Is the connection still usable?  (PING must be answered with a matching PONG.)"""
    await conn.send(Frame.ping(cid))
    reply = await conn.recv()
    return reply.msg_type == MessageType.PONG and reply.correlation_id == cid


async def test_bad_magic_closes_without_any_reply(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(b"GET / HTTP/1.1\r\nHost: example\r\n\r\n")
    assert await conn.eof() == b""  # spec 3.2: forced close, no response


@pytest.mark.parametrize(
    ("bad_header", "needle"),
    [
        (header(version=2, cid=77), "version"),
        (header(msg_type=0, cid=77), "message type"),
        (header(msg_type=9, cid=77), "message type"),
        (header(msg_type=5, payload_len=4, cid=77), "PING/PONG"),
        (header(msg_type=6, meta_len=3, cid=77), "PING/PONG"),
    ],
)
async def test_header_violations_get_bad_request_then_close(
    server: LCPServer, raw_factory: RawFactory, bad_header: bytes, needle: str
) -> None:
    conn = await raw_factory(server)
    await conn.send(bad_header)
    reply = await conn.recv()
    assert reply.msg_type == MessageType.RES
    assert reply.status == Status.BAD_REQUEST and reply.correlation_id == 77
    error = Response(reply).error
    assert error is not None and needle in error["message"]
    assert await conn.eof() == b""


async def test_oversized_payload_is_rejected_from_the_header_alone(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(header(payload_len=MAX_PAYLOAD_LENGTH + 1, cid=5))  # no body is ever sent
    reply = await conn.recv()
    assert reply.status == Status.PAYLOAD_TOO_LARGE and reply.correlation_id == 5
    assert await conn.eof() == b""


async def test_reply_survives_a_peer_that_keeps_streaming_the_oversized_body(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    """Lingering close: without it the TCP reset would destroy the error reply."""
    srv = await server_factory(config=SessionConfig(max_payload=1000, linger_timeout=2.0))
    conn = await raw_factory(srv)
    await conn.send(header(payload_len=500_000, cid=8) + b"x" * 200_000)
    reply = await conn.recv()
    assert reply.status == Status.PAYLOAD_TOO_LARGE and reply.correlation_id == 8
    conn.writer.write(b"x" * 300_000)  # keeps sending; the server just discards it
    await conn.writer.drain()


async def test_unknown_serializer_is_answered_and_the_connection_stays_open(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(header(serializer=0x7F, cid=5))
    reply = await conn.recv()
    assert reply.status == Status.BAD_REQUEST and reply.correlation_id == 5
    assert await alive(conn)


async def test_malformed_metadata_is_answered_and_the_connection_stays_open(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(header(meta_len=2, cid=6) + b"\x02\x00")  # truncated TLV
    reply = await conn.recv()
    assert reply.status == Status.BAD_REQUEST and reply.correlation_id == 6
    assert await alive(conn)


async def test_bad_event_is_acknowledged_with_an_error(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(header(msg_type=3, serializer=0x7F, cid=12))
    reply = await conn.recv()
    assert reply.msg_type == MessageType.EVT_ACK and reply.status == Status.BAD_REQUEST


async def test_req_requires_a_valid_target_path(server: LCPServer, raw_factory: RawFactory) -> None:
    conn = await raw_factory(server)
    for cid, metadata in enumerate(
        [Metadata(), Metadata(target_path="no-slash"), Metadata(target_path="/bad\x00path")], 1
    ):
        await conn.send(Frame(MessageType.REQ, cid, metadata=metadata))
        reply = await conn.recv()
        assert reply.status == Status.BAD_REQUEST and reply.correlation_id == cid
    assert await alive(conn)


async def test_too_many_malformed_frames_close_the_connection(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(config=SessionConfig(max_bad_requests=3, linger_timeout=0.2))
    conn = await raw_factory(srv)
    await conn.send(b"".join(header(serializer=0x7F, cid=i) for i in range(1, 9)))
    data = await conn.eof()
    replies = len(data) // len(Frame(MessageType.RES, 1, Status.BAD_REQUEST).encode())
    assert replies >= 1  # some were answered before the budget ran out
    assert await conn.eof() == b""  # ...and then the connection is gone


async def test_unsolicited_responses_and_pongs_are_ignored(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(Frame(MessageType.RES, 1234, payload=b"{}"))
    await conn.send(Frame(MessageType.EVT_ACK, 1235))
    await conn.send(Frame.pong(1236))
    assert await alive(conn)


async def test_frames_may_arrive_one_byte_at_a_time(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    wire = Frame(
        MessageType.REQ, 3, metadata=Metadata(target_path="/echo"), payload=b'"hi"'
    ).encode()
    for byte in wire:
        conn.writer.write(bytes([byte]))
        await conn.writer.drain()
        await asyncio.sleep(0)
    reply = await conn.recv()
    assert reply.status == Status.OK and reply.correlation_id == 3 and reply.payload == b'"hi"'


async def test_garbage_after_a_valid_frame_closes_the_connection(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    await conn.send(Frame.ping(1).encode() + b"\xde\xad\xbe\xef" * 4)
    assert (await conn.recv()).msg_type == MessageType.PONG
    assert await conn.eof() == b""


async def test_ping_is_answered_with_the_same_correlation_id(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    for cid in (0, 1, 0xFFFFFFFF):
        assert await alive(conn, cid)
