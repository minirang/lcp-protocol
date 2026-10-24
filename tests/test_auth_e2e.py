from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lcp import (
    ConnectionClosedError,
    Frame,
    HS256JWTAuthenticator,
    MessageType,
    Metadata,
    Principal,
    SessionConfig,
    StaticTokenAuthenticator,
    Status,
    issue_hs256_jwt,
)

from .conftest import ClientFactory, RawFactory, ServerFactory

SECRET = "an-example-hs256-secret"


async def test_missing_and_invalid_tokens_are_unauthorized(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(authenticator=StaticTokenAuthenticator({"good": "alice"}))

    @srv.route("/whoami")
    async def whoami(request: Any) -> dict[str, Any]:
        return {"sub": request.principal.subject}

    anonymous = await client_factory(srv)
    wrong = await client_factory(srv, auth_token="nope")
    ok = await client_factory(srv, auth_token="good")
    assert (await anonymous.request("/whoami")).status == Status.UNAUTHORIZED
    assert (await wrong.request("/whoami")).status == Status.UNAUTHORIZED
    assert await ok.call("/whoami") == {"sub": "alice"}


async def test_unauthorized_never_runs_the_handler(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(authenticator=StaticTokenAuthenticator({"good": "alice"}))
    calls: list[int] = []
    srv.add_route("/secret", lambda request: calls.append(1))
    await (await client_factory(srv)).request("/secret")
    assert not calls


async def test_connection_is_closed_after_repeated_failures(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        authenticator=StaticTokenAuthenticator({"good": "alice"}),
        config=SessionConfig(max_auth_failures=3, linger_timeout=0.2),
    )
    srv.add_route("/x", lambda request: "ok")
    client = await client_factory(srv, auth_token="bad")
    for _ in range(3):
        assert (await client.request("/x")).status == Status.UNAUTHORIZED
    with pytest.raises(ConnectionClosedError):
        await client.request("/x")  # spec 2.1: the server may close the socket right away


async def test_a_single_failure_can_close_immediately(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(
        authenticator=StaticTokenAuthenticator({}), config=SessionConfig(max_auth_failures=1)
    )
    srv.add_route("/x", lambda request: "ok")
    client = await client_factory(srv, auth_token="bad")
    assert (await client.request("/x")).status == Status.UNAUTHORIZED
    with pytest.raises(ConnectionClosedError):
        await client.request("/x")


async def test_failure_counter_resets_after_a_success(
    server_factory: ServerFactory, raw_factory: RawFactory
) -> None:
    srv = await server_factory(
        authenticator=StaticTokenAuthenticator({"good": "alice"}),
        config=SessionConfig(max_auth_failures=2, linger_timeout=0.2),
    )
    srv.add_route("/x", lambda request: "ok")
    conn = await raw_factory(srv)
    for cid, token in enumerate(["bad", "good", "bad", "good", "bad", "good"], 1):
        meta = Metadata(auth_token=token, target_path="/x")
        await conn.send(Frame(MessageType.REQ, cid, metadata=meta))
        reply = await conn.recv()
        assert reply.status == (Status.OK if token == "good" else Status.UNAUTHORIZED)


async def test_keepalive_needs_no_token(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(authenticator=StaticTokenAuthenticator({"good": "alice"}))
    assert await (await client_factory(srv)).ping() > 0


async def test_events_are_authenticated_too(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    srv = await server_factory(authenticator=StaticTokenAuthenticator({"good": "alice"}))
    seen: list[str] = []
    srv.on_event("*")(lambda request: seen.append(request.principal.subject))
    assert (await (await client_factory(srv)).send_event("/e")).status == Status.UNAUTHORIZED
    ok = await (await client_factory(srv, auth_token="good")).send_event("/e")
    assert ok.ok and seen == ["alice"]


async def test_async_and_boolean_authenticators(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    async def lookup(token: str) -> Principal | None:
        await asyncio.sleep(0)
        return Principal(f"user-{token}") if token.startswith("u") else None

    srv = await server_factory(authenticator=lookup)
    srv.add_route("/who", lambda request: request.principal.subject)
    assert await (await client_factory(srv, auth_token="u42")).call("/who") == "user-u42"
    assert (
        await (await client_factory(srv, auth_token="x")).request("/who")
    ).status == Status.UNAUTHORIZED

    boolean = await server_factory(authenticator=lambda token: token == "open-sesame")
    boolean.add_route("/who", lambda request: request.principal.subject)
    assert (
        await (await client_factory(boolean, auth_token="open-sesame")).call("/who") == "anonymous"
    )


async def test_authenticator_failures_are_internal_errors_not_auth_failures(
    server_factory: ServerFactory, client_factory: ClientFactory
) -> None:
    def broken(token: str) -> Principal:
        raise RuntimeError("identity provider is down")

    srv = await server_factory(authenticator=broken, config=SessionConfig(max_auth_failures=1))
    srv.add_route("/x", lambda request: "ok")
    client = await client_factory(srv, auth_token="whatever")
    for _ in range(3):  # does not count towards max_auth_failures, so no disconnect
        response = await client.request("/x")
        assert response.status == Status.INTERNAL_ERROR
        assert b"identity provider" not in response.payload


async def test_jwt_end_to_end(server_factory: ServerFactory, client_factory: ClientFactory) -> None:
    srv = await server_factory(authenticator=HS256JWTAuthenticator(SECRET, audience="lcp-api"))
    srv.add_route("/me", lambda request: dict(request.principal.claims))
    good = issue_hs256_jwt(
        SECRET, {"sub": "alice", "aud": "lcp-api", "role": "admin"}, expires_in=60
    )
    expired = issue_hs256_jwt(SECRET, {"sub": "alice", "aud": "lcp-api"}, expires_in=-1)
    wrong_aud = issue_hs256_jwt(SECRET, {"sub": "alice", "aud": "other"}, expires_in=60)
    claims = await (await client_factory(srv, auth_token=good)).call("/me")
    assert claims["sub"] == "alice" and claims["role"] == "admin"
    for token in (expired, wrong_aud):
        response = await (await client_factory(srv, auth_token=token)).request("/me")
        assert response.status == Status.UNAUTHORIZED
