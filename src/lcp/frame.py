"""The :class:`Frame` value object and its wire encoding (spec section 3)."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    HEADER_STRUCT,
    MAGIC,
    MAX_CORRELATION_ID,
    MAX_PAYLOAD_LENGTH,
    PROTOCOL_VERSION,
)
from .enums import MessageType, Serializer, Status, status_name
from .errors import EncodeError
from .metadata import EMPTY_METADATA, Metadata


@dataclass(frozen=True, slots=True, repr=False)
class Frame:
    """One LCP packet: fixed header fields + metadata + payload.

    ``status`` and ``serializer`` are plain integers on purpose: a peer may legitimately
    send values this implementation does not know (they compare equal to the enum
    members when they do).  The wire *Version* is always :data:`PROTOCOL_VERSION`.
    """

    msg_type: MessageType
    correlation_id: int = 0
    status: int = Status.OK
    serializer: int = Serializer.JSON
    metadata: Metadata = EMPTY_METADATA
    payload: bytes = b""

    def __post_init__(self) -> None:
        try:
            msg_type = MessageType(self.msg_type)
        except ValueError as exc:
            raise EncodeError(f"invalid message type: {self.msg_type!r}") from exc
        object.__setattr__(self, "msg_type", msg_type)
        if not 0 <= self.correlation_id <= MAX_CORRELATION_ID:
            raise EncodeError(f"correlation id out of range: {self.correlation_id}")
        if not 0 <= self.status <= 0xFF:
            raise EncodeError(f"status out of range: {self.status}")
        if not 0 <= self.serializer <= 0xFF:
            raise EncodeError(f"serializer out of range: {self.serializer}")
        payload: object = self.payload  # callers may pass bytearray / memoryview
        if not isinstance(payload, bytes):
            object.__setattr__(self, "payload", bytes(payload))  # type: ignore[call-overload]
        if len(self.payload) > MAX_PAYLOAD_LENGTH:
            raise EncodeError(f"payload too large: {len(self.payload)} > {MAX_PAYLOAD_LENGTH}")
        if msg_type in (MessageType.PING, MessageType.PONG) and (
            self.payload or self.metadata != EMPTY_METADATA
        ):
            raise EncodeError("PING/PONG must carry neither metadata nor payload")

    @classmethod
    def _trusted(
        cls,
        msg_type: MessageType,
        correlation_id: int,
        status: int,
        serializer: int,
        metadata: Metadata,
        payload: bytes,
    ) -> Frame:
        """Build a frame whose invariants were already enforced (used by the decoder)."""
        frame = object.__new__(cls)
        setter = object.__setattr__
        setter(frame, "msg_type", msg_type)
        setter(frame, "correlation_id", correlation_id)
        setter(frame, "status", status)
        setter(frame, "serializer", serializer)
        setter(frame, "metadata", metadata)
        setter(frame, "payload", payload)
        return frame

    @classmethod
    def ping(cls, correlation_id: int = 0) -> Frame:
        return cls(MessageType.PING, correlation_id)

    @classmethod
    def pong(cls, correlation_id: int = 0) -> Frame:
        return cls(MessageType.PONG, correlation_id)

    def encode_parts(self) -> list[bytes]:
        """Wire bytes as ``[header + metadata, payload]`` (payload omitted when empty).

        Returning the payload separately avoids copying large payloads before they are
        handed to ``StreamWriter.writelines``.
        """
        meta = self.metadata.encode()
        header = HEADER_STRUCT.pack(
            MAGIC,
            PROTOCOL_VERSION,
            self.msg_type,
            self.serializer,
            self.status,
            len(meta),
            self.correlation_id,
            len(self.payload),
        )
        if self.payload:
            return [header + meta, self.payload]
        return [header + meta]

    def encode(self) -> bytes:
        """Serialize the whole frame to bytes."""
        return b"".join(self.encode_parts())

    def __repr__(self) -> str:  # never print payload bytes: they can be up to 16 MiB
        return (
            f"Frame({self.msg_type.name} cid={self.correlation_id} "
            f"status={status_name(self.status)} serializer=0x{self.serializer:02X} "
            f"metadata={self.metadata} payload={len(self.payload)}B)"
        )
