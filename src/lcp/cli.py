"""Command line toolkit: ``lcp serve | request | ping | inspect | encode``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from . import SPEC_VERSION, __version__
from .auth import StaticTokenAuthenticator
from .client import LCPClient
from .codec import Encoded, JsonCodec
from .constants import DEFAULT_PORT, HEADER_SIZE, MAGIC
from .enums import MessageType, Serializer, Status, Tag, status_name
from .errors import DecodeError, LCPError, MetadataError, ProtocolError
from .frame import Frame
from .framer import FrameDecoder, RawFrame
from .message import Request
from .metadata import Metadata
from .server import LCPServer
from .session import SessionConfig
from .tls import create_client_ssl_context, create_server_ssl_context

_SERIALIZERS = {"json": Serializer.JSON, "protobuf": Serializer.PROTOBUF, "raw": Serializer.RAW}


# -- inspect / encode ------------------------------------------------------------------------


def parse_hex(text: str) -> bytes:
    """Parse hex text; whitespace, ``,``, ``:``, ``_``, ``-`` and ``0x`` prefixes are ignored."""
    cleaned = re.sub(r"0x|[\s,:_-]", "", text)
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"not valid hex: {exc}") from exc


def _payload_lines(serializer: int, payload: bytes, limit: int = 256) -> list[str]:
    if not payload:
        return []
    shown, more = payload[:limit], len(payload) > limit
    if serializer == Serializer.JSON:
        try:
            return [shown.decode("utf-8") + (" ..." if more else "")]
        except UnicodeDecodeError:
            pass
    return ["hex: " + shown.hex(" ") + (" ..." if more else "")]


def _enum_label(enum_type: Any, value: int) -> str:
    try:
        return f"{enum_type(value).name} (0x{value:02X})"
    except ValueError:
        return f"UNKNOWN (0x{value:02X})"


def format_raw_frame(raw: RawFrame, number: int = 1) -> str:
    """Human readable dump of one frame (used by ``lcp inspect``)."""
    total = HEADER_SIZE + len(raw.metadata) + len(raw.payload)
    lines = [
        f"Frame #{number}  ({_enum_label(MessageType, raw.msg_type)}, {total} bytes)",
        f"  magic           0x{MAGIC:04X} ('LC')",
        "  version         1",
        f"  message type    {_enum_label(MessageType, raw.msg_type)}",
        f"  serializer      {_enum_label(Serializer, raw.serializer)}",
        f"  status          {status_name(raw.status)} (0x{raw.status:02X})",
        f"  correlation id  {raw.correlation_id}",
        f"  metadata        {len(raw.metadata)} bytes",
    ]
    if raw.metadata:
        try:
            meta = Metadata.decode(raw.metadata)
        except MetadataError as exc:
            lines.append(f"    !! invalid metadata: {exc}")
        else:
            for tag, value in (
                (Tag.AUTH_TOKEN, meta.auth_token),
                (Tag.TARGET_PATH, meta.target_path),
                (Tag.CLIENT_ID, meta.client_id),
            ):
                if value is not None:
                    shown = "<redacted>" if tag == Tag.AUTH_TOKEN else value
                    lines.append(f"    {tag.name:<12}{shown}")
            if meta.trace_id is not None:
                lines.append(f"    {Tag.TRACE_ID.name:<12}{meta.trace_id.hex()}")
            lines.extend(f"    0x{t:02X} (unknown) {v.hex()}" for t, v in meta.extra)
    lines.append(f"  payload         {len(raw.payload)} bytes")
    lines.extend(f"    {line}" for line in _payload_lines(raw.serializer, raw.payload))
    return "\n".join(lines)


def cmd_inspect(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.hex in (None, "-") else args.hex
    decoder = FrameDecoder()
    decoder.feed(parse_hex(text))
    count = 0
    try:
        while (raw := decoder.next_raw()) is not None:
            count += 1
            print(format_raw_frame(raw, count))
    except ProtocolError as exc:
        print(f"!! protocol violation: {exc}", file=sys.stderr)
        return 1
    if decoder.buffered:
        print(f"-- {decoder.buffered} trailing bytes (incomplete frame)")
    if count == 0 and not decoder.buffered:
        print("no data")
    return 0


def _body_from_args(args: argparse.Namespace) -> tuple[Any, int | None]:
    if getattr(args, "json", None) is not None:
        try:
            return json.loads(args.json), Serializer.JSON
        except ValueError as exc:
            raise ValueError(f"--json is not valid JSON: {exc}") from exc
    if getattr(args, "text", None) is not None:
        return args.text.encode("utf-8"), Serializer.RAW
    if getattr(args, "hex_payload", None) is not None:
        return parse_hex(args.hex_payload), _SERIALIZERS.get(args.serializer or "raw")
    return None, None


def cmd_encode(args: argparse.Namespace) -> int:
    body, serializer = _body_from_args(args)
    if args.serializer and body is not None:
        serializer = _SERIALIZERS[args.serializer]
    trace = bytes.fromhex(args.trace_id) if args.trace_id else None
    metadata = Metadata(
        auth_token=args.token, target_path=args.path, trace_id=trace, client_id=args.client_id
    )
    payload = b"" if body is None else body if isinstance(body, bytes) else JsonCodec().encode(body)
    frame = Frame(
        MessageType[args.type],
        args.cid,
        args.status,
        Serializer.JSON if serializer is None else serializer,
        metadata,
        payload,
    )
    print(frame.encode().hex(" "))
    return 0


# -- network commands ------------------------------------------------------------------------


def _client_ssl(args: argparse.Namespace) -> Any:
    if args.tls or args.ca or args.cert:
        return create_client_ssl_context(cafile=args.ca, certfile=args.cert, keyfile=args.key)
    return None


def cmd_serve(args: argparse.Namespace) -> int:
    async def run() -> None:
        ssl_context = None
        if args.tls_cert:
            ssl_context = create_server_ssl_context(
                args.tls_cert,
                args.tls_key,
                client_ca=args.client_ca,
                require_client_cert=args.require_client_cert,
            )
        authenticator = None
        if args.token:
            authenticator = StaticTokenAuthenticator(
                {token: f"token-{i}" for i, token in enumerate(args.token)}
            )
        server = LCPServer(args.host, args.port, ssl=ssl_context, authenticator=authenticator)

        @server.route("/v1/echo")
        async def echo(request: Request) -> Encoded:
            return Encoded(request.payload, request.serializer)

        @server.route("/v1/time")
        async def utc_time(request: Request) -> dict[str, str]:
            return {"time": datetime.now(timezone.utc).isoformat()}

        @server.on_event("*")
        async def sink(request: Request) -> None:
            print(f"event {request.path or '-'} ({len(request.payload)} bytes)", flush=True)

        await server.start()
        mode = "TLS" if ssl_context else "plain TCP"
        print(f"LCP server listening on {args.host}:{server.port} ({mode})", flush=True)
        try:
            await server.serve_forever()
        finally:
            await server.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


def cmd_request(args: argparse.Namespace) -> int:
    async def run() -> int:
        body, serializer = _body_from_args(args)
        client = LCPClient(
            args.host,
            args.port,
            ssl=_client_ssl(args),
            server_hostname=args.server_name,
            auth_token=args.token,
            client_id=args.client_id,
            config=SessionConfig(),
        )
        async with client:
            response = await client.request(
                args.path,
                body,
                serializer=serializer,
                timeout=args.timeout,
                trace_id=args.trace_id,
            )
        print(f"status: {status_name(response.status)} (0x{response.status:02X})")
        if response.payload:
            try:
                if response.serializer == Serializer.JSON:
                    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
                    return 0 if response.ok else 1
            except DecodeError:
                pass
            try:
                print(response.payload.decode("utf-8"))
            except UnicodeDecodeError:
                print("hex:", response.payload.hex(" "))
        return 0 if response.ok else 1

    return asyncio.run(run())


def cmd_ping(args: argparse.Namespace) -> int:
    async def run() -> int:
        async with LCPClient(
            args.host, args.port, ssl=_client_ssl(args), server_hostname=args.server_name
        ) as client:
            for _ in range(args.count):
                rtt = await client.ping(timeout=args.timeout)
                print(f"PONG from {args.host}:{args.port}  time={rtt * 1000:.2f} ms")
                if args.count > 1:
                    await asyncio.sleep(0.2)
        return 0

    return asyncio.run(run())


# -- argument parsing ------------------------------------------------------------------------


def _add_endpoint(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)


def _add_client_options(parser: argparse.ArgumentParser) -> None:
    _add_endpoint(parser)
    parser.add_argument("--tls", action="store_true", help="use TLS 1.3 with the system CAs")
    parser.add_argument("--ca", help="CA bundle (PEM) to verify the server; implies --tls")
    parser.add_argument("--cert", help="client certificate (PEM) for mutual TLS; implies --tls")
    parser.add_argument("--key", help="private key of --cert (if not in the same file)")
    parser.add_argument("--server-name", help="expected server name (default: --host)")
    parser.add_argument("--timeout", type=float, default=10.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lcp", description="LCP protocol toolkit")
    parser.add_argument(
        "--version", action="version", version=f"lcp {__version__} (spec {SPEC_VERSION})"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run a demo server (/v1/echo, /v1/time, event sink)")
    _add_endpoint(serve)
    serve.add_argument("--token", action="append", default=[], help="require this AUTH_TOKEN")
    serve.add_argument("--tls-cert", help="server certificate (PEM, may include the key)")
    serve.add_argument("--tls-key", help="private key (if not in --tls-cert)")
    serve.add_argument("--client-ca", help="CA used to verify client certificates")
    serve.add_argument("--require-client-cert", action="store_true", help="mutual TLS")
    serve.set_defaults(func=cmd_serve)

    request = sub.add_parser("request", help="send one REQ and print the reply")
    request.add_argument("path", help="TARGET_PATH, e.g. /v1/echo")
    _add_client_options(request)
    request.add_argument("--token", help="AUTH_TOKEN")
    request.add_argument("--client-id")
    request.add_argument("--trace-id", help="TRACE_ID (text)")
    body = request.add_mutually_exclusive_group()
    body.add_argument("--json", help="JSON body")
    body.add_argument("--text", help="raw UTF-8 body")
    body.add_argument("--hex-payload", help="raw body as hex")
    request.add_argument("--serializer", choices=sorted(_SERIALIZERS), help="for --hex-payload")
    request.set_defaults(func=cmd_request)

    ping = sub.add_parser("ping", help="PING/PONG round trip")
    _add_client_options(ping)
    ping.add_argument("-c", "--count", type=int, default=1)
    ping.set_defaults(func=cmd_ping)

    inspect_ = sub.add_parser("inspect", help="decode frames from hex (argument or stdin)")
    inspect_.add_argument("hex", nargs="?", help="hex bytes, or '-' / omitted to read stdin")
    inspect_.set_defaults(func=cmd_inspect)

    encode = sub.add_parser("encode", help="build a frame and print its hex")
    encode.add_argument("type", type=str.upper, choices=[m.name for m in MessageType])
    encode.add_argument("--cid", type=int, default=1, help="correlation id")
    encode.add_argument("--status", type=lambda s: int(s, 0), default=int(Status.OK))
    encode.add_argument("--path")
    encode.add_argument("--token")
    encode.add_argument("--client-id")
    encode.add_argument("--trace-id", help="TRACE_ID as hex")
    payload = encode.add_mutually_exclusive_group()
    payload.add_argument("--json", help="JSON payload")
    payload.add_argument("--text", help="raw UTF-8 payload")
    payload.add_argument("--hex-payload", help="raw payload as hex")
    encode.add_argument("--serializer", choices=sorted(_SERIALIZERS))
    encode.set_defaults(func=cmd_encode)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except (LCPError, OSError, ValueError, asyncio.TimeoutError) as exc:
        print(f"lcp: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
