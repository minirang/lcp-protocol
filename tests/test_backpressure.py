"""Application back-pressure: BUSY / RATE_LIMITED (0x09), spec section 5.2."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lcp import (
    Frame,
    LCPClient,
    MessageType,
    Metadata,
    RateLimit,
    Response,
    ServerBusyError,
    SessionConfig,
    Status,
)

from .conftest import ClientFactory, RawFactory, ServerFactory
from .helpers import wait_until

NO_RETRY = SessionConfig(busy_retries=0, keepalive_interval=None)


async def test_admission_cap_answers_busy_with_a_retry_hint(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        config=SessionConfig(max_in_flight=2, max_concurrency=2, linger_timeout=0.2)
    )
    gate = asyncio.Event()

    @srv.route("/work")
    async def work(request: Any) -> str:
        await gate.wait()
        return "done"

    client = await client_factory(srv, config=NO_RETRY)
    tasks = [asyncio.create_task(client.request("/work")) for _ in range(5)]
    await wait_until(lambda: sum(t.done() for t in tasks) == 3)
    rejected = [t.result() for t in tasks if t.done()]
    assert {r.status for r in rejected} == {Status.BUSY}
    assert all(r.retry_after is not None and r.retry_after > 0 for r in rejected)
    gate.set()
    accepted = [r for r in await asyncio.gather(*tasks) if r.status != Status.BUSY]
    assert len(accepted) == 2 and all(r.json() == "done" for r in accepted)
    with pytest.raises(ServerBusyError) as info:
        rejected[0].raise_for_status()
    assert info.value.retry_after == rejected[0].retry_after


async def test_concurrency_limit_queues_instead_of_rejecting(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(config=SessionConfig(max_concurrency=2, max_in_flight=100))
    running = peak = 0

    @srv.route("/work")
    async def work(request: Any) -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return request.body()

    client = await client_factory(srv, config=NO_RETRY)
    results = await asyncio.gather(*(client.request("/work", i) for i in range(10)))
    assert [r.json() for r in results] == list(range(10)) and peak == 2


async def test_byte_budget_limits_buffered_payloads(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(config=SessionConfig(max_in_flight_bytes=1000, linger_timeout=0.2))
    gate = asyncio.Event()

    @srv.route("/hold")
    async def hold(request: Any) -> int:
        await gate.wait()
        return len(request.payload)

    client = await client_factory(srv, config=NO_RETRY)
    first = asyncio.create_task(client.request("/hold", b"a" * 600))
    await asyncio.sleep(0.1)
    second = await client.request("/hold", b"b" * 600)  # 600 + 600 > 1000 → rejected
    assert second.status == Status.BUSY
    gate.set()
    assert (await first).json() == 600
    big = await client.request("/hold", b"c" * 5000)  # alone it is always admitted
    assert big.json() == 5000


async def test_busy_is_retried_automatically(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        config=SessionConfig(max_in_flight=2, max_concurrency=2, linger_timeout=0.2)
    )
    running = peak = 0

    @srv.route("/work")
    async def work(request: Any) -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return request.body()

    client = await client_factory(
        srv, config=SessionConfig(busy_retries=100, keepalive_interval=None)
    )
    assert await asyncio.gather(*(client.call("/work", i) for i in range(12))) == list(range(12))
    assert peak <= 2
    assert sum(s.stats.busy_rejections for s in srv.sessions) > 0  # it really was throttled


async def test_token_bucket_rate_limit(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        config=SessionConfig(rate_limit=RateLimit(rate=5, burst=2), linger_timeout=0.2)
    )
    srv.add_route("/echo", lambda request: request.body())
    client = await client_factory(srv, config=NO_RETRY)
    statuses = [(await client.request("/echo", i)).status for i in range(4)]
    assert statuses == [Status.OK, Status.OK, Status.BUSY, Status.BUSY]
    throttled = await client.request("/echo", 9)
    assert throttled.status == Status.BUSY
    assert throttled.retry_after is not None and 0 < throttled.retry_after <= 0.21
    await asyncio.sleep(throttled.retry_after + 0.05)
    assert (await client.request("/echo", 10)).status == Status.OK


async def test_client_honours_retry_after_by_pausing(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        config=SessionConfig(rate_limit=RateLimit(rate=5, burst=2), linger_timeout=0.2)
    )
    srv.add_route("/echo", lambda request: request.body())
    client = await client_factory(srv)  # default: busy_retries=3
    await client.call("/echo", 1)
    await client.call("/echo", 2)  # bucket is empty now
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await client.call("/echo", 3) == 3  # BUSY → wait for the hint → retry → OK
    assert 0.1 < loop.time() - started < 2.0


async def test_busy_pauses_all_outbound_calls_of_the_session(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory()
    srv.add_route("/echo", lambda request: request.body())
    client = await client_factory(srv)
    loop = asyncio.get_running_loop()
    client.session._pause_until = loop.time() + 0.3  # what a BUSY reply does
    started = loop.time()
    assert await client.call("/echo", 1) == 1
    assert loop.time() - started >= 0.25


async def test_busy_after_retries_are_exhausted_is_reported(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        config=SessionConfig(max_in_flight=1, max_concurrency=1, linger_timeout=0.2)
    )
    gate = asyncio.Event()

    @srv.route("/hold")
    async def hold(request: Any) -> str:
        await gate.wait()
        return "ok"

    client = await client_factory(
        srv, config=SessionConfig(busy_retries=2, keepalive_interval=None)
    )
    first = asyncio.create_task(client.call("/hold"))
    await asyncio.sleep(0.1)  # the only slot is taken
    with pytest.raises(ServerBusyError) as info:
        await client.call("/hold")  # BUSY, retried twice, still BUSY -> reported to the caller
    assert info.value.status == Status.BUSY
    assert info.value.retry_after == pytest.approx(0.05)
    gate.set()
    assert await first == "ok"


async def test_busy_event_ack_and_ping_are_not_throttled(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(
        config=SessionConfig(rate_limit=RateLimit(rate=1, burst=1), linger_timeout=0.2)
    )
    srv.on_event("*")(lambda request: None)
    conn = await raw_factory(srv)
    evt = lambda cid: Frame(MessageType.EVT, cid, metadata=Metadata(target_path="/e"))  # noqa: E731
    await conn.send(evt(1))
    assert (await conn.recv()).status == Status.OK
    await conn.send(evt(2))
    busy = await conn.recv()
    assert busy.msg_type == MessageType.EVT_ACK and busy.status == Status.BUSY
    assert Response(busy).retry_after is not None
    await conn.send(Frame.ping(3))  # keep-alive traffic is never rate limited
    assert (await conn.recv()).msg_type == MessageType.PONG


async def test_a_peer_that_never_reads_is_cut_off(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(config=SessionConfig(write_timeout=0.3, linger_timeout=0.2))
    srv.add_route("/big", lambda request: b"x" * (8 * 1024 * 1024))
    conn = await raw_factory(srv)
    wire = Frame(MessageType.REQ, 1, metadata=Metadata(target_path="/big")).encode()
    conn.writer.write(wire * 40)  # ...and never read a single reply byte
    await conn.writer.drain()
    await wait_until(lambda: len(srv.sessions) == 1)
    await wait_until(lambda: not srv.sessions, timeout=10)


async def test_server_refuses_new_calls_while_shutting_down(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory()
    release = asyncio.Event()

    @srv.route("/slow")
    async def slow(request: Any) -> str:
        await release.wait()
        return "done"

    client: LCPClient = await client_factory(srv, config=NO_RETRY)
    running = asyncio.create_task(client.request("/slow"))
    await asyncio.sleep(0.1)
    closing = asyncio.create_task(srv.close(grace=5))
    await asyncio.sleep(0.1)
    refused = await client.request("/slow")
    assert refused.status == Status.BUSY
    assert isinstance(refused, Response) and "shutting down" in refused.payload.decode()
    release.set()
    assert (await running).json() == "done"
    await closing
