"""Application back-pressure: BUSY (0x09) with a retry hint, honoured by the client.

The server runs at most 2 handlers at once and admits at most 4 calls in total; a burst of
12 concurrent calls is therefore throttled with BUSY replies, and the client transparently
waits for the server's ``retry_after`` hint and retries.

Run:  python examples/04_backpressure.py
"""

from __future__ import annotations

import asyncio

from lcp import LCPClient, LCPServer, Request, SessionConfig


async def main() -> None:
    server = LCPServer("127.0.0.1", 0, config=SessionConfig(max_concurrency=2, max_in_flight=4))

    @server.route("/v1/work")
    async def work(request: Request) -> int:
        await asyncio.sleep(0.05)
        return request.body() * 2

    client_config = SessionConfig(busy_retries=50, keepalive_interval=None)
    async with server, LCPClient("127.0.0.1", server.port, config=client_config) as client:
        results = await asyncio.gather(*(client.call("/v1/work", n) for n in range(12)))
        busy = sum(s.stats.busy_rejections for s in server.sessions)
        print("results       :", results)
        print("BUSY replies  :", busy, "(all retried automatically)")


if __name__ == "__main__":
    asyncio.run(main())
