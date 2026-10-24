"""The wire format without any networking: build, dump and parse frames.

Run:  python examples/06_raw_frames.py
"""

from __future__ import annotations

from lcp import Frame, FrameDecoder, MessageType, Metadata, Serializer, Status
from lcp.cli import format_raw_frame

frame = Frame(
    MessageType.REQ,
    correlation_id=42,
    serializer=Serializer.JSON,
    metadata=Metadata(
        target_path="/v1/user/profile", client_id="demo", trace_id=b"0123456789abcdef"
    ),
    payload=b'{"id":7}',
)
wire = frame.encode()
print(f"{len(wire)} bytes on the wire:")
print(wire.hex(" "))

decoder = FrameDecoder()
for chunk in (wire[:5], wire[5:20], wire[20:]):  # TCP may deliver any chunking
    decoder.feed(chunk)
    for decoded in decoder:
        print("\nparsed:", decoded)

decoder.feed(Frame(MessageType.RES, 42, Status.OK, payload=b"{}").encode())
raw = decoder.next_raw()
assert raw is not None
print()
print(format_raw_frame(raw))
