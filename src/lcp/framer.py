"""Incremental, sans-IO frame decoder (spec section 6, "Strict Framer").

Two layers are exposed:

* :meth:`FrameDecoder.next_raw` performs exactly the header validation of the spec's
  reference framer and returns raw bytes for metadata and payload.
* :meth:`FrameDecoder.next_frame` additionally validates the *Serializer* value and
  parses the TLV metadata, returning a :class:`~lcp.frame.Frame`.

Errors are reported as :class:`~lcp.errors.ProtocolError`.  ``fatal`` errors poison the
decoder (the stream is out of sync or hostile); non-fatal errors concern a single frame
that has already been consumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any, Final, NamedTuple, NoReturn

from .constants import HEADER_SIZE, HEADER_STRUCT, MAGIC, MAX_PAYLOAD_LENGTH, PROTOCOL_VERSION
from .enums import MessageType, Serializer, Status
from .errors import MetadataError, ProtocolError
from .frame import Frame
from .metadata import EMPTY_METADATA, Metadata

_MAGIC_HI: Final = MAGIC >> 8
_MAGIC_LO: Final = MAGIC & 0xFF
_KEEPALIVE_TYPES: Final = frozenset({MessageType.PING, MessageType.PONG})
_PAYLOAD_TYPES: Final = frozenset(
    {MessageType.REQ, MessageType.RES, MessageType.EVT, MessageType.EVT_ACK}
)
_COMPACT_THRESHOLD: Final = 1 << 16
_MESSAGE_TYPES: Final = {int(member): member for member in MessageType}
_STATUSES: Final = {int(member): member for member in Status}
_SERIALIZER_IDS: Final = frozenset(int(member) for member in Serializer)
_METADATA_CACHE_MAX_BYTES: Final = 512


@lru_cache(maxsize=512)
def _decode_metadata_cached(data: bytes) -> Metadata:
    return Metadata.decode(data)


def _decode_metadata(data: bytes) -> Metadata:
    """Decode a TLV block; short blocks (typically identical per route/token) are memoized.

    ``Metadata`` is immutable, so sharing instances is safe.  Failures are never cached.
    """
    if not data:
        return EMPTY_METADATA
    if len(data) <= _METADATA_CACHE_MAX_BYTES:
        return _decode_metadata_cached(data)
    return Metadata.decode(data)


class RawFrame(NamedTuple):
    """Header fields plus untouched metadata/payload bytes."""

    msg_type: int
    serializer: int
    status: int
    correlation_id: int
    metadata: bytes
    payload: bytes


class FrameDecoder:
    """Turns an arbitrary chunking of the byte stream into frames.

    Header checks run as soon as 16 bytes are buffered, so oversized or invalid frames
    are rejected *before* their body is received (DoS protection).  The magic number is
    checked as early as the first two bytes, which rejects foreign protocols (e.g. HTTP)
    immediately.
    """

    __slots__ = ("_buf", "_error", "_max_payload", "_pos")

    def __init__(self, *, max_payload: int = MAX_PAYLOAD_LENGTH) -> None:
        if not 0 <= max_payload <= MAX_PAYLOAD_LENGTH:
            raise ValueError(f"max_payload must be within 0..{MAX_PAYLOAD_LENGTH}")
        self._buf = bytearray()
        self._pos = 0
        self._max_payload = max_payload
        self._error: ProtocolError | None = None

    @property
    def buffered(self) -> int:
        """Number of received bytes not yet consumed by a complete frame."""
        return len(self._buf) - self._pos

    def feed(self, data: bytes | bytearray | memoryview) -> None:
        """Append received bytes; raises the stored error after a fatal violation."""
        if self._error is not None:
            raise self._error
        self._buf += data

    # -- layer 1: header framing ---------------------------------------------------------

    def next_raw(self) -> RawFrame | None:
        """Extract the next complete frame without interpreting metadata or serializer."""
        if self._error is not None:
            raise self._error
        buf = self._buf
        pos = self._pos
        avail = len(buf) - pos
        if avail < 2:
            return None
        if buf[pos] != _MAGIC_HI or buf[pos + 1] != _MAGIC_LO:
            magic = (buf[pos] << 8) | buf[pos + 1]
            self._fail(
                f"invalid magic number 0x{magic:04X} (expected 0x{MAGIC:04X}); stream corrupted",
                reply=False,
            )
        if avail < HEADER_SIZE:
            return None

        _, version, msg_type, serializer, status, meta_len, cid, payload_len = (
            HEADER_STRUCT.unpack_from(buf, pos)
        )
        if version != PROTOCOL_VERSION:
            self._fail(
                f"unsupported protocol version {version}", correlation_id=cid, msg_type=msg_type
            )
        if not MessageType.REQ <= msg_type <= MessageType.PONG:
            self._fail(f"invalid message type 0x{msg_type:02X}", correlation_id=cid)
        if msg_type in _KEEPALIVE_TYPES and (meta_len != 0 or payload_len != 0):
            self._fail(
                "PING/PONG must have 0 metadata length and 0 payload length "
                f"(got {meta_len}, {payload_len})",
                correlation_id=cid,
                msg_type=msg_type,
            )
        if payload_len > self._max_payload:
            self._fail(
                f"payload length {payload_len} exceeds the limit of {self._max_payload} bytes",
                status=Status.PAYLOAD_TOO_LARGE,
                correlation_id=cid,
                msg_type=msg_type,
            )

        total = HEADER_SIZE + meta_len + payload_len
        if avail < total:
            return None
        meta_start = pos + HEADER_SIZE
        meta_end = meta_start + meta_len
        metadata = bytes(buf[meta_start:meta_end])
        payload = bytes(buf[meta_end : pos + total])
        self._consume(total)
        return RawFrame(msg_type, serializer, status, cid, metadata, payload)

    # -- layer 2: semantic frames --------------------------------------------------------

    def next_frame(self) -> Frame | None:
        """Extract the next frame with parsed metadata, or ``None`` if more bytes are needed."""
        raw = self.next_raw()
        if raw is None:
            return None
        msg_type = _MESSAGE_TYPES[raw.msg_type]  # next_raw() already range-checked it
        if msg_type in _PAYLOAD_TYPES and raw.serializer not in _SERIALIZER_IDS:
            raise ProtocolError(
                f"unknown serializer 0x{raw.serializer:02X}",
                correlation_id=raw.correlation_id,
                msg_type=raw.msg_type,
            )
        try:
            metadata = _decode_metadata(raw.metadata)
        except MetadataError as exc:
            raise ProtocolError(
                f"invalid metadata: {exc}",
                correlation_id=raw.correlation_id,
                msg_type=raw.msg_type,
            ) from exc
        return Frame._trusted(
            msg_type,
            raw.correlation_id,
            _STATUSES.get(raw.status, raw.status),
            raw.serializer,
            metadata,
            raw.payload,
        )

    def __iter__(self) -> Iterator[Frame]:
        """Yield every complete frame currently buffered."""
        while (frame := self.next_frame()) is not None:
            yield frame

    # -- internals -----------------------------------------------------------------------

    def _consume(self, count: int) -> None:
        self._pos += count
        if self._pos == len(self._buf):
            self._buf.clear()
            self._pos = 0
        elif self._pos >= _COMPACT_THRESHOLD and self._pos * 2 >= len(self._buf):
            del self._buf[: self._pos]
            self._pos = 0

    def _fail(
        self,
        reason: str,
        *,
        status: int = Status.BAD_REQUEST,
        reply: bool = True,
        correlation_id: int | None = None,
        msg_type: int | None = None,
    ) -> NoReturn:
        error = ProtocolError(
            reason,
            status=status,
            fatal=True,
            reply=reply,
            correlation_id=correlation_id,
            msg_type=msg_type,
        )
        self._error = error
        self._buf.clear()
        self._pos = 0
        raise error


class StrictLCPFramer:
    """Compatibility wrapper mirroring the reference class of spec section 6.

    ``feed()`` / ``parse_frame(sock)`` behave like the reference: any violation closes
    ``sock`` (if given) and raises :class:`ValueError`.  ``parse_frame`` returns the same
    dictionary, with metadata and payload as raw ``bytes``.  New code should prefer
    :class:`FrameDecoder`.
    """

    def __init__(self, *, max_payload: int = MAX_PAYLOAD_LENGTH) -> None:
        self._max_payload = max_payload
        self._decoder = FrameDecoder(max_payload=max_payload)

    def feed(self, data: bytes) -> None:
        self._decoder.feed(data)

    def parse_frame(self, sock: Any = None) -> dict[str, Any] | None:
        try:
            raw = self._decoder.next_raw()
        except ProtocolError as exc:
            self._abort(sock, str(exc))
        if raw is None:
            return None
        return {
            "version": PROTOCOL_VERSION,
            "msg_type": raw.msg_type,
            "serializer": raw.serializer,
            "status": raw.status,
            "corr_id": raw.correlation_id,
            "metadata": raw.metadata,
            "payload": raw.payload,
        }

    def _abort(self, sock: Any, reason: str) -> NoReturn:
        self._decoder = FrameDecoder(max_payload=self._max_payload)  # buffer cleared, like the spec
        if sock:
            sock.close()
        raise ValueError(f"[LCP Protocol Error] {reason}")
