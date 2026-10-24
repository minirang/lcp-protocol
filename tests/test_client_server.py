from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import pytest

from lcp import (
    MAX_PAYLOAD_LENGTH,
    ConnectionClosedError,
    Encoded,
    Frame,
    LCPClient,
    LCPServer,
    MessageType,
    Metadata,
    Reply,
    RequestTimeoutError,
    Serializer,
    SessionConfig,
    Status,
    StatusError,
)

from .conftest import ClientFactory, RawFactory, ServerFactory


async def test_echo_roundtrip(client: LCPClient) -> None:
    body = {"hello": "LCP", "n": [1, 2, 3], "한글": True}
    assert await client.call("/echo", body) == body


async def test_unknown_route_is_not_found(client: LCPClient) -> None:
    response = await client.request("/missing")
    assert response.status == Status.NOT_FOUND and not response.ok
    assert response.error is not None and response.error["name"] == "NOT_FOUND"
    with pytest.raises(StatusError) as info:
        response.raise_for_status()
    assert info.value.status == Status.NOT_FOUND


async def test_handler_exception_never_leaks_details(
    server: LCPServer, client: LCPClient, caplog: pytest.LogCaptureFixture
) -> None:
    @server.route("/boom")
    async def boom(request: Any) -> None:
        raise RuntimeError("database password is hunter2")

    response = await client.request("/boom")
    assert response.status == Status.INTERNAL_ERROR
    assert b"hunter2" not in response.payload
    assert "hunter2" in caplog.text  # ...but it is logged on the server
    assert await client.call("/echo", 1) == 1  # and the connection survives


async def test_status_error_and_reply_control_the_status(
    server: LCPServer, client: LCPClient
) -> None:
    @server.route("/user")
    async def user(request: Any) -> None:
        raise StatusError(Status.NOT_FOUND, "no such user", details={"id": request.body()["id"]})

    @server.route("/reply")
    async def reply(request: Any) -> Reply:
        return Reply({"why": "nope"}, status=Status.BAD_REQUEST)

    response = await client.request("/user", {"id": 7})
    assert response.status == Status.NOT_FOUND
    assert response.error == {
        "code": 4,
        "name": "NOT_FOUND",
        "message": "no such user",
        "details": {"id": 7},
    }
    with pytest.raises(StatusError) as info:
        response.raise_for_status()
    assert info.value.details == {"id": 7}
    custom = await client.request("/reply")
    assert custom.status == Status.BAD_REQUEST and custom.json() == {"why": "nope"}


async def test_handler_return_types(server: LCPServer, client: LCPClient) -> None:
    server.add_route("/none", lambda request: None)
    server.add_route("/bytes", lambda request: b"\x00\x01")
    server.add_route("/str", lambda request: "text")
    server.add_route("/encoded", lambda request: Encoded(b'{"raw":true}', Serializer.JSON))

    @server.route("/async-dict")
    async def async_dict(request: Any) -> dict[str, bool]:
        return {"async": True}

    none = await client.request("/none")
    assert none.ok and none.payload == b"" and none.body() is None
    raw = await client.request("/bytes")
    assert raw.serializer == Serializer.RAW and raw.body() == b"\x00\x01"
    assert (await client.request("/str")).json() == "text"
    assert (await client.request("/encoded")).json() == {"raw": True}
    assert (await client.request("/async-dict")).json() == {"async": True}  # sync + async handlers


async def test_binary_bodies_use_the_raw_serializer(client: LCPClient) -> None:
    response = await client.request("/echo", b"\x00\xff\x10")
    assert response.serializer == Serializer.RAW and response.payload == b"\x00\xff\x10"


async def test_prefix_catch_all_and_unicode_routes(server: LCPServer, client: LCPClient) -> None:
    server.add_route("/files/*", lambda r: "files")
    server.add_route("/files/img/*", lambda r: "img")
    server.add_route("*", lambda r: "default")
    server.add_route("/v1/사용자/프로필", lambda r: "profile")
    assert await client.call("/files/a/b") == "files"
    assert await client.call("/files/img/x.png") == "img"  # the longest prefix wins
    assert await client.call("/files") == "default"
    assert await client.call("/anything") == "default"
    assert await client.call("/v1/사용자/프로필") == "profile"


