from __future__ import annotations

import random
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lcp import (
    EncodeError,
    Frame,
    FrameDecoder,
    MessageType,
    Metadata,
    ProtocolError,
    Serializer,
    Status,
    StrictLCPFramer,
)
from lcp.constants import MAGIC, MAX_PAYLOAD_LENGTH

from . import spec_reference
from .helpers import header

# -- Frame -----------------------------------------------------------------------------------


def test_frame_validation() -> None:
    with pytest.raises(EncodeError):
        Frame(7)  # type: ignore[arg-type]
    with pytest.raises(EncodeError):
        Frame(MessageType.REQ, correlation_id=2**32)
    with pytest.raises(EncodeError):
        Frame(MessageType.REQ, correlation_id=-1)
    with pytest.raises(EncodeError):
        Frame(MessageType.REQ, status=256)
    with pytest.raises(EncodeError):
        Frame(MessageType.REQ, serializer=256)
    with pytest.raises(EncodeError):
        Frame(MessageType.REQ, payload=bytes(MAX_PAYLOAD_LENGTH + 1))
    with pytest.raises(EncodeError):
        Frame(MessageType.PING, payload=b"x")
    with pytest.raises(EncodeError):
        Frame(MessageType.PONG, metadata=Metadata(client_id="c"))


def test_frame_repr_never_dumps_the_payload() -> None:
    text = repr(Frame(MessageType.REQ, payload=b"secret" * 1000))
    assert "secret" not in text and "6000B" in text


def test_frame_accepts_bytes_like_payload() -> None:
    assert Frame(MessageType.REQ, payload=bytearray(b"ab")).payload == b"ab"  # type: ignore[arg-type]


def test_encode_parts_keeps_large_payload_uncopied() -> None:
    payload = b"x" * 100_000
    parts = Frame(MessageType.REQ, payload=payload).encode_parts()
    assert len(parts) == 2 and parts[1] is payload
    assert len(Frame(MessageType.REQ).encode_parts()) == 1


# -- decoding basics -------------------------------------------------------------------------


def test_decodes_back_to_back_frames_and_unknown_status() -> None:
    frames = [
        Frame(MessageType.REQ, 1, metadata=Metadata(target_path="/a"), payload=b"1"),
        Frame.ping(2),
        Frame(MessageType.RES, 1, status=0x77, payload=b"{}"),
    ]
    decoder = FrameDecoder()
    decoder.feed(b"".join(f.encode() for f in frames))
    decoded = list(decoder)
    assert decoded == frames
    assert decoded[2].status == 0x77  # undefined status codes are preserved, not rejected
    assert decoder.next_frame() is None and decoder.buffered == 0


def test_partial_data_waits_for_more() -> None:
    wire = Frame(MessageType.REQ, 9, metadata=Metadata(target_path="/x"), payload=b"abc").encode()
    decoder = FrameDecoder()
    for byte in wire[:-1]:
        decoder.feed(bytes([byte]))
        assert decoder.next_frame() is None
    decoder.feed(wire[-1:])
    assert decoder.next_frame() is not None


def test_many_frames_in_one_buffer_are_all_decoded() -> None:
    wire = Frame(MessageType.REQ, 1, payload=b"p" * 20).encode()
    decoder = FrameDecoder()
    decoder.feed(wire * 20_000)  # exercises buffer compaction
    count = sum(1 for _ in decoder)
    assert count == 20_000 and decoder.buffered == 0


def test_max_payload_boundary() -> None:
    decoder = FrameDecoder()
    decoder.feed(header(payload_len=MAX_PAYLOAD_LENGTH))  # exactly 16 MiB is allowed
    assert decoder.next_frame() is None  # ...and simply waits for the body
    decoder = FrameDecoder()
    decoder.feed(header(payload_len=MAX_PAYLOAD_LENGTH + 1))
    with pytest.raises(ProtocolError) as info:
        decoder.next_frame()
    assert info.value.fatal and info.value.status == Status.PAYLOAD_TOO_LARGE


def test_decoder_validates_its_limit() -> None:
    with pytest.raises(ValueError):
        FrameDecoder(max_payload=MAX_PAYLOAD_LENGTH + 1)
    decoder = FrameDecoder(max_payload=10)
    decoder.feed(header(payload_len=11))
    with pytest.raises(ProtocolError):
        decoder.next_frame()


