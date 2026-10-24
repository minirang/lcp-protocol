"""Pluggable authentication of the ``AUTH_TOKEN`` metadata entry (spec section 2.1).

An *authenticator* is any callable ``(token: str) -> Principal | bool | None`` (sync or
async).  A falsy result (or ``None``) means "reject" and is answered with
``UNAUTHORIZED`` (``0x02``).  ``True`` accepts as an anonymous principal.

Two small implementations are bundled.  :class:`HS256JWTAuthenticator` is meant for
tests, demos and simple service-to-service setups; for anything else plug in a vetted JWT
or OIDC library behind the same callable interface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

Authenticator = Callable[[str], Any]
"""``(token) -> Principal | bool | None`` or an awaitable of that."""


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    subject: str
    claims: Mapping[str, Any] = field(default_factory=dict)


class StaticTokenAuthenticator:
    """Accepts a fixed set of opaque tokens (``{token: subject}``), compared in constant time."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = [(token.encode("utf-8"), subject) for token, subject in tokens.items()]

    def __call__(self, token: str) -> Principal | None:
        candidate = token.encode("utf-8")
        subject: str | None = None
        for known, name in self._tokens:  # no early exit: timing does not reveal which matched
            if hmac.compare_digest(candidate, known):
                subject = name
        return None if subject is None else Principal(subject)


_B64URL = re.compile(r"[A-Za-z0-9_-]*")


def _b64url_decode(segment: str) -> bytes:
    if not _B64URL.fullmatch(segment):
        raise ValueError("invalid base64url segment")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class HS256JWTAuthenticator:
    """Minimal JWT (HS256) verifier.

    Only ``alg == "HS256"`` is accepted (``none`` and every other algorithm are rejected),
    the signature is verified in constant time *before* any claim is trusted, and ``exp``
    is required by default.  ``nbf``, ``iss`` and ``aud`` are checked when configured/present.
    """

    def __init__(
        self,
        secret: bytes | str,
        *,
        issuer: str | None = None,
        audience: str | None = None,
        leeway: float = 0.0,
        require_exp: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(self._secret) < 16:
            raise ValueError("HS256 secret must be at least 16 bytes")
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway
        self._require_exp = require_exp
        self._clock = clock

    def __call__(self, token: str) -> Principal | None:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
            header = json.loads(_b64url_decode(header_b64))
            claims = json.loads(_b64url_decode(payload_b64))
            signature = _b64url_decode(signature_b64)
        except (ValueError, RecursionError):
            return None
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            return None
        expected = hmac.new(
            self._secret, f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            return None
        if not isinstance(claims, dict):
            return None

        now = self._clock()
        exp = claims.get("exp")
        if exp is None:
            if self._require_exp:
                return None
        elif not _is_number(exp) or now - self._leeway >= exp:
            return None
        nbf = claims.get("nbf")
        if nbf is not None and (not _is_number(nbf) or now + self._leeway < nbf):
            return None
        if self._issuer is not None and claims.get("iss") != self._issuer:
            return None
        if self._audience is not None:
            aud = claims.get("aud")
            if self._audience not in (aud if isinstance(aud, list) else [aud]):
                return None
        subject = claims.get("sub")
        return Principal(subject if isinstance(subject, str) else "", claims)


def issue_hs256_jwt(
    secret: bytes | str,
    claims: Mapping[str, Any] | None = None,
    *,
    expires_in: float | None = 3600.0,
    now: float | None = None,
) -> str:
    """Create an HS256 JWT (helper for tests, demos and the CLI)."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    payload = dict(claims or {})
    if expires_in is not None:
        payload["exp"] = int((time.time() if now is None else now) + expires_in)
    header_b64 = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("ascii"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"
