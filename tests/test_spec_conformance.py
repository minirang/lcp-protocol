"""Ties the code to the specification text: constants, tables and byte-exact vectors."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lcp import (
    HEADER_SIZE,
    MAGIC,
    MAX_CORRELATION_ID,
    MAX_METADATA_LENGTH,
    MAX_PAYLOAD_LENGTH,
    PROTOCOL_VERSION,
    Frame,
    FrameDecoder,
    MessageType,
    Metadata,
    Serializer,
    Status,
    Tag,
)
from lcp.constants import HEADER_STRUCT

SPEC = Path(__file__).parent.parent / "docs" / "spec" / "LCP_Protocol_Specification.md"


def _section(text: str, start: str, end: str) -> str:
    begin = re.search(start, text, re.M)
    finish = re.search(end, text, re.M)
    assert begin and finish, f"section markers not found: {start!r} / {end!r}"
    return text[begin.end() : finish.start()]


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_constants_match_spec() -> None:
    assert HEADER_SIZE == HEADER_STRUCT.size == 16
    assert MAGIC == 0x4C43 and MAGIC.to_bytes(2, "big") == b"LC"
    assert PROTOCOL_VERSION == 0x01
    assert MAX_METADATA_LENGTH == 65_535
    assert MAX_PAYLOAD_LENGTH == 16 * 1024 * 1024
    assert MAX_CORRELATION_ID == 2**32 - 1


def test_message_types_match_spec_section_5_1(spec_text: str) -> None:
    section = _section(spec_text, r"^### 5\.1", r"^### 5\.2")
    pairs = re.findall(r"`0x([0-9A-Fa-f]{2})` \(`([A-Z_]+)`\)", section)
    assert len(pairs) == 6
    for code, name in pairs:
        assert MessageType[name] == int(code, 16)
    assert {m.name for m in MessageType} == {name for _, name in pairs}


def test_status_codes_match_spec_section_5_2(spec_text: str) -> None:
    section = _section(spec_text, r"^### 5\.2", r"^## 6\.")
    rows = re.findall(r"\|\s*`0x([0-9A-Fa-f]{2})`\s*\|\s*([^|]+?)\s*\|", section)
    assert len(rows) == 7
    for code, names_cell in rows:
        names = re.findall(r"`([A-Z_]+)`", names_cell)
        assert names
        for name in names:
            assert Status[name] == int(code, 16)
    assert {int(code, 16) for code, _ in rows} == {int(s) for s in Status}


def test_tags_match_spec_section_4(spec_text: str) -> None:
    section = _section(spec_text, r"^## 4\.", r"^## 5\.")
    pairs = re.findall(r"`0x([0-9A-Fa-f]{2})` \(`([A-Z_]+)`\)", section)
    assert len(pairs) == 4
    for code, name in pairs:
        assert Tag[name] == int(code, 16)


def test_serializer_values() -> None:
    assert (Serializer.JSON, Serializer.PROTOBUF, Serializer.RAW) == (0x00, 0x01, 0x02)


def test_header_field_offsets_and_big_endian() -> None:
    """Spec 3.1 layout: every field at its documented byte offset, network byte order."""
    frame = Frame(
        MessageType.EVT_ACK,
        correlation_id=0x01020304,
        status=Status.BUSY,
        serializer=Serializer.RAW,
        metadata=Metadata(client_id="ab"),
        payload=b"xyz",
    )
    wire = frame.encode()
    assert wire[0:2] == b"\x4c\x43"  # Magic Number
    assert wire[2] == 0x01  # Version
    assert wire[3] == 0x04  # Message Type
    assert wire[4] == 0x02  # Serializer
    assert wire[5] == 0x09  # Status Code
    assert wire[6:8] == (5).to_bytes(2, "big")  # Metadata Length (3-byte TLV header + "ab")
    assert wire[8:12] == bytes([1, 2, 3, 4])  # Correlation ID
    assert wire[12:16] == (3).to_bytes(4, "big")  # Payload Length
    assert wire[16:21] == b"\x04\x00\x02ab"  # TLV: tag 0x04, length 2, value
    assert wire[21:] == b"xyz"
    assert len(wire) == HEADER_SIZE + 5 + 3


@pytest.mark.parametrize(
    ("frame", "hex_wire"),
    [
        (
            Frame(
                MessageType.REQ,
                1,
                Status.OK,
                Serializer.JSON,
                Metadata(target_path="/v1/echo"),
                b'{"a":1}',
            ),
            "4c43 01 01 00 00 000b 00000001 00000007 02 0008 2f76312f6563686f 7b2261223a317d",
        ),
        (Frame.ping(7), "4c43 01 05 00 00 0000 00000007 00000000"),
        (Frame.pong(0xFFFFFFFF), "4c43 01 06 00 00 0000 ffffffff 00000000"),
        (
            Frame(MessageType.RES, 2, Status.UNAUTHORIZED, Serializer.JSON),
            "4c43 01 02 00 02 0000 00000002 00000000",
        ),
    ],
)
def test_golden_vectors(frame: Frame, hex_wire: str) -> None:
    wire = bytes.fromhex(hex_wire.replace(" ", ""))
    assert frame.encode() == wire
    decoder = FrameDecoder()
    decoder.feed(wire)
    assert decoder.next_frame() == frame
    assert decoder.buffered == 0


def test_no_padding_between_header_metadata_and_payload() -> None:
    """Documented deviation: spec 3 claims 4-byte alignment but defines no padding, and the
    reference framer computes ``16 + meta_len + payload_len`` — so do we (see docs)."""
    frame = Frame(MessageType.REQ, 1, metadata=Metadata(target_path="/a"), payload=b"1")
    assert len(frame.encode()) == 16 + 3 + 2 + 1
