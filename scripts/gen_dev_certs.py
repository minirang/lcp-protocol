#!/usr/bin/env python3
"""Create a throw-away CA plus a server and a client certificate for local TLS / mTLS tests.

    pip install trustme
    python scripts/gen_dev_certs.py certs/
    lcp serve --tls-cert certs/server.pem --client-ca certs/ca.pem --require-client-cert
    lcp request /v1/echo --json '{"a": 1}' --ca certs/ca.pem --cert certs/client.pem

DEVELOPMENT ONLY. The CA key is discarded after signing and the certificates are short
lived demo material; production deployments need certificates from a real PKI.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("directory", type=Path, help="output directory (created if missing)")
    parser.add_argument(
        "--server-name",
        action="append",
        help="DNS name or IP address of the server certificate (repeatable; "
        "default: localhost, 127.0.0.1, ::1)",
    )
    parser.add_argument(
        "--client-name", default="lcp-client", help="identity of the client certificate"
    )
    args = parser.parse_args(argv)

    try:
        import trustme
    except ImportError:
        print("this script needs the 'trustme' package: pip install trustme", file=sys.stderr)
        return 1

    out: Path = args.directory
    out.mkdir(parents=True, exist_ok=True)
    names = args.server_name or ["localhost", "127.0.0.1", "::1"]

    ca = trustme.CA()
    ca.cert_pem.write_to_path(out / "ca.pem")
    ca.issue_cert(*names).private_key_and_cert_chain_pem.write_to_path(out / "server.pem")
    ca.issue_cert(args.client_name).private_key_and_cert_chain_pem.write_to_path(out / "client.pem")
    for key_file in ("server.pem", "client.pem"):  # these contain private keys
        os.chmod(out / key_file, 0o600)

    print(f"wrote {out}/ca.pem, {out}/server.pem, {out}/client.pem")
    return 0


if __name__ == "__main__":
    sys.exit(main())
