"""Wire-level constants for LCP (spec v1.3.0, wire version ``0x01``).

All multi-byte integers on the wire are **big-endian** (network byte order); the
reference framer in spec section 6 unpacks the header with ``">HBBBBHII"``.
"""

from __future__ import annotations

import struct
from typing import Final

MAGIC: Final[int] = 0x4C43
"""Header *Magic Number* — ASCII ``"LC"``."""

PROTOCOL_VERSION: Final[int] = 0x01
"""Value of the header *Version* byte implemented by this package."""

HEADER_SIZE: Final[int] = 16
"""Fixed header size in bytes (spec 3.2)."""

HEADER_STRUCT: Final[struct.Struct] = struct.Struct(">HBBBBHII")
"""magic, version, type, serializer, status, metadata length, correlation id, payload length."""

TLV_STRUCT: Final[struct.Struct] = struct.Struct(">BH")
"""Tag (1 byte) + Length (2 bytes) in front of every metadata value (spec 4)."""

TLV_HEADER_SIZE: Final[int] = 3

MAX_METADATA_LENGTH: Final[int] = 0xFFFF
"""*Metadata Length* is a ``uint16``: at most 65,535 bytes of TLV data."""

MAX_PAYLOAD_LENGTH: Final[int] = 16 * 1024 * 1024
"""Maximum payload size accepted by the protocol (16 MiB, inclusive)."""

MAX_CORRELATION_ID: Final[int] = 0xFFFFFFFF
"""*Correlation ID* is a ``uint32``."""

DEFAULT_PORT: Final[int] = 19523
"""Default TCP port of this implementation (``0x4C43`` read as a decimal number).

This is an arbitrary choice made by this package; the specification does not define a
port and no IANA registration is implied.
"""
