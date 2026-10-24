"""TLV metadata block (spec section 4).

Layout of one entry: ``Tag (1 byte) | Length (2 bytes, big-endian) | Value (N bytes)``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from .constants import MAX_METADATA_LENGTH, TLV_HEADER_SIZE, TLV_STRUCT
from .enums import Tag
from .errors import EncodeError, MetadataError

_MAX_TLV_VALUE: Final = 0xFFFF
_KNOWN_TAGS: Final = frozenset(int(tag) for tag in Tag)
TRACE_ID_RAW_LENGTH: Final = 16

TraceIdLike = bytes | str | uuid.UUID
"""Anything accepted where a trace id is expected (see :func:`normalize_trace_id`)."""


def normalize_trace_id(value: TraceIdLike) -> bytes:
    """Convert a trace id to wire bytes.

    The spec describes ``TRACE_ID`` as "16 Bytes / String".  Both forms are accepted:
    a :class:`uuid.UUID` becomes its 16 raw bytes, ``str`` is UTF-8 encoded and
    ``bytes`` are used verbatim.
    """
    if isinstance(value, uuid.UUID):
        return value.bytes
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if not data:
        raise EncodeError("TRACE_ID must not be empty")
    return data


def _put(out: bytearray, tag: int, value: bytes) -> None:
    if len(value) > _MAX_TLV_VALUE:
        raise EncodeError(f"TLV 0x{tag:02X} value too long: {len(value)} > {_MAX_TLV_VALUE}")
    out += TLV_STRUCT.pack(tag, len(value))
    out += value


def _utf8(tag: int, value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetadataError(f"TLV 0x{tag:02X} is not valid UTF-8") from exc


def _decode_known(tag: int, value: bytes) -> Any:
    if tag == Tag.TRACE_ID:
        if not value:
            raise MetadataError("TRACE_ID must not be empty")
        if len(value) != TRACE_ID_RAW_LENGTH:
            _utf8(tag, value)  # anything but 16 raw bytes must be a UTF-8 string
        return value
    return _utf8(tag, value)


@dataclass(frozen=True, slots=True, repr=False)
class Metadata:
    """Decoded metadata of one frame.

    Unknown tags are preserved (in wire order) in :attr:`extra` so that peers speaking
    a newer revision of the protocol stay interoperable.
    """

    auth_token: str | None = None
    target_path: str | None = None
    trace_id: bytes | None = None
    client_id: str | None = None
    extra: tuple[tuple[int, bytes], ...] = ()

    def __repr__(self) -> str:
        # Frames are logged with %r; an authentication token must never end up in a log file.
        token = None if self.auth_token is None else "<redacted>"
        return (
            f"Metadata(auth_token={token!r}, target_path={self.target_path!r}, "
            f"trace_id={self.trace_id!r}, client_id={self.client_id!r}, extra={self.extra!r})"
        )

    def encode(self) -> bytes:
        """Serialize to the TLV wire format (canonical order: tags 1..4, then extras)."""
        out = bytearray()
        try:
            if self.auth_token is not None:
                _put(out, Tag.AUTH_TOKEN, self.auth_token.encode("utf-8"))
            if self.target_path is not None:
                _put(out, Tag.TARGET_PATH, self.target_path.encode("utf-8"))
            if self.trace_id is not None:
                _put(out, Tag.TRACE_ID, self.trace_id)
            if self.client_id is not None:
                _put(out, Tag.CLIENT_ID, self.client_id.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise EncodeError(f"metadata string is not encodable as UTF-8: {exc}") from exc
        for tag, value in self.extra:
            if not 0 <= tag <= 0xFF:
                raise EncodeError(f"TLV tag out of range: {tag}")
            if tag in _KNOWN_TAGS:
                raise EncodeError(f"standard tag 0x{tag:02X} must use its dedicated field")
            _put(out, tag, bytes(value))
        if len(out) > MAX_METADATA_LENGTH:
            raise EncodeError(f"metadata too large: {len(out)} > {MAX_METADATA_LENGTH} bytes")
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes | bytearray | memoryview) -> Metadata:
        """Parse a TLV block; raises :class:`MetadataError` if it is malformed.

        Strictness: truncated or overrunning entries, invalid UTF-8 in string tags and
        duplicated *standard* tags are rejected.  Unknown tags are kept.
        """
        view = memoryview(data)
        size = len(view)
        if size > MAX_METADATA_LENGTH:
            raise MetadataError(f"metadata too large: {size} > {MAX_METADATA_LENGTH} bytes")
        offset = 0
        fields: dict[int, Any] = {}
        extra: list[tuple[int, bytes]] = []
        while offset < size:
            if size - offset < TLV_HEADER_SIZE:
                raise MetadataError(f"truncated TLV header at offset {offset}")
            tag, length = TLV_STRUCT.unpack_from(view, offset)
            offset += TLV_HEADER_SIZE
            end = offset + length
            if end > size:
                raise MetadataError(
                    f"TLV 0x{tag:02X} declares {length} bytes but only {size - offset} remain"
                )
            value = bytes(view[offset:end])
            offset = end
            if tag in _KNOWN_TAGS:
                if tag in fields:
                    raise MetadataError(f"duplicate TLV tag 0x{tag:02X}")
                fields[tag] = _decode_known(tag, value)
            else:
                extra.append((tag, value))
        return cls(
            auth_token=fields.get(Tag.AUTH_TOKEN),
            target_path=fields.get(Tag.TARGET_PATH),
            trace_id=fields.get(Tag.TRACE_ID),
            client_id=fields.get(Tag.CLIENT_ID),
            extra=tuple(extra),
        )


EMPTY_METADATA: Final = Metadata()
