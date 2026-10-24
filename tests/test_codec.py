from __future__ import annotations

import pytest

from lcp import (
    DecodeError,
    Encoded,
    EncodeError,
    Serializer,
    UnsupportedSerializerError,
    decode_body,
    encode_body,
    get_codec,
    register_codec,
)
from lcp.codec import infer_serializer


def test_json_is_compact_and_keeps_unicode() -> None:
    serializer, payload = encode_body({"이름": "에이다", "n": [1, 2.5, None, True]})
    assert serializer == Serializer.JSON
    assert payload == '{"이름":"에이다","n":[1,2.5,null,true]}'.encode()
    assert decode_body(Serializer.JSON, payload) == {"이름": "에이다", "n": [1, 2.5, None, True]}


def test_json_strictness() -> None:
    for bad in (b"{", b"\xff\xfe", b"NaN", b"Infinity", b"[1,]", b"\x00"):
        with pytest.raises(DecodeError):
            decode_body(Serializer.JSON, bad)
    with pytest.raises(DecodeError):
        decode_body(Serializer.JSON, b"[" * 100_000)  # recursion bomb
    with pytest.raises(EncodeError):
        encode_body({"x": float("nan")})
    with pytest.raises(EncodeError):
        encode_body({"x": object()})


def test_empty_payload_means_none() -> None:
    assert decode_body(Serializer.JSON, b"") is None
    assert encode_body(None) == (Serializer.JSON, b"")
    assert encode_body(None, Serializer.RAW) == (Serializer.RAW, b"")


def test_raw_codec() -> None:
    assert encode_body(b"\x00\x01") == (Serializer.RAW, b"\x00\x01")
    assert encode_body(bytearray(b"ab"), Serializer.RAW) == (Serializer.RAW, b"ab")
    assert decode_body(Serializer.RAW, b"\x00\xff") == b"\x00\xff"
    with pytest.raises(EncodeError):
        encode_body("text", Serializer.RAW)


def test_encoded_bypasses_the_codec() -> None:
    assert encode_body(Encoded(b'{"x":1}', Serializer.JSON)) == (Serializer.JSON, b'{"x":1}')
    assert encode_body(Encoded(b"\x00")) == (Serializer.RAW, b"\x00")


def test_protobuf_codec_with_a_real_message() -> None:
    wrappers = pytest.importorskip("google.protobuf.wrappers_pb2")
    message = wrappers.StringValue(value="안녕")
    serializer, payload = encode_body(message)
    assert serializer == Serializer.PROTOBUF
    assert decode_body(Serializer.PROTOBUF, payload, wrappers.StringValue).value == "안녕"
    assert decode_body(Serializer.PROTOBUF, payload) == payload  # no type: raw bytes
    with pytest.raises(DecodeError):
        decode_body(Serializer.PROTOBUF, b"\xff\xff\xff", wrappers.StringValue)
    with pytest.raises(EncodeError):
        encode_body({"a": 1}, Serializer.PROTOBUF)


def test_serializer_inference() -> None:
    assert infer_serializer({"a": 1}) == Serializer.JSON
    assert infer_serializer([1]) == Serializer.JSON
    assert infer_serializer("text") == Serializer.JSON
    assert infer_serializer(b"x") == Serializer.RAW
    assert infer_serializer(memoryview(b"x")) == Serializer.RAW

    class FakeMessage:
        def SerializeToString(self) -> bytes:  # noqa: N802 - protobuf naming
            return b"\x08\x01"

    assert infer_serializer(FakeMessage()) == Serializer.PROTOBUF


def test_unsupported_serializer_and_custom_codec() -> None:
    with pytest.raises(UnsupportedSerializerError):
        get_codec(0x42)
    with pytest.raises(ValueError):
        register_codec(256, get_codec(Serializer.RAW))

    class Upper:
        def encode(self, obj: object) -> bytes:
            return str(obj).upper().encode()

        def decode(self, data: bytes, message_type: object = None) -> object:
            return data.decode().lower()

    register_codec(0x42, Upper())
    try:
        assert encode_body("abc", 0x42) == (0x42, b"ABC")
        assert decode_body(0x42, b"ABC") == "abc"
    finally:
        from lcp import codec

        del codec._CODECS[0x42]
