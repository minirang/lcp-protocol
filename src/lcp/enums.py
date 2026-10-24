"""Enumerations defined by the LCP specification (sections 3.2, 4, 5)."""

from __future__ import annotations

import enum


class MessageType(enum.IntEnum):
    """Header *Message Type* values (spec 5.1)."""

    REQ = 0x01
    RES = 0x02
    EVT = 0x03
    EVT_ACK = 0x04
    PING = 0x05
    PONG = 0x06


class Serializer(enum.IntEnum):
    """Header *Serializer* values (spec 3.2)."""

    JSON = 0x00
    PROTOBUF = 0x01
    RAW = 0x02


class Status(enum.IntEnum):
    """Status codes (spec 5.2)."""

    OK = 0x00
    BAD_REQUEST = 0x01
    UNAUTHORIZED = 0x02
    NOT_FOUND = 0x04
    PAYLOAD_TOO_LARGE = 0x08
    BUSY = 0x09
    RATE_LIMITED = 0x09  # the spec names this code "BUSY / RATE_LIMITED"; same wire value
    INTERNAL_ERROR = 0x0F


class Tag(enum.IntEnum):
    """Standard metadata TLV tags (spec 4)."""

    AUTH_TOKEN = 0x01
    TARGET_PATH = 0x02
    TRACE_ID = 0x03
    CLIENT_ID = 0x04


def status_name(code: int) -> str:
    """Return the symbolic name of a status code (``UNKNOWN_0x..`` if undefined)."""
    try:
        return Status(code).name
    except ValueError:
        return f"UNKNOWN_0x{int(code):02X}"
