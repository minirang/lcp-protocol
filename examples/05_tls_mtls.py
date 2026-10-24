"""LCP over TLS 1.3 with mutual TLS (needs ``pip install trustme`` to mint a throw-away CA).

For real deployments use certificates from your own CA / PKI; see scripts/gen_dev_certs.py.

Run:  python examples/05_tls_mtls.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import trustme

from lcp import (
    LCPClient,
    LCPServer,
    Request,
    create_client_ssl_context,
    create_server_ssl_context,
)


async def main() -> None:
    ca = trustme.CA()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        ca.cert_pem.write_to_path(folder / "ca.pem")
        ca.issue_cert("localhost", "127.0.0.1").private_key_and_cert_chain_pem.write_to_path(
            folder / "server.pem"
        )
        ca.issue_cert("service-a").private_key_and_cert_chain_pem.write_to_path(
            folder / "client.pem"
        )

        server = LCPServer(
            "127.0.0.1",
            0,
            ssl=create_server_ssl_context(
                folder / "server.pem", client_ca=folder / "ca.pem", require_client_cert=True
            ),
        )

        @server.route("/v1/hello")
        async def hello(request: Request) -> dict[str, str]:
            return {"message": "hello over mutual TLS"}

        client_ssl = create_client_ssl_context(
            cafile=folder / "ca.pem", certfile=folder / "client.pem"
        )
        async with server, LCPClient("127.0.0.1", server.port, ssl=client_ssl) as client:
            print(await client.call("/v1/hello"))
            print("negotiated:", client.session.tls_version)


if __name__ == "__main__":
    asyncio.run(main())
