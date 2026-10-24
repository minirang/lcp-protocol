"""Echo servers used by the benchmark harness; each runs in its own process.

``python -m benchmarks.servers {lcp,lcp-raw,http1}`` prints ``PORT=<n>`` and then serves until
stdin is closed.  Writing ``mark`` to stdin makes it answer with one ``STATS {...}`` line
(process CPU seconds, GC collections, peak RSS) so the harness can attribute resource usage
to a measurement window.
"""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import threading
import time

from lcp import (
    Encoded,
    Frame,
    FrameDecoder,
    LCPServer,
    MessageType,
    ProtocolError,
    Request,
    SessionConfig,
    Status,
)


def snapshot() -> dict[str, float]:
    stats = {
        "cpu_s": time.process_time(),
        "gc_collections": float(sum(s["collections"] for s in gc.get_stats())),
        "maxrss_kb": 0.0,
    }
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        stats["maxrss_kb"] = rss / 1024 if sys.platform == "darwin" else rss
    except ImportError:  # Windows
        pass
    return stats


async def serve_lcp(stop: asyncio.Event) -> None:
    """The full stack: LCPServer with sessions, routing, timeouts and back-pressure."""
    config = SessionConfig(idle_timeout=None, frame_timeout=None, max_in_flight=100_000)
    server = LCPServer("127.0.0.1", 0, config=config)

    @server.route("/echo")
    async def echo(request: Request) -> Encoded:
        return Encoded(request.payload, request.serializer)

    await server.start()
    print(f"PORT={server.port}", flush=True)
    await stop.wait()
    await server.close(grace=0)


async def serve_lcp_raw(stop: asyncio.Event) -> None:
    """Framing only: FrameDecoder + direct replies (no session layer)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = FrameDecoder()
        try:
            while data := await reader.read(262144):
                decoder.feed(data)
                for frame in decoder:
                    reply = Frame(
                        MessageType.RES,
                        frame.correlation_id,
                        Status.OK,
                        frame.serializer,
                        payload=frame.payload,
                    )
                    writer.writelines(reply.encode_parts())
                await writer.drain()
        except (ProtocolError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=262144)
    print(f"PORT={server.sockets[0].getsockname()[1]}", flush=True)
    await stop.wait()
    server.close()


async def serve_http1(stop: asyncio.Event) -> None:
    """A deliberately minimal HTTP/1.1 keep-alive echo server (text headers, Content-Length)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value)
                body = await reader.readexactly(length) if length else b""
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body) + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=262144)
    print(f"PORT={server.sockets[0].getsockname()[1]}", flush=True)
    await stop.wait()
    server.close()


SERVERS = {"lcp": serve_lcp, "lcp-raw": serve_lcp_raw, "http1": serve_http1}


def _control(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for line in sys.stdin:
        if line.strip() == "mark":
            print("STATS " + json.dumps(snapshot()), flush=True)
    loop.call_soon_threadsafe(stop.set)


async def main(name: str) -> None:
    stop = asyncio.Event()
    threading.Thread(target=_control, args=(asyncio.get_running_loop(), stop), daemon=True).start()
    await SERVERS[name](stop)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in SERVERS:
        sys.exit(f"usage: python -m benchmarks.servers {{{','.join(SERVERS)}}}")
    asyncio.run(main(sys.argv[1]))
