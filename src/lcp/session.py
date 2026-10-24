"""Protocol engine shared by both ends of an LCP connection.

A :class:`Session` owns one ``(StreamReader, StreamWriter)`` pair and implements

* framing via :class:`~lcp.framer.FrameDecoder`, including the spec-mandated connection
  aborts (bad magic → close, bad version → ``BAD_REQUEST`` then close, ...),
* request/response and event/ack correlation by *Correlation ID*,
* ``PING``/``PONG`` keep-alive,
* authentication of inbound ``REQ``/``EVT`` frames (``AUTH_TOKEN`` → ``UNAUTHORIZED``),
* application back-pressure (``BUSY``): per-connection token bucket, in-flight cap and a
  client-side "send gate" that honours ``retry_after_ms``,
* bounded write buffering, idle/frame/write/handler timeouts and graceful close.

The class is symmetric: a client session issues requests, a server session serves them,
and either side may push events.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import ssl
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from .auth import Authenticator, Principal
from .codec import encode_body
from .constants import MAX_CORRELATION_ID, MAX_PAYLOAD_LENGTH
from .enums import MessageType, Serializer, Status
from .errors import (
    ConnectionClosedError,
    EncodeError,
    LCPError,
    ProtocolError,
    RequestTimeoutError,
    StatusError,
)
from .frame import Frame
from .framer import FrameDecoder
from .message import Reply, Request, Response, error_body
from .metadata import EMPTY_METADATA, Metadata, TraceIdLike, normalize_trace_id
from .ratelimit import RateLimit, TokenBucket
from .router import Router

logger = logging.getLogger("lcp.session")

_T = TypeVar("_T")

READ_SIZE = 256 * 1024
"""Maximum bytes requested from the socket per read."""

READ_LIMIT = 256 * 1024
"""``limit`` passed to asyncio streams (reader flow-control threshold)."""

_IN_FLIGHT_RETRY_HINT = 0.05
_LINGER_MAX_BYTES = 1 << 20

if sys.version_info >= (3, 11):

    async def _wait_for(awaitable: Awaitable[_T], timeout: float) -> _T:
        async with asyncio.timeout(timeout):
            return await awaitable

else:

    async def _wait_for(awaitable: Awaitable[_T], timeout: float) -> _T:
        return await asyncio.wait_for(awaitable, timeout)


@dataclass(slots=True)
class SessionConfig:
    """Tunables of a :class:`Session`.  ``None`` disables the respective timeout."""

    max_payload: int = MAX_PAYLOAD_LENGTH
    """Largest accepted inbound payload; bigger frames are answered with
    ``PAYLOAD_TOO_LARGE`` and the connection is closed.  Never above 16 MiB."""

    idle_timeout: float | None = None
    """Close the connection when no bytes arrive for this long."""

    frame_timeout: float | None = 30.0
    """Close the connection if a started frame is not completed within this time
    (slow-loris protection)."""

    write_timeout: float | None = 30.0
    """Abort if the peer does not drain our output buffer for this long."""

    handler_timeout: float | None = 30.0
    """Maximum run time of one handler (``INTERNAL_ERROR`` when exceeded)."""

    request_timeout: float | None = 30.0
    """Default reply deadline of outbound requests, events and pings."""

    max_in_flight: int = 1024
    """Inbound requests/events admitted at once (running *and* queued); beyond this the
    peer gets ``BUSY`` with a ``retry_after_ms`` hint."""

    max_in_flight_bytes: int = 64 * 1024 * 1024
    """Payload bytes of admitted requests/events (memory bound against pipelining floods);
    a call is always admitted when nothing else is in flight."""

    max_concurrency: int = 128
    """Handlers running at the same time; admitted calls beyond this wait their turn."""

    rate_limit: RateLimit | None = None
    """Per-connection token bucket for inbound requests/events (``BUSY`` when empty)."""

    max_auth_failures: int = 3
    """Close the connection after this many consecutive ``UNAUTHORIZED`` replies
    (``1`` closes right after the first failure, as the spec allows)."""

    max_bad_requests: int = 16
    """Close the connection after this many malformed (non-fatal) frames."""

    keepalive_interval: float | None = None
    """Send ``PING`` this often; a missing ``PONG`` within ``keepalive_timeout`` aborts."""

    keepalive_timeout: float = 10.0

    busy_retries: int = 3
    """How often outbound calls are retried automatically after a ``BUSY`` reply."""

    event_dedupe_window: int = 1024
    """Number of recent inbound event ids remembered to suppress duplicate deliveries."""

    linger_timeout: float = 1.0
    """After a fatal protocol error: how long to keep reading (and discarding) so the
    error reply is not destroyed by a TCP reset."""

    auto_trace_id: bool = False
    """Attach a random 16-byte ``TRACE_ID`` to outbound calls that do not set one."""


@dataclass(slots=True)
class SessionStats:
    """Simple counters, mostly useful for tests and monitoring."""

    requests: int = 0
    events: int = 0
    busy_rejections: int = 0
    auth_failures: int = 0
    protocol_errors: int = 0


def _check_path(path: str) -> None:
    if not path.startswith("/") or not path.isprintable():
        raise EncodeError(f"TARGET_PATH must start with '/' and be printable: {path!r}")


class Session:
    """One LCP connection (see module docstring)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        config: SessionConfig | None = None,
        router: Router | None = None,
        events: Router | None = None,
        authenticator: Authenticator | None = None,
        auth_token: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self.config = config if config is not None else SessionConfig()
        self.router = router if router is not None else Router()
        self.events = events if events is not None else Router()
        self.client_id = client_id
        self.principal: Principal | None = None
        self.stats = SessionStats()
        self.peername: Any = writer.get_extra_info("peername")

        self._reader = reader
        self._writer = writer
        self._transport = writer.transport
        self._authenticator = authenticator
        self._auth_token = auth_token

        cfg = self.config
        self._decoder = FrameDecoder(max_payload=cfg.max_payload)
        self._pending: dict[int, tuple[asyncio.Future[Frame], MessageType]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._cid = 0
        self._in_flight = 0
        self._in_flight_bytes = 0
        self._slots = asyncio.Semaphore(max(1, cfg.max_concurrency))
        self._bucket = (
            TokenBucket(cfg.rate_limit.rate, cfg.rate_limit.burst) if cfg.rate_limit else None
        )
        self._auth_failures = 0
        self._bad_requests = 0
        self._pause_until = 0.0
        self._recent_events: OrderedDict[int, tuple[int, int, bytes]] = OrderedDict()
        self._events_in_progress: set[int] = set()
        self._write_lock = asyncio.Lock()
        self._high_water: int = self._transport.get_write_buffer_limits()[1]
        self._max_write_buffer = cfg.max_payload + (1 << 20)
        self._fatal: ProtocolError | None = None
        self._draining = False
        self._closing = False
        self._shutdown_started = False
        self._closed = asyncio.Event()

    def __str__(self) -> str:
        return f"<lcp.Session peer={self.peername}>"

    # -- public state ---------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """``True`` once the connection is fully closed."""
        return self._closed.is_set()

    @property
    def closing(self) -> bool:
        """``True`` once the connection is closed or on its way to being closed."""
        return self._closing

    @property
    def tls_version(self) -> str | None:
        """Negotiated TLS protocol version (``"TLSv1.3"``) or ``None`` for plain TCP."""
        ssl_object = self._writer.get_extra_info("ssl_object")
        return None if ssl_object is None else str(ssl_object.version())

    @property
    def peer_certificate(self) -> dict[str, Any] | None:
        """The verified peer certificate (mutual TLS), if any."""
        cert = self._writer.get_extra_info("peercert")
        return cert if cert else None

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # -- outbound API ---------------------------------------------------------------------

    async def request(
        self,
        path: str,
        body: Any = None,
        *,
        serializer: int | None = None,
        timeout: float | None = None,
        trace_id: TraceIdLike | None = None,
        busy_retries: int | None = None,
    ) -> Response:
        """Send a ``REQ`` and wait for the matching ``RES``.

        A ``BUSY`` reply pauses *all* outbound calls of this session for the server's
        ``retry_after`` hint and is retried up to ``busy_retries`` times; the final
        response is returned even if it is an error — use ``raise_for_status()``.
        """
        _check_path(path)
        chosen, payload = encode_body(body, serializer)
        metadata = self._outbound_metadata(path, trace_id)
        return await self._call(
            MessageType.REQ, MessageType.RES, metadata, chosen, payload, timeout, busy_retries, 0
        )

    async def send_event(
        self,
        path: str = "",
        body: Any = None,
        *,
        serializer: int | None = None,
        timeout: float | None = None,
        retries: int = 0,
        trace_id: TraceIdLike | None = None,
        busy_retries: int | None = None,
    ) -> Response:
        """Send an ``EVT`` and wait for its ``EVT_ACK`` (at-least-once delivery).

        ``retries`` resends the *same* correlation id after an ack timeout; the receiver
        suppresses duplicates of recently seen ids, so the handler normally runs once.
        The returned response carries the receiver's status (``BUSY`` means: back off).
        """
        if path:
            _check_path(path)
        chosen, payload = encode_body(body, serializer)
        metadata = self._outbound_metadata(path, trace_id)
        return await self._call(
            MessageType.EVT,
            MessageType.EVT_ACK,
            metadata,
            chosen,
            payload,
            timeout,
            busy_retries,
            retries,
        )

    async def ping(self, timeout: float | None = None) -> float:
        """Round-trip a ``PING``/``PONG``; returns the latency in seconds."""
        deadline = self.config.request_timeout if timeout is None else timeout
        started = time.perf_counter()
        await self._exchange(
            MessageType.PING, MessageType.PONG, EMPTY_METADATA, Serializer.JSON, b"", deadline
        )
        return time.perf_counter() - started

    # -- lifecycle ------------------------------------------------------------------------

    async def serve(self) -> None:
        """Read and process frames until the connection ends, then clean up."""
        try:
            if self.config.keepalive_interval:
                self._keepalive_task = asyncio.get_running_loop().create_task(
                    self._keepalive_loop()
                )
            await self._read_loop()
        finally:
            await self._shutdown()

    async def close(self, *, grace: float = 0.0) -> None:
        """Close the connection; with ``grace`` wait that long for running handlers first."""
        if not self._closing:
            self._draining = True
            current = asyncio.current_task()
            running = [t for t in self._tasks if t is not current and not t.done()]
            if grace > 0 and running:
                await asyncio.wait(running, timeout=grace)
            self._closing = True
            self._writer.close()
        try:
            await asyncio.wait_for(self._closed.wait(), 5.0)
        except asyncio.TimeoutError:
            await self._shutdown()

    # -- read side ------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        cfg = self.config
        loop = asyncio.get_running_loop()
        partial_since: float | None = None
        while not self._closing:
            timeout = cfg.idle_timeout
            if partial_since is not None and cfg.frame_timeout is not None:
                remaining = partial_since + cfg.frame_timeout - loop.time()
                timeout = remaining if timeout is None else min(timeout, remaining)
                if timeout <= 0:
                    logger.warning("%s: frame timeout (incomplete frame)", self)
                    return
            try:
                if timeout is None:
                    data = await self._reader.read(READ_SIZE)
                else:
                    data = await asyncio.wait_for(self._reader.read(READ_SIZE), timeout)
            except asyncio.TimeoutError:
                what = "frame" if partial_since is not None else "idle"
                logger.info("%s: %s timeout, closing", self, what)
                return
            except (OSError, ssl.SSLError) as exc:
                logger.debug("%s: connection error while reading: %r", self, exc)
                return
            if not data:
                return
            self._decoder.feed(data)
            progressed = self._pump()
            if self._fatal is not None:
                await self._handle_fatal(self._fatal)
                return
            if self._decoder.buffered == 0:
                partial_since = None
            elif progressed or partial_since is None:
                partial_since = loop.time()

    def _pump(self) -> bool:
        """Decode and dispatch everything buffered; ``True`` if at least one frame was seen."""
        progressed = False
        while not self._closing:
            try:
                frame = self._decoder.next_frame()
            except ProtocolError as err:
                if err.fatal:
                    self._fatal = err
                    return progressed
                progressed = True
                self._on_frame_error(err)
                if self._bad_requests > self.config.max_bad_requests:
                    self._fatal = ProtocolError("too many protocol errors", fatal=True, reply=False)
                    return progressed
                continue
            if frame is None:
                return progressed
            progressed = True
            self._dispatch(frame)
        return progressed

    def _dispatch(self, frame: Frame) -> None:
        msg_type = frame.msg_type
        if msg_type is MessageType.REQ or msg_type is MessageType.EVT:
            self._on_inbound_call(frame)
        elif msg_type is MessageType.PING:
            self._write_nowait(Frame.pong(frame.correlation_id))
        else:  # RES, EVT_ACK, PONG
            self._resolve(frame)

    # -- inbound REQ / EVT ------------------------------------------------------------------

    def _on_inbound_call(self, frame: Frame) -> None:
        is_event = frame.msg_type is MessageType.EVT
        cid = frame.correlation_id
        if self._draining:
            self._reject(frame, "shutting down", None)
            return
        if is_event:
            cached = self._recent_events.get(cid)
            if cached is not None:  # a retransmission of an event we already handled
                self._recent_events.move_to_end(cid)
                self._write_nowait(self._reply_frame(frame, *cached))
                return
            if cid in self._events_in_progress:  # still running; its ack will follow
                return
        retry_after = self._overload_retry_after(frame)
        if retry_after is not None:
            self._reject(frame, "server busy", retry_after)
            return
        self._in_flight += 1
        self._in_flight_bytes += len(frame.payload)
        if is_event:
            self.stats.events += 1
            self._events_in_progress.add(cid)
        else:
            self.stats.requests += 1
        self._spawn(self._handle_call(frame))

    def _overload_retry_after(self, frame: Frame) -> float | None:
        """Seconds the peer should back off, or ``None`` if the call can be admitted."""
        if self._bucket is not None:
            wait = self._bucket.try_acquire()
            if wait > 0:
                return wait
        cfg = self.config
        if self._in_flight >= cfg.max_in_flight:
            return _IN_FLIGHT_RETRY_HINT
        if self._in_flight and self._in_flight_bytes + len(frame.payload) > cfg.max_in_flight_bytes:
            return _IN_FLIGHT_RETRY_HINT
        return None

    def _reject(self, request: Frame, message: str, retry_after: float | None) -> None:
        self.stats.busy_rejections += 1
        body = error_body(Status.BUSY, message, retry_after=retry_after)
        self._write_nowait(self._reply_frame(request, Status.BUSY, Serializer.JSON, body))

    async def _handle_call(self, request: Frame) -> None:
        is_event = request.msg_type is MessageType.EVT
        cid = request.correlation_id
        try:
            async with self._slots:  # admitted calls wait here when max_concurrency is reached
                status, serializer, payload = await self._run_handler(request, is_event)
            try:
                reply = self._reply_frame(request, status, serializer, payload)
            except EncodeError as exc:
                logger.error("%s: reply to %r cannot be encoded: %s", self, request, exc)
                status, serializer = int(Status.INTERNAL_ERROR), int(Serializer.JSON)
                payload = error_body(status, "reply cannot be encoded")
                reply = self._reply_frame(request, status, serializer, payload)
            if is_event and status != Status.BUSY:
                self._remember_event(cid, status, serializer, payload)
            await self._send(reply)
            if (
                status == Status.UNAUTHORIZED
                and self._auth_failures >= self.config.max_auth_failures
            ):
                self._close_soon("too many authentication failures")
        except ConnectionClosedError:
            pass
        finally:
            self._in_flight -= 1
            self._in_flight_bytes -= len(request.payload)
            if is_event:
                self._events_in_progress.discard(cid)

    async def _run_handler(self, frame: Frame, is_event: bool) -> tuple[int, int, bytes]:
        """Authenticate, route and run the handler; returns ``(status, serializer, payload)``."""
        try:
            principal = await self._authenticate(frame)
            path = frame.metadata.target_path
            if path is None:
                if not is_event:
                    raise StatusError(Status.BAD_REQUEST, "REQ requires TARGET_PATH metadata")
                path = ""
            elif not path.startswith("/") or not path.isprintable():
                raise StatusError(Status.BAD_REQUEST, "invalid TARGET_PATH")
            handler = (self.events if is_event else self.router).resolve(path)
            if handler is None:
                kind = "event handler" if is_event else "route"
                raise StatusError(Status.NOT_FOUND, f"no {kind} for {path!r}")
            result = handler(Request(frame, self, principal))
            if inspect.isawaitable(result):
                result = await self._with_handler_timeout(result)
            return self._encode_result(result, frame)
        except StatusError as exc:
            return self._error_result(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s: unhandled error while handling %r", self, frame)
            return self._error_result(StatusError(Status.INTERNAL_ERROR, "internal error"))

    async def _with_handler_timeout(self, awaitable: Awaitable[_T]) -> _T:
        timeout = self.config.handler_timeout
        if timeout is None:
            return await awaitable
        try:
            return await _wait_for(awaitable, timeout)
        except asyncio.TimeoutError:
            raise StatusError(Status.INTERNAL_ERROR, "handler timed out") from None

    @staticmethod
    def _encode_result(result: Any, request: Frame) -> tuple[int, int, bytes]:
        status = int(Status.OK)
        serializer: int | None = None
        body = result
        if isinstance(result, Reply):
            status, serializer, body = int(result.status), result.serializer, result.body
        if body is None:
            return status, int(request.serializer if serializer is None else serializer), b""
        chosen, payload = encode_body(body, serializer)
        return status, chosen, payload

    @staticmethod
    def _error_result(exc: StatusError) -> tuple[int, int, bytes]:
        body = error_body(exc.status, exc.message, details=exc.details, retry_after=exc.retry_after)
        return exc.status, int(Serializer.JSON), body

    async def _authenticate(self, frame: Frame) -> Principal | None:
        authenticator = self._authenticator
        if authenticator is None:
            return None
        token = frame.metadata.auth_token
        if not token:
            raise self._auth_failed("missing AUTH_TOKEN")
        try:
            result = authenticator(token)
            if inspect.isawaitable(result):
                result = await result
        except StatusError:
            raise
        except Exception:
            logger.exception("%s: authenticator raised", self)
            raise StatusError(Status.INTERNAL_ERROR, "authentication backend error") from None
        if not result:
            raise self._auth_failed("invalid AUTH_TOKEN")
        principal = result if isinstance(result, Principal) else Principal("anonymous")
        self._auth_failures = 0
        self.principal = principal
        return principal

    def _auth_failed(self, message: str) -> StatusError:
        self._auth_failures += 1
        self.stats.auth_failures += 1
        return StatusError(Status.UNAUTHORIZED, message)

    def _remember_event(self, cid: int, status: int, serializer: int, payload: bytes) -> None:
        window = self.config.event_dedupe_window
        if window <= 0:
            return
        self._recent_events[cid] = (status, serializer, payload)
        self._recent_events.move_to_end(cid)
        while len(self._recent_events) > window:
            self._recent_events.popitem(last=False)

    @staticmethod
    def _reply_frame(request: Frame, status: int, serializer: int, payload: bytes) -> Frame:
        trace = request.metadata.trace_id
        metadata = Metadata(trace_id=trace) if trace is not None else EMPTY_METADATA
        msg_type = MessageType.EVT_ACK if request.msg_type is MessageType.EVT else MessageType.RES
        return Frame(msg_type, request.correlation_id, status, serializer, metadata, payload)

    @staticmethod
    def _error_frame(err: ProtocolError) -> Frame:
        msg_type = MessageType.EVT_ACK if err.msg_type == MessageType.EVT else MessageType.RES
        return Frame(
            msg_type,
            err.correlation_id or 0,
            err.status,
            Serializer.JSON,
            EMPTY_METADATA,
            error_body(err.status, str(err)),
        )

    # -- protocol errors --------------------------------------------------------------------

    def _on_frame_error(self, err: ProtocolError) -> None:
        self.stats.protocol_errors += 1
        self._bad_requests += 1
        logger.warning("%s: %s", self, err)
        if err.msg_type in (MessageType.REQ, MessageType.EVT):
            self._write_nowait(self._error_frame(err))
        elif err.correlation_id is not None:
            self._fail_pending(err.correlation_id, err)

    async def _handle_fatal(self, err: ProtocolError) -> None:
        self.stats.protocol_errors += 1
        logger.warning("%s: fatal protocol error: %s", self, err)
        if err.reply:
            self._write_nowait(self._error_frame(err))
            await self._linger()

    async def _linger(self) -> None:
        """Flush the error reply, then swallow trailing bytes for a moment so that closing
        does not turn into a TCP reset that would destroy the reply."""
        timeout = self.config.linger_timeout
        if timeout <= 0 or self._transport.is_closing():
            return
        try:
            if self._writer.can_write_eof():
                self._writer.write_eof()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            discarded = 0
            while discarded < _LINGER_MAX_BYTES:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                chunk = await asyncio.wait_for(self._reader.read(65536), remaining)
                if not chunk:
                    break
                discarded += len(chunk)
        except (asyncio.TimeoutError, OSError):
            pass

    # -- correlation ------------------------------------------------------------------------

    def _alloc_cid(self) -> int:
        for _ in range(64):  # a collision needs a wrap-around plus a very old pending call
            self._cid = self._cid % MAX_CORRELATION_ID + 1
            if self._cid not in self._pending:
                return self._cid
        raise LCPError("no free correlation id")

    def _resolve(self, frame: Frame) -> None:
        cid = frame.correlation_id
        entry = self._pending.pop(cid, None)
        if entry is None:
            logger.debug("%s: dropping unsolicited or late %s cid=%d", self, frame.msg_type, cid)
            return
        future, expected = entry
        if future.done():
            return
        if frame.msg_type is not expected:
            future.set_exception(
                ProtocolError(f"expected {expected.name} for cid={cid}, got {frame.msg_type.name}")
            )
        else:
            future.set_result(frame)

    def _fail_pending(self, cid: int, error: Exception) -> None:
        entry = self._pending.pop(cid, None)
        if entry is not None and not entry[0].done():
            entry[0].set_exception(error)

    async def _exchange(
        self,
        msg_type: MessageType,
        expected: MessageType,
        metadata: Metadata,
        serializer: int,
        payload: bytes,
        timeout: float | None,
        *,
        cid: int | None = None,
    ) -> Frame:
        if self._closing:
            raise ConnectionClosedError("connection closed")
        if cid is None:
            cid = self._alloc_cid()
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[cid] = (future, expected)
        try:
            await self._send(Frame(msg_type, cid, Status.OK, serializer, metadata, payload))
            if timeout is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise RequestTimeoutError(
                    f"no {expected.name} within {timeout}s (cid={cid})"
                ) from None
        finally:
            self._pending.pop(cid, None)

    def _outbound_metadata(self, path: str, trace_id: TraceIdLike | None) -> Metadata:
        if trace_id is not None:
            tid: bytes | None = normalize_trace_id(trace_id)
        elif self.config.auto_trace_id:
            tid = os.urandom(16)
        else:
            tid = None
        return Metadata(
            auth_token=self._auth_token,
            target_path=path or None,
            trace_id=tid,
            client_id=self.client_id,
        )

    async def _call(
        self,
        msg_type: MessageType,
        expected: MessageType,
        metadata: Metadata,
        serializer: int,
        payload: bytes,
        timeout: float | None,
        busy_retries: int | None,
        timeout_retries: int,
    ) -> Response:
        cfg = self.config
        deadline = cfg.request_timeout if timeout is None else timeout
        max_busy = cfg.busy_retries if busy_retries is None else busy_retries
        loop = asyncio.get_running_loop()
        busy_attempt = 0
        while True:
            await self._wait_gate()
            cid = self._alloc_cid()
            attempt = 0
            while True:
                try:
                    frame = await self._exchange(
                        msg_type, expected, metadata, serializer, payload, deadline, cid=cid
                    )
                    break
                except RequestTimeoutError:
                    if attempt >= timeout_retries:
                        raise
                    attempt += 1  # resend with the same id so the receiver can deduplicate
            response = Response(frame)
            if response.status != Status.BUSY or busy_attempt >= max_busy:
                return response
            delay = response.retry_after
            if delay is None:
                delay = min(0.05 * 2**busy_attempt, 2.0)
            delay *= random.uniform(1.0, 1.25)  # jitter: avoid synchronized retries
            self._pause_until = max(self._pause_until, loop.time() + delay)
            busy_attempt += 1

    async def _wait_gate(self) -> None:
        """Honour a ``BUSY`` back-off: no outbound call starts before ``_pause_until``."""
        loop = asyncio.get_running_loop()
        while True:
            remaining = self._pause_until - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    # -- write side -------------------------------------------------------------------------

    async def _send(self, frame: Frame) -> None:
        """Write a frame, waiting (bounded by ``write_timeout``) while the peer is slow."""
        parts = frame.encode_parts()
        async with self._write_lock:  # one writer at a time bounds the buffered bytes
            self._ensure_open()
            if self._transport.get_write_buffer_size() > self._high_water:
                try:
                    await asyncio.wait_for(self._writer.drain(), self.config.write_timeout)
                except asyncio.TimeoutError:
                    self._abort("write timeout: peer is not reading")
                    raise ConnectionClosedError("write timeout") from None
                except OSError as exc:
                    raise ConnectionClosedError(str(exc)) from exc
                self._ensure_open()
            self._transport.writelines(parts)
            self._check_write_buffer()

    def _write_nowait(self, frame: Frame) -> None:
        """Write a small control frame (PONG, BUSY, error) without waiting."""
        if self._closing or self._transport.is_closing():
            return
        self._transport.writelines(frame.encode_parts())
        self._check_write_buffer()

    def _ensure_open(self) -> None:
        if self._closing or self._transport.is_closing():
            raise ConnectionClosedError("connection closed")

    def _check_write_buffer(self) -> None:
        if self._transport.get_write_buffer_size() > self._max_write_buffer:
            self._abort("output buffer overflow: peer is not reading")

    def _abort(self, reason: str) -> None:
        if not self._closing:
            logger.warning("%s: aborting connection: %s", self, reason)
        self._closing = True
        self._transport.abort()

    def _close_soon(self, reason: str) -> None:
        logger.info("%s: closing connection: %s", self, reason)
        self._closing = True
        self._writer.close()  # buffered replies are flushed first

    # -- tasks & shutdown -------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("%s: task failed", self, exc_info=task.exception())

    async def _keepalive_loop(self) -> None:
        cfg = self.config
        assert cfg.keepalive_interval is not None
        while True:
            await asyncio.sleep(cfg.keepalive_interval)
            try:
                await self.ping(timeout=cfg.keepalive_timeout)
            except (RequestTimeoutError, ConnectionClosedError):
                self._abort("keep-alive failed (no PONG)")
                return

    async def _shutdown(self) -> None:
        if self._shutdown_started:
            await self._closed.wait()
            return
        self._shutdown_started = True
        self._closing = True
        for future, _ in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionClosedError("connection closed"))
        self._pending.clear()
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        if self._keepalive_task is not None and self._keepalive_task is not current:
            tasks.append(self._keepalive_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        except (OSError, asyncio.TimeoutError):
            self._transport.abort()
        self._closed.set()
