"""LCP — Lightweight Communication Protocol (reference implementation of spec v1.3.0)."""

from __future__ import annotations

from .auth import (
    Authenticator,
    HS256JWTAuthenticator,
    Principal,
    StaticTokenAuthenticator,
    issue_hs256_jwt,
)
from .client import LCPClient
from .codec import (
    Codec,
    Encoded,
    JsonCodec,
    ProtobufCodec,
    RawCodec,
    decode_body,
    encode_body,
    get_codec,
    register_codec,
)
from .constants import (
    DEFAULT_PORT,
    HEADER_SIZE,
    MAGIC,
    MAX_CORRELATION_ID,
    MAX_METADATA_LENGTH,
    MAX_PAYLOAD_LENGTH,
    PROTOCOL_VERSION,
)
from .enums import MessageType, Serializer, Status, Tag, status_name
from .errors import (
    ConnectionClosedError,
    DecodeError,
    EncodeError,
    LCPError,
    MetadataError,
    ProtocolError,
    RequestTimeoutError,
    ServerBusyError,
    StatusError,
    UnsupportedSerializerError,
)
from .frame import Frame
from .framer import FrameDecoder, RawFrame, StrictLCPFramer
from .message import Reply, Request, Response
from .metadata import Metadata, normalize_trace_id
from .ratelimit import RateLimit, TokenBucket
from .router import Router
from .server import LCPServer
from .session import Session, SessionConfig
from .tls import create_client_ssl_context, create_server_ssl_context

__version__ = "0.1.0"
SPEC_VERSION = "1.3.0"

__all__ = [
    "DEFAULT_PORT",
    "HEADER_SIZE",
    "MAGIC",
    "MAX_CORRELATION_ID",
    "MAX_METADATA_LENGTH",
    "MAX_PAYLOAD_LENGTH",
    "PROTOCOL_VERSION",
    "SPEC_VERSION",
    "Authenticator",
    "Codec",
    "ConnectionClosedError",
    "DecodeError",
    "Encoded",
    "EncodeError",
    "Frame",
    "FrameDecoder",
    "HS256JWTAuthenticator",
    "JsonCodec",
    "LCPClient",
    "LCPError",
    "LCPServer",
    "MessageType",
    "Metadata",
    "MetadataError",
    "Principal",
    "ProtobufCodec",
    "ProtocolError",
    "RateLimit",
    "RawCodec",
    "RawFrame",
    "Reply",
    "Request",
    "RequestTimeoutError",
    "Response",
    "Router",
    "Serializer",
    "ServerBusyError",
    "Session",
    "SessionConfig",
    "StaticTokenAuthenticator",
    "Status",
    "StatusError",
    "StrictLCPFramer",
    "Tag",
    "TokenBucket",
    "UnsupportedSerializerError",
    "__version__",
    "create_client_ssl_context",
    "create_server_ssl_context",
    "decode_body",
    "encode_body",
    "get_codec",
    "issue_hs256_jwt",
    "normalize_trace_id",
    "register_codec",
    "status_name",
]
