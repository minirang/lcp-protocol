"""LCP over TLS (spec 2.1): TLS 1.3 minimum, certificate checks, optional mutual TLS."""

from __future__ import annotations

import ssl
import types
import warnings
from pathlib import Path

import pytest
import trustme

from lcp import (
    LCPClient,
    LCPError,
    LCPServer,
    create_client_ssl_context,
    create_server_ssl_context,
)

from .conftest import ClientFactory, ServerFactory

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> types.SimpleNamespace:
    folder = tmp_path_factory.mktemp("pki")
    ca, other_ca = trustme.CA(), trustme.CA()
    paths = {
        "ca": folder / "ca.pem",
        "other_ca": folder / "other-ca.pem",
        "server": folder / "server.pem",
        "client": folder / "client.pem",
        "rogue_client": folder / "rogue-client.pem",
    }
    ca.cert_pem.write_to_path(paths["ca"])
    other_ca.cert_pem.write_to_path(paths["other_ca"])
    ca.issue_cert("localhost", "127.0.0.1").private_key_and_cert_chain_pem.write_to_path(
        paths["server"]
    )
    ca.issue_cert("client.example.test").private_key_and_cert_chain_pem.write_to_path(
        paths["client"]
    )
    other_ca.issue_cert("rogue.example.test").private_key_and_cert_chain_pem.write_to_path(
        paths["rogue_client"]
    )
    return types.SimpleNamespace(**paths)


async def tls_server(
    server_factory: ServerFactory, pki: types.SimpleNamespace, **kwargs: object
) -> LCPServer:
    srv = await server_factory(ssl=create_server_ssl_context(pki.server, **kwargs))  # type: ignore[arg-type]
    srv.add_route("/echo", lambda request: request.body())
    return srv


async def test_tls13_roundtrip(
    server_factory: ServerFactory, client_factory: ClientFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki)
    client = await client_factory(srv, ssl=create_client_ssl_context(cafile=pki.ca))
    assert await client.call("/echo", {"secure": True}) == {"secure": True}
    assert client.session.tls_version == "TLSv1.3"
    assert next(iter(srv.sessions)).tls_version == "TLSv1.3"
    assert client.session.peer_certificate is not None  # the verified server certificate


async def test_explicit_server_name(
    server_factory: ServerFactory, client_factory: ClientFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki)
    client = await client_factory(
        srv, ssl=create_client_ssl_context(cafile=pki.ca), server_hostname="localhost"
    )
    assert await client.call("/echo", 1) == 1


async def test_tls12_only_clients_are_rejected(
    server_factory: ServerFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(pki.ca)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises((ssl.SSLError, OSError)):
        await LCPClient("127.0.0.1", srv.port, ssl=context).connect()


async def test_untrusted_server_certificate_is_rejected(
    server_factory: ServerFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki)
    context = create_client_ssl_context(cafile=pki.other_ca)
    with pytest.raises(ssl.SSLCertVerificationError):
        await LCPClient("127.0.0.1", srv.port, ssl=context).connect()


async def test_host_name_is_verified(
    server_factory: ServerFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki)
    context = create_client_ssl_context(cafile=pki.ca)
    with pytest.raises(ssl.SSLCertVerificationError):
        await LCPClient(
            "127.0.0.1", srv.port, ssl=context, server_hostname="wrong.example"
        ).connect()


async def test_plain_client_cannot_talk_to_a_tls_server(
    server_factory: ServerFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki)
    client = LCPClient("127.0.0.1", srv.port)
    try:
        await client.connect()
        with pytest.raises((LCPError, OSError)):
            await client.ping(timeout=2)
    finally:
        await client.close()


async def test_mutual_tls_requires_a_client_certificate(
    server_factory: ServerFactory, client_factory: ClientFactory, pki: types.SimpleNamespace
) -> None:
    srv = await tls_server(server_factory, pki, client_ca=pki.ca, require_client_cert=True)

    # no certificate: TLS 1.3 may only report the failure on the first read/write
    anonymous = LCPClient("127.0.0.1", srv.port, ssl=create_client_ssl_context(cafile=pki.ca))
    try:
        with pytest.raises((ssl.SSLError, OSError, LCPError)):
            await anonymous.connect()
            await anonymous.request("/echo", 1, timeout=2)
    finally:
        await anonymous.close()

    # certificate from another CA
    rogue = LCPClient(
        "127.0.0.1",
        srv.port,
        ssl=create_client_ssl_context(cafile=pki.ca, certfile=pki.rogue_client),
    )
    try:
        with pytest.raises((ssl.SSLError, OSError, LCPError)):
            await rogue.connect()
            await rogue.request("/echo", 1, timeout=2)
    finally:
        await rogue.close()

    trusted = await client_factory(
        srv, ssl=create_client_ssl_context(cafile=pki.ca, certfile=pki.client)
    )
    assert await trusted.call("/echo", "hello") == "hello"
    assert next(iter(srv.sessions)).peer_certificate is not None


def test_ssl_context_helpers(pki: types.SimpleNamespace) -> None:
    server = create_server_ssl_context(pki.server)
    assert server.minimum_version == ssl.TLSVersion.TLSv1_3
    assert server.verify_mode == ssl.CERT_NONE
    optional = create_server_ssl_context(pki.server, client_ca=pki.ca)
    assert optional.verify_mode == ssl.CERT_OPTIONAL
    required = create_server_ssl_context(pki.server, client_ca=pki.ca, require_client_cert=True)
    assert required.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ValueError):
        create_server_ssl_context(pki.server, require_client_cert=True)
    client = create_client_ssl_context(cafile=pki.ca)
    assert client.minimum_version == ssl.TLSVersion.TLSv1_3
    assert client.verify_mode == ssl.CERT_REQUIRED and client.check_hostname


def test_plaintext_on_a_public_address_warns(pki: types.SimpleNamespace) -> None:
    with pytest.warns(UserWarning, match="TLS"):
        LCPServer("0.0.0.0", 0)
    with pytest.warns(UserWarning, match="TLS"):
        LCPServer(None, 0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        LCPServer("127.0.0.1", 0)
        LCPServer("localhost", 0)
        LCPServer("::1", 0)
        LCPServer("0.0.0.0", 0, ssl=create_server_ssl_context(Path(pki.server)))
