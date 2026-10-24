"""Maps ``TARGET_PATH`` values to handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from .message import Request

Handler = Callable[["Request"], Any]
"""A handler may be sync or async and may return a plain body, ``Encoded`` or ``Reply``."""

_H = TypeVar("_H", bound=Callable[..., Any])


class Router:
    """Resolves a path to a handler.

    Patterns: an exact path (``/v1/user/profile``), a prefix wildcard (``/v1/files/*``;
    the longest prefix wins) or the catch-all ``*``.
    """

    def __init__(self) -> None:
        self._exact: dict[str, Handler] = {}
        self._prefixes: list[tuple[str, Handler]] = []
        self._default: Handler | None = None

    def add(self, pattern: str, handler: Handler) -> None:
        if pattern == "*":
            self._default = handler
        elif pattern.startswith("/") and pattern.endswith("/*") and "*" not in pattern[:-1]:
            self._prefixes.append((pattern[:-1], handler))
            self._prefixes.sort(key=lambda item: len(item[0]), reverse=True)
        elif pattern.startswith("/") and "*" not in pattern:
            self._exact[pattern] = handler
        else:
            raise ValueError(f"invalid route pattern: {pattern!r}")

    def route(self, pattern: str) -> Callable[[_H], _H]:
        """Decorator form of :meth:`add`."""

        def decorator(handler: _H) -> _H:
            self.add(pattern, handler)
            return handler

        return decorator

    def resolve(self, path: str) -> Handler | None:
        handler = self._exact.get(path)
        if handler is not None:
            return handler
        for prefix, candidate in self._prefixes:
            if path.startswith(prefix):
                return candidate
        return self._default

    def __len__(self) -> int:
        return len(self._exact) + len(self._prefixes) + (self._default is not None)
