from __future__ import annotations

import asyncio
from collections.abc import Callable

from lcp.constants import HEADER_STRUCT, MAGIC


def header(
    *,
    magic: int = MAGIC,
    version: int = 1,
    msg_type: int = 1,
    serializer: int = 0,
    status: int = 0,
    meta_len: int = 0,
    cid: int = 1,
    payload_len: int = 0,
) -> bytes:
    """Hand-built 16-byte header, so tests can violate the protocol on purpose."""
    return HEADER_STRUCT.pack(
        magic, version, msg_type, serializer, status, meta_len, cid, payload_len
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)
