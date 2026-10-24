# LCP — Lightweight Communication Protocol (Python reference implementation)

A complete, dependency-free implementation of the **LCP v1.3.0** specification (wire version `0x01`):
a 16-byte binary header, TLV metadata and JSON / Protobuf / raw payloads over TCP or TLS 1.3.
The original (Korean) specification is kept verbatim in [`docs/spec/`](docs/spec/LCP_Protocol_Specification.md);
the Korean [README.md](README.md) is the primary documentation.

```python
import asyncio
from lcp import LCPClient, LCPServer


async def main() -> None:
    server = LCPServer("127.0.0.1", 0)

    @server.route("/v1/echo")
    async def echo(request):
        return request.body()

    async with server, LCPClient("127.0.0.1", server.port) as client:
        print(await client.call("/v1/echo", {"hello": "LCP"}))


asyncio.run(main())
```

**What is included**

- asyncio server and client: request/response, acknowledged events (both directions), PING/PONG keep-alive,
  correlation-ID multiplexing
- strict framing (`FrameDecoder`) that implements every check of spec §6 and is differentially tested against
  the reference framer *executed straight from the spec text*
- application back-pressure (`BUSY` / `RATE_LIMITED` with `retry_after_ms`): token bucket, admission cap, payload
  byte budget, client-side send pause and automatic retry
- TLS 1.3 / mutual TLS helpers, pluggable authentication of `AUTH_TOKEN` (HS256 JWT and static tokens included)
- CLI (`lcp serve | request | ping | inspect | encode`), benchmark harness for spec §7, runnable examples
- `docs/IMPLEMENTATION_NOTES.md` lists every place where the spec is ambiguous or inconsistent and the decision
  taken (e.g. the "4-byte alignment" claim that has no padding rule, PONG correlation ids, event semantics)

**Install and develop**

```bash
pip install -e ".[dev]"      # Python >= 3.10
pytest && ruff check . && mypy
```

**Honest performance note** — in pure Python a minimal HTTP/1.1 keep-alive echo is *faster* than this LCP
implementation (text header parsing runs in C inside `asyncio`; LCP framing runs in the interpreter). See
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md). This repository is the semantic reference; ports to compiled languages
are where the binary framing pays off.

License: MIT
