from __future__ import annotations

import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lcp import EncodeError, Metadata, MetadataError, Tag, normalize_trace_id
from lcp.constants import MAX_METADATA_LENGTH


def test_roundtrip_all_standard_tags_in_canonical_order() -> None:
    meta = Metadata(
        auth_token="tok", target_path="/v1/user/profile", trace_id=bytes(range(16)), client_id="c-1"
    )
    wire = meta.encode()
    assert wire.startswith(b"\x01\x00\x03tok\x02\x00\x10/v1/user/profile\x03\x00\x10")
    assert Metadata.decode(wire) == meta


def test_auth_token_is_redacted_in_reprs_and_logs() -> None:
    from lcp import Frame, MessageType

    meta = Metadata(auth_token="hunter2", target_path="/x")
    frame = Frame(MessageType.REQ, 1, metadata=meta)
    for text in (repr(meta), str(meta), repr(frame), f"{frame!r}"):
        assert "hunter2" not in text and "<redacted>" in text
    assert Metadata(auth_token="a") != Metadata(auth_token="b")  # equality still sees the token


def test_empty_metadata() -> None:
    assert Metadata().encode() == b""
    assert Metadata.decode(b"") == Metadata()


def test_korean_text_roundtrip() -> None:
    meta = Metadata(target_path="/v1/사용자/프로필", client_id="클라이언트-1")
    assert Metadata.decode(meta.encode()) == meta


def test_unknown_tags_are_preserved_in_wire_order() -> None:
    wire = b"\x7f\x00\x02hi" + b"\x02\x00\x02/a" + b"\x7f\x00\x01Z"
    meta = Metadata.decode(wire)
    assert meta.target_path == "/a"
    assert meta.extra == ((0x7F, b"hi"), (0x7F, b"Z"))  # repeating unknown tags is tolerated
    assert Metadata.decode(meta.encode()) == meta


def test_trace_id_forms() -> None:
    value = uuid.uuid4()
    assert normalize_trace_id(value) == value.bytes and len(value.bytes) == 16
    assert normalize_trace_id("req-42") == b"req-42"
    assert normalize_trace_id(b"\x00" * 16) == b"\x00" * 16
    with pytest.raises(EncodeError):
        normalize_trace_id("")
    # a 16-byte value need not be UTF-8, any other length must be a string
    assert Metadata.decode(b"\x03\x00\x10" + b"\xff" * 16).trace_id == b"\xff" * 16
    with pytest.raises(MetadataError):
        Metadata.decode(b"\x03\x00\x02\xff\xfe")
    with pytest.raises(MetadataError):
        Metadata.decode(b"\x03\x00\x00")


@pytest.mark.parametrize(
    "wire",
    [
        b"\x02",  # tag without length
        b"\x02\x00",  # truncated length
        b"\x02\x00\x05abc",  # value overruns the block
        b"\x02\x00\x01a\x02\x00\x01b",  # duplicate standard tag
        b"\x01\x00\x02\xff\xfe",  # invalid UTF-8 in a string tag
    ],
)
def test_malformed_blocks_are_rejected(wire: bytes) -> None:
    with pytest.raises(MetadataError):
        Metadata.decode(wire)


def test_size_limits() -> None:
    # Metadata Length is a uint16, so one entry can carry at most 65535 - 3 bytes.
    ok = Metadata(auth_token="a" * (MAX_METADATA_LENGTH - 3))
    assert len(ok.encode()) == MAX_METADATA_LENGTH
    assert Metadata.decode(ok.encode()) == ok
    with pytest.raises(EncodeError):
        Metadata(auth_token="a" * (MAX_METADATA_LENGTH - 2)).encode()
    with pytest.raises(EncodeError):
        Metadata(auth_token="a" * 40_000, client_id="b" * 40_000).encode()
    with pytest.raises(MetadataError):
        Metadata.decode(b"\x7f\x00\x00" * 21846)  # > 65535 bytes of TLV data


def test_encoder_rejects_misuse() -> None:
    with pytest.raises(EncodeError):
        Metadata(extra=((int(Tag.AUTH_TOKEN), b"x"),)).encode()  # use the dedicated field
    with pytest.raises(EncodeError):
        Metadata(extra=((256, b"x"),)).encode()
    with pytest.raises(EncodeError):
        Metadata(auth_token="\ud800").encode()  # lone surrogate is not UTF-8 encodable


_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)


@given(
    auth=st.none() | _text,
    path=st.none() | _text,
    trace=st.none() | st.binary(min_size=1, max_size=16),
    client=st.none() | _text,
    extra=st.lists(
        st.tuples(
            st.integers(0, 255).filter(lambda t: t not in {1, 2, 3, 4}), st.binary(max_size=20)
        ),
        max_size=4,
    ),
)
def test_hypothesis_roundtrip(
    auth: str | None,
    path: str | None,
    trace: bytes | None,
    client: str | None,
    extra: list[tuple[int, bytes]],
) -> None:
    if trace is not None and len(trace) != 16:
        trace = trace.hex().encode()  # non-16 byte ids must be valid UTF-8 text
    meta = Metadata(auth, path, trace, client, tuple(extra))
    assert Metadata.decode(meta.encode()) == meta
