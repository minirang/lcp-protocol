"""EVT / EVT_ACK: acknowledged events in both directions (at-least-once with de-duplication)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from lcp import (
    EncodeError,
    Frame,
    LCPClient,
    LCPServer,
    MessageType,
    Metadata,
    RequestTimeoutError,
    Response,
    SessionConfig,
    Status,
    StatusError,
)

from .conftest import ClientFactory, RawFactory, ServerFactory


async def test_client_event_is_acked_after_the_handler_ran(
    server: LCPServer, client: LCPClient
) -> None:
    received: list[tuple[str, Any, bool]] = []

    @server.on_event("/v1/events/click")
    async def on_click(request: Any) -> None:
        received.append((request.path, request.body(), request.is_event))

    ack = await client.send_event("/v1/events/click", {"x": 1})
    assert ack.ok and ack.frame.msg_type == MessageType.EVT_ACK
    assert received == [("/v1/events/click", {"x": 1}, True)]


async def test_server_can_push_events_to_clients(
    server: LCPServer, client_factory: ClientFactory
) -> None:
    clients = [await client_factory(server) for _ in range(2)]
    got: list[tuple[int, Any]] = []
    for index, client in enumerate(clients):

        @client.on_event("/news")
        async def news(request: Any, index: int = index) -> None:
            got.append((index, request.body()))

    session = next(iter(server.sessions))
    assert (await session.send_event("/news", {"headline": "hello"})).ok
    assert len(got) == 1

    got.clear()
    results = await server.broadcast_event("/news", {"n": 2})
    assert len(results) == 2 and all(isinstance(v, Response) and v.ok for v in results.values())
    assert sorted(got) == [(0, {"n": 2}), (1, {"n": 2})]


async def test_handler_can_push_an_event_back_over_the_same_connection(
    server: LCPServer, client: LCPClient
) -> None:
    pushed: list[Any] = []
    client.on_event("/progress")(lambda request: pushed.append(request.body()))

    @server.route("/job")
    async def job(request: Any) -> str:
        for step in range(3):
            ack = await request.session.send_event("/progress", {"step": step})
            assert ack.ok
        return "finished"

    assert await client.call("/job") == "finished"
    assert pushed == [{"step": 0}, {"step": 1}, {"step": 2}]


async def test_missing_handler_is_answered_with_not_found(client: LCPClient) -> None:
    ack = await client.send_event("/nobody")
    assert ack.status == Status.NOT_FOUND and not ack.ok
    assert ack.error is not None and "event handler" in ack.error["message"]


async def test_catch_all_handler_also_serves_events_without_a_path(
    server: LCPServer, client: LCPClient
) -> None:
    seen: list[str] = []
    server.on_event()(lambda request: seen.append(request.path))
    assert (await client.send_event()).ok and (await client.send_event("/x")).ok
    assert seen == ["", "/x"]


async def test_handler_errors_are_reported_in_the_ack(server: LCPServer, client: LCPClient) -> None:
    def refuse(request: Any) -> None:
        raise StatusError(Status.BAD_REQUEST, "bad event")

    def crash(request: Any) -> None:
        raise RuntimeError("kaboom")

    server.on_event("/refuse")(refuse)
    server.on_event("/crash")(crash)
    assert (await client.send_event("/refuse")).status == Status.BAD_REQUEST
    assert (await client.send_event("/crash")).status == Status.INTERNAL_ERROR


async def test_busy_acks_are_retried_by_the_sender(server: LCPServer, client: LCPClient) -> None:
    attempts = 0

    @server.on_event("/flaky")
    async def flaky(request: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StatusError(Status.BUSY, "try later", retry_after=0.02)

    assert (await client.send_event("/flaky")).ok
    assert attempts == 3  # a BUSY event was not processed, so re-sending it is safe


async def test_retransmitted_events_run_the_handler_once(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    count = 0

    @server.on_event("/once")
    async def once(request: Any) -> None:
        nonlocal count
        count += 1

    conn = await raw_factory(server)
    evt = Frame(MessageType.EVT, 5, metadata=Metadata(target_path="/once"), payload=b"{}")
    await conn.send(evt)
    first = await conn.recv()
    await conn.send(evt)  # e.g. the ack was lost and the sender retried
    second = await conn.recv()
    assert first.msg_type == second.msg_type == MessageType.EVT_ACK
    assert first.status == second.status == Status.OK and count == 1


async def test_duplicate_of_a_failed_event_gets_the_same_error(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    evt = Frame(MessageType.EVT, 9, metadata=Metadata(target_path="/unrouted"))
    await conn.send(evt)
    first = await conn.recv()
    await conn.send(evt)  # a retransmission after the first ack was lost
    second = await conn.recv()
    assert first.status == second.status == Status.NOT_FOUND
    assert Response(first).error == Response(second).error


async def test_duplicate_while_the_original_is_still_running_is_ignored(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    count = 0

    @server.on_event("/slow")
    async def slow(request: Any) -> None:
        nonlocal count
        count += 1
        await asyncio.sleep(0.2)

    conn = await raw_factory(server)
    evt = Frame(MessageType.EVT, 1, metadata=Metadata(target_path="/slow"))
    await conn.send(evt)
    await conn.send(evt)
    assert (await conn.recv()).status == Status.OK  # exactly one ack...
    with pytest.raises(asyncio.TimeoutError):
        await conn.recv(timeout=0.3)
    assert count == 1


async def test_dedupe_window_is_bounded(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(config=SessionConfig(event_dedupe_window=2, linger_timeout=0.2))
    count = 0

    @srv.on_event("/e")
    async def handler(request: Any) -> None:
        nonlocal count
        count += 1

    conn = await raw_factory(srv)

    async def send(cid: int) -> None:
        await conn.send(Frame(MessageType.EVT, cid, metadata=Metadata(target_path="/e")))
        await conn.recv()

    for cid in (1, 2, 3):
        await send(cid)
    assert count == 3
    await send(3)  # still remembered
    assert count == 3
    await send(1)  # evicted from the 2-entry window -> handled again
    assert count == 4


async def test_dedupe_can_be_disabled(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(config=SessionConfig(event_dedupe_window=0, linger_timeout=0.2))
    count = 0

    @srv.on_event("/e")
    async def handler(request: Any) -> None:
        nonlocal count
        count += 1

    conn = await raw_factory(srv)
    for _ in range(2):
        await conn.send(Frame(MessageType.EVT, 1, metadata=Metadata(target_path="/e")))
        await conn.recv()
    assert count == 2


async def test_send_event_retries_with_the_same_id_after_an_ack_timeout(
    server: LCPServer, client: LCPClient
) -> None:
    count = 0

    @server.on_event("/slow")
    async def slow(request: Any) -> None:
        nonlocal count
        count += 1
        await asyncio.sleep(0.25)

    with pytest.raises(RequestTimeoutError):
        await client.send_event("/slow", timeout=0.05)  # no retries: the caller learns about it
    await asyncio.sleep(0.3)
    count = 0
    ack = await client.send_event("/slow", timeout=0.1, retries=5)
    assert ack.ok and count == 1  # the retransmissions did not run the handler again


async def test_event_payloads_trace_ids_and_validation(
    server: LCPServer, client: LCPClient
) -> None:
    seen: dict[str, Any] = {}

    @server.on_event("/bin")
    async def binary(request: Any) -> None:
        seen["body"], seen["trace"] = request.body(), request.metadata.trace_id

    trace = uuid.uuid4()
    ack = await client.send_event("/bin", b"\x00\x01\x02", trace_id=trace)
    assert ack.ok and seen == {"body": b"\x00\x01\x02", "trace": trace.bytes}
    assert ack.metadata.trace_id == trace.bytes
    with pytest.raises(EncodeError):
        await client.send_event("not-absolute")
    with pytest.raises(EncodeError):
        await client.request("not-absolute")
