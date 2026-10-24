"""Exception hierarchy of the package."""

from __future__ import annotations

from typing import Any

from .enums import Status, status_name


class LCPError(Exception):
    """Base class of every exception raised by this package."""


class EncodeError(LCPError, ValueError):
    """An outgoing frame, metadata block or body cannot be encoded."""


class MetadataError(LCPError, ValueError):
    """The TLV metadata block is malformed."""


class ProtocolError(LCPError):
    """The peer violated the wire protocol.

    ``fatal`` errors mean the byte stream can no longer be trusted and the connection
    must be closed.  Non-fatal errors only affect the offending frame, which has
    already been consumed from the stream, so decoding can continue.

    ``reply`` tells whether a best-effort error frame should be sent to the peer before
    closing (spec: a wrong *Magic Number* closes the connection without any reply).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = Status.BAD_REQUEST,
        fatal: bool = False,
        reply: bool = True,
        correlation_id: int | None = None,
        msg_type: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.fatal = fatal
        self.reply = reply
        self.correlation_id = correlation_id
        self.msg_type = msg_type


class StatusError(LCPError):
    """An LCP status code other than ``OK``.

    Handlers raise it to answer with a specific status; clients get it from
    :meth:`lcp.Response.raise_for_status`.
    """

    def __init__(
        self,
        status: int,
        message: str = "",
        *,
        details: Any = None,
        retry_after: float | None = None,
    ) -> None:
        self.status = int(status)
        self.message = message or status_name(self.status)
        self.details = details
        self.retry_after = retry_after
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{status_name(self.status)} (0x{self.status:02X}): {self.message}"


class DecodeError(StatusError):
    """A payload could not be decoded (answered with ``BAD_REQUEST``)."""

    def __init__(self, message: str) -> None:
        super().__init__(Status.BAD_REQUEST, message)


class UnsupportedSerializerError(StatusError):
    """No codec is registered for a *Serializer* value (answered with ``BAD_REQUEST``)."""

    def __init__(self, serializer: int) -> None:
        super().__init__(Status.BAD_REQUEST, f"unsupported serializer 0x{int(serializer):02X}")


class ServerBusyError(StatusError):
    """The peer answered ``BUSY`` / ``RATE_LIMITED`` (application back-pressure)."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(Status.BUSY, message, retry_after=retry_after)


class ConnectionClosedError(LCPError, ConnectionError):
    """The connection is closed (or closing); pending exchanges fail with this error."""


class RequestTimeoutError(LCPError, TimeoutError):
    """No reply arrived within the allotted time."""
