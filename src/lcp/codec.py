"""Payload serializers selected by the header *Serializer* byte (spec 3.2).

* ``0x00`` JSON      — built in (strict: UTF-8, no ``NaN``/``Infinity``).
* ``0x01`` Protobuf  — duck-typed; objects need ``SerializeToString()`` and message
  classes need ``FromString()``, so the ``protobuf`` package is not a hard dependency.
* ``0x02`` Raw       — bytes pass through untouched.

Custom codecs can be installed with :func:`register_codec`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .enums import Serializer
from .errors import DecodeError, EncodeError, UnsupportedSerializerError


class Codec(Protocol):
    """A payload encoder/decoder."""

    def encode(self, obj: Any) -> bytes: ...

    def decode(self, data: bytes, message_type: Any = None) -> Any: ...


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


class JsonCodec:
    """Compact, strict JSON (RFC 8259) — an empty payload decodes to ``None``."""

    def encode(self, obj: Any) -> bytes:
        try:
            return json.dumps(
                obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise EncodeError(f"object is not JSON serializable: {exc}") from exc

    def decode(self, data: bytes, message_type: Any = None) -> Any:
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DecodeError(f"invalid JSON payload: {exc}") from exc


class RawCodec:
    """Identity codec for opaque binary payloads."""

    def encode(self, obj: Any) -> bytes:
        if isinstance(obj, bytes | bytearray | memoryview):
            return bytes(obj)
        raise EncodeError("the RAW serializer only accepts bytes-like objects")

    def decode(self, data: bytes, message_type: Any = None) -> Any:
        return data


class ProtobufCodec:
    """Protobuf support without importing ``protobuf``.

    Encoding calls ``obj.SerializeToString()``.  Decoding needs the message class
    (``message_type.FromString(data)``); without it the raw bytes are returned.
    """

    def encode(self, obj: Any) -> bytes:
        serialize = getattr(obj, "SerializeToString", None)
        if serialize is None:
            raise EncodeError("the PROTOBUF serializer requires an object with SerializeToString()")
        return bytes(serialize())

    def decode(self, data: bytes, message_type: Any = None) -> Any:
        if message_type is None:
            return data
        try:
            return message_type.FromString(data)
        except Exception as exc:  # protobuf raises google.protobuf.message.DecodeError
            raise DecodeError(f"invalid protobuf payload: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Encoded:
    """A payload that is already serialized — bypasses the codec (proxies, echo, benchmarks)."""

    payload: bytes
    serializer: int = Serializer.RAW


_CODECS: dict[int, Codec] = {
    Serializer.JSON: JsonCodec(),
    Serializer.PROTOBUF: ProtobufCodec(),
    Serializer.RAW: RawCodec(),
}


def get_codec(serializer: int) -> Codec:
    """Return the codec registered for ``serializer`` (``UnsupportedSerializerError`` if none)."""
    try:
        return _CODECS[int(serializer)]
    except KeyError:
        raise UnsupportedSerializerError(serializer) from None


def register_codec(serializer: int, codec: Codec) -> None:
    """Install or replace the codec of a *Serializer* value (0..255)."""
    if not 0 <= int(serializer) <= 0xFF:
        raise ValueError("serializer must fit in one byte")
    _CODECS[int(serializer)] = codec


def infer_serializer(body: Any) -> int:
    """Choose the serializer for an outgoing body: bytes → RAW, protobuf → PROTOBUF, else JSON."""
    if isinstance(body, bytes | bytearray | memoryview):
        return Serializer.RAW
    if hasattr(body, "SerializeToString"):
        return Serializer.PROTOBUF
    return Serializer.JSON


def encode_body(body: Any, serializer: int | None = None) -> tuple[int, bytes]:
    """Serialize ``body``; returns ``(serializer, payload)``.  ``None`` means an empty payload."""
    if isinstance(body, Encoded):
        return int(body.serializer), body.payload
    if body is None:
        return int(Serializer.JSON if serializer is None else serializer), b""
    chosen = infer_serializer(body) if serializer is None else int(serializer)
    return chosen, get_codec(chosen).encode(body)


def decode_body(serializer: int, data: bytes, message_type: Any = None) -> Any:
    """Deserialize a payload according to its *Serializer* byte."""
    return get_codec(serializer).decode(data, message_type)
