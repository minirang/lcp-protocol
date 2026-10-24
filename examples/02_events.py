"""Acknowledged events (EVT / EVT_ACK) in both directions.

Run:  python examples/02_events.py
"""

from __future__ import annotations

import asyncio

from lcp import LCPClient, LCPServer, Request, status_name


async def main() -> None:
    server = LCPServer("127.0.0.1", 0)

    @server.on_event("/v1/events/click")
    async def on_click(request: Request) -> None:
        print("server got event:", request.path, request.body())

    async with server, LCPClient("127.0.0.1", server.port) as client:

        @client.on_event("/v1/news")
        async def on_news(request: Request) -> None:
            print("client got push:", request.body())

        # client -> server: returns once the server's handler has run and acked
        ack = await client.send_event("/v1/events/click", {"x": 10, "y": 20})
        print("ack status:", status_name(ack.status))

        # server -> client: push to every connected client
        await server.broadcast_event("/v1/news", {"headline": "LCP v0.1 released"})


if __name__ == "__main__":
    asyncio.run(main())
