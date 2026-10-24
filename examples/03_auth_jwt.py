"""AUTH_TOKEN metadata carrying a JWT; failed authentication answers UNAUTHORIZED (0x02).

Run:  python examples/03_auth_jwt.py
"""

from __future__ import annotations

import asyncio

from lcp import HS256JWTAuthenticator, LCPClient, LCPServer, Request, issue_hs256_jwt, status_name

SECRET = "change-me-please-32-bytes-minimum"


async def main() -> None:
    server = LCPServer(
        "127.0.0.1", 0, authenticator=HS256JWTAuthenticator(SECRET, audience="lcp-demo")
    )

    @server.route("/v1/me")
    async def me(request: Request) -> dict[str, object]:
        assert request.principal is not None
        return {"subject": request.principal.subject, "role": request.principal.claims.get("role")}

    good = issue_hs256_jwt(
        SECRET, {"sub": "ada", "aud": "lcp-demo", "role": "admin"}, expires_in=60
    )
    expired = issue_hs256_jwt(SECRET, {"sub": "ada", "aud": "lcp-demo"}, expires_in=-1)

    async with server:
        for label, token in (("valid", good), ("expired", expired), ("missing", None)):
            async with LCPClient("127.0.0.1", server.port, auth_token=token) as client:
                response = await client.request("/v1/me")
                outcome = response.json() if response.ok else status_name(response.status)
                print(f"{label:8} token -> {outcome}")


if __name__ == "__main__":
    asyncio.run(main())