async def test_concurrent_requests_complete_out_of_order(
    server: LCPServer, client: LCPClient
) -> None:
    finished: list[int] = []

    @server.route("/sleep")
    async def sleep(request: Any) -> int:
        n = request.body()
        await asyncio.sleep(0.25 - n * 0.03)  # request 0 is the slowest
        finished.append(n)
        return n

    results = await asyncio.gather(*(client.call("/sleep", n) for n in range(8)))
    assert results == list(range(8))  # each reply reached the right caller
    assert finished == sorted(finished, reverse=True)  # although the server answered in reverse


async def test_many_concurrent_requests(client: LCPClient) -> None:
    results = await asyncio.gather(*(client.call("/echo", i) for i in range(1000)))
    assert results == list(range(1000))


async def test_trace_and_client_ids(
    server: LCPServer, server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    seen: dict[str, Any] = {}

    @server.route("/who")
    async def who(request: Any) -> None:
        seen["trace"] = request.metadata.trace_id
        seen["client"] = request.metadata.client_id
        seen["session_client"] = request.session.client_id

    client = await client_factory(server, client_id="svc-a")
    trace = uuid.uuid4()
    response = await client.request("/who", trace_id=trace)
    assert seen["trace"] == trace.bytes and seen["client"] == "svc-a"
    assert response.metadata.trace_id == trace.bytes  # echoed for distributed tracing
    assert (await client.request("/who")).metadata.trace_id is None
    server_side = next(iter(server.sessions))
    assert server_side.client_id is None  # server-side sessions do not learn ids implicitly

    auto = await client_factory(
        server, config=SessionConfig(auto_trace_id=True, keepalive_interval=None)
    )
    reply = await auto.request("/who")
    assert reply.metadata.trace_id is not None and len(reply.metadata.trace_id) == 16
    assert seen["trace"] == reply.metadata.trace_id


async def test_ping_measures_round_trip(client: LCPClient) -> None:
    assert 0 < await client.ping() < 1.0


async def test_request_timeout_leaves_the_connection_usable(
    server: LCPServer, client: LCPClient
) -> None:
    @server.route("/slow")
    async def slow(request: Any) -> str:
        await asyncio.sleep(0.4)
        return "late"

    with pytest.raises(RequestTimeoutError):
        await client.request("/slow", timeout=0.1)
    assert await client.call("/echo", 1) == 1
    await asyncio.sleep(0.5)  # the late reply arrives and is silently dropped
    assert await client.call("/echo", 2) == 2


async def test_handler_timeout(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(config=SessionConfig(handler_timeout=0.1, linger_timeout=0.2))

    @srv.route("/slow")
    async def slow(request: Any) -> None:
        await asyncio.sleep(5)

    response = await (await client_factory(srv)).request("/slow")
    assert response.status == Status.INTERNAL_ERROR
    assert response.error is not None and "timed out" in response.error["message"]


async def test_server_close_fails_pending_requests(server: LCPServer, client: LCPClient) -> None:
    @server.route("/hang")
    async def hang(request: Any) -> None:
        await asyncio.sleep(30)

    pending = asyncio.create_task(client.request("/hang"))
    await asyncio.sleep(0.1)
    await server.close(grace=0)
    with pytest.raises(ConnectionClosedError):
        await pending
    with pytest.raises(ConnectionClosedError):
        await client.request("/echo", 1)


async def test_graceful_close_lets_running_requests_finish(
    server: LCPServer, client: LCPClient
) -> None:
    @server.route("/work")
    async def work(request: Any) -> str:
        await asyncio.sleep(0.3)
        return "finished"

    pending = asyncio.create_task(client.call("/work"))
    await asyncio.sleep(0.1)
    await server.close(grace=3.0)
    assert await pending == "finished"


async def test_connecting_without_a_server_fails(server_factory: ServerFactory) -> None:
    srv = await server_factory()
    port = srv.port
    await srv.close(grace=0)
    with pytest.raises(OSError):
        await LCPClient("127.0.0.1", port).connect()
    with pytest.raises(ConnectionClosedError):
        await LCPClient("127.0.0.1", port).request("/x")  # never connected


async def test_max_connections_refuses_with_busy(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(max_connections=1)
    first = await raw_factory(srv)
    await first.send(Frame.ping(1))
    assert (await first.recv()).msg_type == MessageType.PONG  # the only slot is taken
    second = await raw_factory(srv)
    refusal = await second.recv()
    assert refusal.msg_type == MessageType.RES and refusal.status == Status.BUSY
    assert refusal.correlation_id == 0
    assert await second.eof() == b""
    await first.send(Frame.ping(2))
    assert (await first.recv()).correlation_id == 2  # the admitted client is unaffected


async def test_keepalive_closes_a_dead_connection() -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()  # never answers PING
        finally:
            writer.close()

    dead = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = dead.sockets[0].getsockname()[1]
    config = SessionConfig(keepalive_interval=0.1, keepalive_timeout=0.1)
    client = LCPClient("127.0.0.1", port, config=config)
    try:
        await client.connect()
        await asyncio.sleep(0.6)
        assert not client.connected
        with pytest.raises(ConnectionClosedError):
            await client.ping()
    finally:
        await client.close()
        dead.close()
        await dead.wait_closed()


async def test_keepalive_keeps_a_live_connection_open(
    server: LCPServer, client_factory: ClientFactory
) -> None:
    client = await client_factory(
        server, config=SessionConfig(keepalive_interval=0.05, keepalive_timeout=1.0)
    )
    await asyncio.sleep(0.4)
    assert client.connected and await client.call("/echo", 1) == 1


async def test_idle_timeout_closes_silent_connections(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(config=SessionConfig(idle_timeout=0.3))
    conn = await raw_factory(srv)
    assert await conn.eof(timeout=3) == b""


async def test_incomplete_frames_time_out(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(config=SessionConfig(idle_timeout=None, frame_timeout=0.3))
    conn = await raw_factory(srv)
    await conn.send(b"\x4c\x43\x01\x01\x00")  # slow-loris: a header that never completes
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await conn.eof(timeout=3) == b""
    assert 0.25 < loop.time() - started < 2.0


async def test_pipelined_requests_in_one_tcp_write(
    server: LCPServer, raw_factory: RawFactory
) -> None:
    conn = await raw_factory(server)
    frames = [
        Frame(MessageType.REQ, cid, metadata=Metadata(target_path="/echo"), payload=b'"x%d"' % cid)
        for cid in (1, 2, 3)
    ]
    await conn.send(b"".join(f.encode() for f in frames))
    replies = {(await conn.recv()).correlation_id: None for _ in range(3)}
    assert set(replies) == {1, 2, 3}


async def test_large_payloads(client: LCPClient) -> None:
    blob = bytes(range(256)) * (4 * 1024 * 1024 // 256)
    assert await client.call("/echo", blob) == blob


async def test_payload_of_exactly_16_mib_is_accepted(client: LCPClient) -> None:
    blob = b"\xa5" * MAX_PAYLOAD_LENGTH
    response = await client.request("/echo", blob, timeout=30)
    assert response.ok and response.payload == blob


async def test_server_side_payload_limit(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(config=SessionConfig(max_payload=1024, linger_timeout=0.2))
    srv.add_route("/echo", lambda r: r.body())
    client = await client_factory(srv)
    assert (await client.request("/echo", b"x" * 1024)).ok
    response = await client.request("/echo", b"x" * 1025)
    assert response.status == Status.PAYLOAD_TOO_LARGE  # answered, then the connection is closed
    with pytest.raises(ConnectionClosedError):
        for _ in range(50):
            await client.ping()
            await asyncio.sleep(0.02)


async def test_server_pushes_nothing_unless_asked(server: LCPServer, client: LCPClient) -> None:
    assert len(server.sessions) == 1
    assert next(iter(server.sessions)).stats.requests == 0


async def test_unhandled_log_levels(caplog: pytest.LogCaptureFixture, client: LCPClient) -> None:
    with caplog.at_level(logging.WARNING, logger="lcp"):
        await client.call("/echo", 1)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_tokens_never_reach_the_server_log(
    server_factory: ServerFactory,
    client_factory: ClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from lcp import StaticTokenAuthenticator

    srv = await server_factory(authenticator=StaticTokenAuthenticator({"top-secret-token": "ada"}))

    @srv.route("/boom")
    async def boom(request: Any) -> None:
        raise RuntimeError("handler failure")  # logged together with the offending frame

    client = await client_factory(srv, auth_token="top-secret-token")
    with caplog.at_level(logging.DEBUG, logger="lcp"):
        assert (await client.request("/boom")).status == Status.INTERNAL_ERROR
    assert "handler failure" in caplog.text
    assert "top-secret-token" not in caplog.text
