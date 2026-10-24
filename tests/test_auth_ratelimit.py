from __future__ import annotations

import base64
import json

import pytest

from lcp import (
    HS256JWTAuthenticator,
    Principal,
    RateLimit,
    StaticTokenAuthenticator,
    TokenBucket,
    issue_hs256_jwt,
)

SECRET = "0123456789abcdef-secret"
NOW = 1_700_000_000.0


def b64(data: dict | bytes) -> str:
    raw = json.dumps(data).encode() if isinstance(data, dict) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_static_tokens() -> None:
    auth = StaticTokenAuthenticator({"tok-a": "alice", "tok-b": "bob"})
    assert auth("tok-b") == Principal("bob")
    assert auth("tok-c") is None and auth("") is None and auth("tok-") is None


def jwt_auth(**kwargs: object) -> HS256JWTAuthenticator:
    return HS256JWTAuthenticator(SECRET, clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


def test_jwt_accepts_a_valid_token() -> None:
    token = issue_hs256_jwt(SECRET, {"sub": "alice", "role": "admin"}, expires_in=60, now=NOW)
    principal = jwt_auth()(token)
    assert principal is not None and principal.subject == "alice"
    assert principal.claims["role"] == "admin"


def test_jwt_time_claims() -> None:
    auth = jwt_auth(leeway=5)
    assert auth(issue_hs256_jwt(SECRET, {"sub": "a"}, expires_in=-4, now=NOW)) is not None  # leeway
    assert auth(issue_hs256_jwt(SECRET, {"sub": "a"}, expires_in=-10, now=NOW)) is None
    assert jwt_auth()(issue_hs256_jwt(SECRET, {"sub": "a"}, expires_in=0, now=NOW)) is None
    early = issue_hs256_jwt(SECRET, {"sub": "a", "nbf": NOW + 100}, expires_in=500, now=NOW)
    assert jwt_auth()(early) is None
    no_exp = issue_hs256_jwt(SECRET, {"sub": "a"}, expires_in=None)
    assert jwt_auth()(no_exp) is None  # exp is required by default
    assert jwt_auth(require_exp=False)(no_exp) is not None
    assert jwt_auth()(issue_hs256_jwt(SECRET, {"exp": "tomorrow"}, expires_in=None)) is None


def test_jwt_issuer_and_audience() -> None:
    claims = {"sub": "a", "iss": "lcp-auth", "aud": ["svc-1", "svc-2"]}
    token = issue_hs256_jwt(SECRET, claims, expires_in=60, now=NOW)
    assert jwt_auth(issuer="lcp-auth", audience="svc-2")(token) is not None
    assert jwt_auth(issuer="other")(token) is None
    assert jwt_auth(audience="svc-3")(token) is None
    single = issue_hs256_jwt(SECRET, {"aud": "svc-1"}, expires_in=60, now=NOW)
    assert jwt_auth(audience="svc-1")(single) is not None


def test_jwt_rejects_forgeries() -> None:
    good = issue_hs256_jwt(SECRET, {"sub": "alice"}, expires_in=60, now=NOW)
    header, payload, signature = good.split(".")
    forged_claims = b64({"sub": "root", "exp": NOW + 60})
    attacks = {
        "wrong secret": issue_hs256_jwt(
            "another-secret-value", {"sub": "a"}, expires_in=60, now=NOW
        ),
        "tampered payload": f"{header}.{forged_claims}.{signature}",
        "alg none": f"{b64({'alg': 'none'})}.{forged_claims}.",
        "alg none + sig": f"{b64({'alg': 'none'})}.{forged_claims}.{signature}",
        "alg RS256": f"{b64({'alg': 'RS256'})}.{payload}.{signature}",
        "alg lowercase": f"{b64({'alg': 'hs256'})}.{payload}.{signature}",
        "missing sig": f"{header}.{payload}",
        "extra part": f"{good}.x",
        "empty": "",
        "garbage": "not a jwt",
        "bad base64": f"{header}.{payload}.@@@@",
        "non-dict claims": f"{header}.{b64(b'[1]')}.{signature}",
    }
    auth = jwt_auth()
    for name, token in attacks.items():
        assert auth(token) is None, name


def test_jwt_secret_must_be_long_enough() -> None:
    with pytest.raises(ValueError):
        HS256JWTAuthenticator("short")


def test_token_bucket() -> None:
    now = [100.0]
    bucket = TokenBucket(rate=2, burst=3, clock=lambda: now[0])
    assert [bucket.try_acquire() for _ in range(3)] == [0.0, 0.0, 0.0]  # burst
    wait = bucket.try_acquire()
    assert wait == pytest.approx(0.5)  # 1 token at 2/s
    now[0] += 0.25
    assert bucket.try_acquire() == pytest.approx(0.25)
    now[0] += 0.25
    assert bucket.try_acquire() == 0.0
    now[0] += 1000  # never exceeds the burst capacity
    assert [bucket.try_acquire() for _ in range(4)][-1] > 0


def test_token_bucket_validation() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        TokenBucket(1, burst=0.5)
    assert TokenBucket(5).capacity == 5
    assert RateLimit(10).burst is None
