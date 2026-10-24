"""Minimal request/response: a server with two routes and a client calling them.

Run:  python examples/01_echo.py
"""

from __future__ import annotations

import asyncio

from lcp import LCPClient, LCPServer, Request, Status, StatusError, status_name


async def main() -> None:
    server = LCPServer("127.0.0.1", 0)  # port 0: let the OS pick a free port

    @server.route("/v1/echo")
    async def echo(request: Request) -> object:
        return request.body()  # JSON in, JSON out

    @server.route("/v1/user/profile")
    async def profile(request: Request) -> dict[str, object]:
        user_id = request.json()["id"]
        if user_id != 7:
            raise StatusError(Status.NOT_FOUND, f"no user {user_id}")
        return {"id": 7, "name": "Ada", "roles": ["admin"]}

    async with server, LCPClient("127.0.0.1", server.port) as client:
        print("echo     ->", await client.call("/v1/echo", {"hello": "LCP", "한글": True}))
        print("profile  ->", await client.call("/v1/user/profile", {"id": 7}))

        missing = await client.request("/v1/user/profile", {"id": 8})
        print("missing  ->", status_name(missing.status), missing.error)

        print("ping     ->", f"{await client.ping() * 1000:.2f} ms")


if __name__ == "__main__":
    asyncio.run(main())
