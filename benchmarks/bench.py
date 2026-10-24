"""Benchmark harness for spec section 7 (measurement guidelines).

Measures, per protocol x message size x connection count:
TPS, latency p50/p99/max, client and server CPU time per request, server peak RSS and GC
collections per 1000 requests (the Python analogue of "allocations/op").

Protocols
  lcp      full LCP stack (LCPServer / LCPClient)
  lcp-raw  LCP framing only (FrameDecoder, no session layer) - compare this one with http1
  http1    minimal asyncio HTTP/1.1 keep-alive baseline (JSON body, Content-Length)

The spec also lists HTTP/2 (gRPC/Protobuf) and MQTT (QoS 1) as comparison targets; they need
external packages and a broker, so they are not bundled - add a class to ``clients.py`` and an
entry to ``servers.py`` to extend the harness.

Run:  python -m benchmarks.bench --sizes 100,1024,65536 --concurrency 1,16,128
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .clients import TARGETS, Conn

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Result:
    protocol: str
    size: int
    connections: int
    requests: int
    tps: float
    p50_ms: float
    p99_ms: float
    max_ms: float
    client_cpu_us: float
    server_cpu_us: float
    server_rss_mb: float
    server_gc_per_1k: float


def make_payload(size: int) -> bytes:
    if size < 8:
        raise ValueError("size must be at least 8 bytes")
    return b'{"d":"' + b"x" * (size - 8) + b'"}'


async def read_stats(proc: asyncio.subprocess.Process) -> dict[str, float]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(b"mark\n")
    await proc.stdin.drain()
    while True:
        line = (await asyncio.wait_for(proc.stdout.readline(), 10)).decode()
        if line.startswith("STATS "):
            data: dict[str, float] = json.loads(line[6:])
            return data


async def run_scenario(
    protocol: str, size: int, connections: int, duration: float, warmup: float
) -> Result:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "benchmarks.servers",
        protocol,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        cwd=ROOT,
    )
    assert proc.stdout is not None and proc.stdin is not None
    conns: list[Conn] = []
    try:
        banner = (await asyncio.wait_for(proc.stdout.readline(), 30)).decode().strip()
        if not banner.startswith("PORT="):
            raise RuntimeError(f"server failed to start: {banner!r}")
        port = int(banner[5:])
        conns = [await TARGETS[protocol].open(port) for _ in range(connections)]  # type: ignore[attr-defined]
        payload = make_payload(size)

        latencies: list[float] = []
        loop = asyncio.get_running_loop()
        t_start = loop.time()
        measure_from = t_start + warmup
        stop_at = measure_from + duration
        window: dict[str, float] = {}

        async def worker(conn: Conn) -> None:
            while (started := loop.time()) < stop_at:
                reply = await conn.request(payload)
                if reply != payload:
                    raise RuntimeError("echo mismatch")
                if started >= measure_from:
                    latencies.append(loop.time() - started)

        async def window_probe() -> None:
            await asyncio.sleep(warmup)
            window["server_before"] = await read_stats(proc)
            window["client_before"] = time.process_time()
            window["t0"] = loop.time()
            await asyncio.sleep(duration)
            window["t1"] = loop.time()
            window["client_after"] = time.process_time()
            window["server_after"] = await read_stats(proc)

        await asyncio.gather(window_probe(), *(worker(c) for c in conns))

        done = len(latencies)
        if done == 0:
            raise RuntimeError("no request completed in the measurement window")
        elapsed = window["t1"] - window["t0"]
        before, after = window["server_before"], window["server_after"]  # type: ignore[index]
        latencies.sort()
        quantiles = statistics.quantiles(latencies, n=100) if done >= 100 else None
        p99 = quantiles[98] if quantiles else latencies[-1]
        return Result(
            protocol=protocol,
            size=size,
            connections=connections,
            requests=done,
            tps=done / elapsed,
            p50_ms=statistics.median(latencies) * 1000,
            p99_ms=p99 * 1000,
            max_ms=latencies[-1] * 1000,
            client_cpu_us=(window["client_after"] - window["client_before"]) / done * 1e6,
            server_cpu_us=(after["cpu_s"] - before["cpu_s"]) / done * 1e6,  # type: ignore[index]
            server_rss_mb=after["maxrss_kb"] / 1024,  # type: ignore[index]
            server_gc_per_1k=(after["gc_collections"] - before["gc_collections"]) / done * 1000,  # type: ignore[index]
        )
    finally:
        for conn in conns:
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await conn.close()
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()


COLUMNS = [
    ("protocol", "protocol", "{:<8}"),
    ("size", "size", "{:>6}"),
    ("conns", "connections", "{:>5}"),
    ("requests", "requests", "{:>8}"),
    ("TPS", "tps", "{:>9.0f}"),
    ("p50 ms", "p50_ms", "{:>7.3f}"),
    ("p99 ms", "p99_ms", "{:>7.3f}"),
    ("max ms", "max_ms", "{:>8.2f}"),
    ("cli us/op", "client_cpu_us", "{:>9.1f}"),
    ("srv us/op", "server_cpu_us", "{:>9.1f}"),
    ("srv RSS MB", "server_rss_mb", "{:>10.1f}"),
    ("gc/1k", "server_gc_per_1k", "{:>6.2f}"),
]


def print_table(results: list[Result]) -> None:
    header = []
    for title, _, template in COLUMNS:
        width = len(template.format(0))
        header.append(title.ljust(width) if "<" in template else title.rjust(width))
    print("  ".join(header))
    for result in results:
        print("  ".join(template.format(getattr(result, attr)) for _, attr, template in COLUMNS))


async def main_async(args: argparse.Namespace) -> list[Result]:
    results: list[Result] = []
    for size in args.sizes:
        for connections in args.concurrency:
            for protocol in args.protocols:
                result = await run_scenario(protocol, size, connections, args.duration, args.warmup)
                results.append(result)
                print(
                    f"  done: {protocol} size={size} conns={connections} -> {result.tps:.0f} TPS",
                    file=sys.stderr,
                    flush=True,
                )
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    def ints(text: str) -> list[int]:
        return [int(part) for part in text.split(",") if part]

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--protocols", default="lcp,lcp-raw,http1", type=lambda t: t.split(","))
    parser.add_argument("--sizes", default="100,1024,65536", type=ints, help="payload bytes")
    parser.add_argument("--concurrency", default="1,16,128", type=ints, help="connections")
    parser.add_argument("--duration", type=float, default=3.0, help="measured seconds per scenario")
    parser.add_argument("--warmup", type=float, default=0.5)
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    args = parser.parse_args(argv)
    unknown = set(args.protocols) - set(TARGETS)
    if unknown:
        parser.error(f"unknown protocol(s): {', '.join(sorted(unknown))}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = asyncio.run(main_async(args))
    print()
    print_table(results)
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