# -- fatal violations (spec 3.2 / 6) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("wire", "status", "reply"),
    [
        (header(magic=0x4745), Status.BAD_REQUEST, False),  # "GE" — HTTP; no reply, just close
        (header(version=2), Status.BAD_REQUEST, True),
        (header(version=0), Status.BAD_REQUEST, True),
        (header(msg_type=0), Status.BAD_REQUEST, True),
        (header(msg_type=7), Status.BAD_REQUEST, True),
        (header(msg_type=0xFF), Status.BAD_REQUEST, True),
        (header(msg_type=5, payload_len=1), Status.BAD_REQUEST, True),
        (header(msg_type=6, meta_len=3), Status.BAD_REQUEST, True),
        (header(payload_len=MAX_PAYLOAD_LENGTH + 1), Status.PAYLOAD_TOO_LARGE, True),
    ],
)
def test_fatal_violations(wire: bytes, status: int, reply: bool) -> None:
    decoder = FrameDecoder()
    decoder.feed(wire)
    with pytest.raises(ProtocolError) as info:
        decoder.next_frame()
    error = info.value
    assert error.fatal and error.status == status and error.reply is reply
    # the decoder is poisoned: the stream cannot be trusted any more
    with pytest.raises(ProtocolError):
        decoder.feed(b"more")
    with pytest.raises(ProtocolError):
        decoder.next_frame()


def test_bad_magic_is_detected_after_two_bytes() -> None:
    decoder = FrameDecoder()
    decoder.feed(b"GE")
    with pytest.raises(ProtocolError, match="magic"):
        decoder.next_frame()


def test_fatal_error_reports_correlation_id_for_the_reply() -> None:
    decoder = FrameDecoder()
    decoder.feed(header(version=9, cid=0xAABBCCDD, msg_type=3))
    with pytest.raises(ProtocolError) as info:
        decoder.next_frame()
    assert info.value.correlation_id == 0xAABBCCDD and info.value.msg_type == 3


# -- non-fatal violations -----------------------------------------------------------------------


def test_unknown_serializer_is_not_fatal() -> None:
    bad = header(serializer=0x7F, cid=5)
    good = Frame(MessageType.REQ, 6, metadata=Metadata(target_path="/ok")).encode()
    decoder = FrameDecoder()
    decoder.feed(bad + good)
    with pytest.raises(ProtocolError) as info:
        decoder.next_frame()
    assert not info.value.fatal and info.value.correlation_id == 5
    assert info.value.msg_type == MessageType.REQ
    frame = decoder.next_frame()  # the stream is still in sync
    assert frame is not None and frame.correlation_id == 6


def test_invalid_metadata_is_not_fatal() -> None:
    bad = header(meta_len=2, cid=8) + b"\x02\x00"
    decoder = FrameDecoder()
    decoder.feed(bad + Frame.ping(9).encode())
    with pytest.raises(ProtocolError, match="metadata") as info:
        decoder.next_frame()
    assert not info.value.fatal and info.value.correlation_id == 8
    assert decoder.next_frame() == Frame.ping(9)


def test_serializer_byte_is_ignored_for_keepalive_frames() -> None:
    decoder = FrameDecoder()
    decoder.feed(header(msg_type=5, serializer=0x7F, cid=3))
    assert decoder.next_frame() is not None


# -- compatibility wrapper ----------------------------------------------------------------------


class FakeSocket:
    closed = False

    def close(self) -> None:
        self.closed = True


def test_strict_framer_wrapper_mirrors_the_reference_api() -> None:
    framer = StrictLCPFramer()
    wire = Frame(MessageType.REQ, 4, metadata=Metadata(target_path="/x"), payload=b"hi").encode()
    framer.feed(wire[:10])
    assert framer.parse_frame(None) is None
    framer.feed(wire[10:])
    assert framer.parse_frame(None) == {
        "version": 1,
        "msg_type": 1,
        "serializer": 0,
        "status": 0,
        "corr_id": 4,
        "metadata": b"\x02\x00\x02/x",
        "payload": b"hi",
    }
    sock = FakeSocket()
    framer.feed(header(version=3))
    with pytest.raises(ValueError, match=r"\[LCP Protocol Error\]"):
        framer.parse_frame(sock)
    assert sock.closed
    framer.feed(Frame.ping(1).encode())  # like the reference, usable again after an abort
    assert framer.parse_frame(None) is not None


