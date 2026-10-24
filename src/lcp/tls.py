"""TLS helpers (spec section 2.1: LCP over TLS, TLS 1.3 or later, optional mutual TLS)."""

from __future__ import annotations

import os
import ssl

PathLike = str | os.PathLike[str]


def create_server_ssl_context(
    certfile: PathLike,
    keyfile: PathLike | None = None,
    *,
    password: str | None = None,
    client_ca: PathLike | None = None,
    require_client_cert: bool = False,
) -> ssl.SSLContext:
    """Server-side context: TLS >= 1.3.

    With ``require_client_cert=True`` clients must present a certificate signed by
    ``client_ca`` (mutual TLS, for high-security service-to-service traffic).  Passing only
    ``client_ca`` requests an *optional* client certificate.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certfile, keyfile, password)
    if require_client_cert and client_ca is None:
        raise ValueError("require_client_cert=True needs client_ca")
    if client_ca is not None:
        context.load_verify_locations(cafile=client_ca)
        context.verify_mode = ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_OPTIONAL
    return context


def create_client_ssl_context(
    *,
    cafile: PathLike | None = None,
    certfile: PathLike | None = None,
    keyfile: PathLike | None = None,
    password: str | None = None,
) -> ssl.SSLContext:
    """Client-side context: TLS >= 1.3, server certificate *and* host name are verified.

    ``cafile`` replaces the system trust store (use it for a private CA); ``certfile`` /
    ``keyfile`` enable mutual TLS.
    """
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if certfile is not None:
        context.load_cert_chain(certfile, keyfile, password)
    return context