# -- property / differential tests -----------------------------------------------------------------

_frames = st.builds(
    lambda mt, cid, status, ser, path, payload: Frame(
        mt,
        cid,
        status,
        ser,
        Metadata(target_path=path)
        if mt not in (MessageType.PING, MessageType.PONG)
        else Metadata(),
        payload if mt not in (MessageType.PING, MessageType.PONG) else b"",
    ),
    st.sampled_from(list(MessageType)),
    st.integers(0, 2**32 - 1),
    st.integers(0, 255),
    st.sampled_from(list(Serializer)),
    st.none() | st.text(max_size=30),
    st.binary(max_size=300),
)


@given(st.lists(_frames, max_size=8), st.randoms(use_true_random=False))
def test_roundtrip_with_arbitrary_chunking(frames: list[Frame], rnd: random.Random) -> None:
    wire = b"".join(f.encode() for f in frames)
    decoder = FrameDecoder()
    out: list[Frame] = []
    pos = 0
    while pos < len(wire):
        step = rnd.randint(1, 40)
        decoder.feed(wire[pos : pos + step])
        pos += step
        out.extend(decoder)
    assert out == frames and decoder.buffered == 0


@given(st.binary(max_size=200))
@settings(max_examples=300)
def test_garbage_never_crashes_or_hangs(data: bytes) -> None:
    decoder = FrameDecoder()
    try:
        decoder.feed(data)
        for _ in decoder:
            pass
    except ProtocolError:
        pass  # the only permitted failure mode


_header_fields = st.fixed_dictionaries(
    {
        "magic": st.sampled_from([MAGIC, MAGIC, MAGIC, 0x4C44, 0]),
        "version": st.sampled_from([1, 1, 1, 0, 2, 255]),
        "msg_type": st.integers(0, 8),
        "serializer": st.integers(0, 3),
        "status": st.integers(0, 255),
        "meta_len": st.integers(0, 12),
        "cid": st.integers(0, 2**32 - 1),
        "payload_len": st.sampled_from([0, 1, 5, 40, MAX_PAYLOAD_LENGTH + 1]),
    }
)


@given(fields=_header_fields, body=st.binary(max_size=80), cut=st.integers(0, 120))
@settings(max_examples=500)
def test_differential_against_the_spec_reference_framer(
    fields: dict[str, int], body: bytes, cut: int
) -> None:
    """Both framers must agree on accept/abort and on every extracted field."""
    wire = (header(**fields) + body)[: 16 + cut]
    if len(wire) < 16:
        return  # the reference does nothing below 16 bytes (we may reject a bad magic earlier)

    reference = spec_reference.StrictLCPFramer()
    reference.feed(wire)
    try:
        expected: dict | None = reference.parse_frame(None)
        ref_aborted = False
    except ValueError:
        expected, ref_aborted = None, True

    ours = FrameDecoder()
    ours.feed(wire)
    try:
        raw = ours.next_raw()
        our_aborted = False
    except ProtocolError as error:
        raw, our_aborted = None, True
        assert error.fatal

    assert our_aborted == ref_aborted
    if expected is None:
        assert raw is None
    else:
        assert raw is not None
        assert (raw.msg_type, raw.serializer, raw.status, raw.correlation_id) == (
            expected["msg_type"],
            expected["serializer"],
            expected["status"],
            expected["corr_id"],
        )
        assert raw.metadata == expected["metadata"] and raw.payload == expected["payload"]
    # whatever is left in the buffer must match as well
    assert ours.buffered == len(reference.buffer)


def test_header_struct_matches_reference_format() -> None:
    assert struct.calcsize(">HBBBBHII") == 16
    assert (
        spec_reference.HEADER_SIZE == 16 and spec_reference.MAX_PAYLOAD_LIMIT == MAX_PAYLOAD_LENGTH
    )
