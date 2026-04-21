"""Contrib middleware for the friese-mcp tool call middleware system."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_PERIOD_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600}
_RATE_RE: re.Pattern[str] = re.compile(r"^(\d+)/([smh])$")


class RateLimitMiddleware:
    """
    In-process sliding-window rate limiter for MCP tool calls.

    Plugs into ``FRIESE_MCP_TOOL_MIDDLEWARE`` and reads configuration from
    ``FRIESE_MCP_RATE_LIMIT``::

        FRIESE_MCP_TOOL_MIDDLEWARE = [
            "friese_mcp.contrib.middleware.RateLimitMiddleware",
        ]

        FRIESE_MCP_RATE_LIMIT = {
            "rate": "10/s",     # <count>/<period> — s=second, m=minute, h=hour
            "key": "user_id",   # "user_id" | "tenant_id" | "ip"
        }

    When ``FRIESE_MCP_RATE_LIMIT`` is absent the middleware is a no-op.

    Key resolution:
    - ``"user_id"``   — ``str(request.user.pk)``; ``"anonymous"`` for unauthenticated.
    - ``"tenant_id"`` — ``str(request.user.tenant_id)`` if the attribute exists,
                        otherwise falls back to ``user_id`` resolution.
    - ``"ip"``        — ``request.META["REMOTE_ADDR"]``.

    Counter state is held in-process; no Redis or external dependency is required.
    A new window starts the first time a key is seen or after the previous window
    has elapsed.  When the count for a key exceeds the limit within its window,
    :exc:`PermissionError` is raised (the gateway converts this to an
    ``isError: True`` tool-level response).

    Raises:
        :exc:`~django.core.exceptions.ImproperlyConfigured`: When
            ``FRIESE_MCP_RATE_LIMIT`` is present but ``rate`` has an invalid
            format.

    """

    def __init__(self) -> None:
        """Read settings and parse the rate string once at instantiation time."""
        config: dict[str, Any] | None = getattr(settings, "FRIESE_MCP_RATE_LIMIT", None)
        self._enabled: bool = config is not None
        self._limit: int = 0
        self._window: int = 1
        self._key_type: str = "user_id"

        if config is not None:
            rate_str: str = config.get("rate", "")
            match = _RATE_RE.match(rate_str)
            if not match:
                raise ImproperlyConfigured(
                    f"FRIESE_MCP_RATE_LIMIT: invalid rate {rate_str!r}. "
                    "Expected '<count>/<period>' where period is s, m, or h "
                    "(e.g. '10/s', '100/m', '1000/h')."
                )
            self._limit = int(match.group(1))
            self._window = _PERIOD_SECONDS[match.group(2)]
            self._key_type = config.get("key", "user_id")

        self._counters: dict[str, tuple[int, float]] = {}
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_key(self, request: Any) -> str:
        """Derive the rate-limit bucket key from *request*."""
        if self._key_type == "ip":
            return str(request.META.get("REMOTE_ADDR", "unknown"))

        user = getattr(request, "user", None)

        if self._key_type == "tenant_id" and user is not None:
            tenant_id = getattr(user, "tenant_id", None)
            if tenant_id is not None:
                return str(tenant_id)

        if user is not None and not getattr(user, "is_anonymous", True):
            return str(user.pk)
        return "anonymous"

    # ------------------------------------------------------------------
    # Middleware protocol
    # ------------------------------------------------------------------

    def __call__(
        self,
        request: Any,
        tool_name: str,
        arguments: dict[str, Any],
        call_next: Callable[..., Any],
    ) -> Any:
        """Check the rate limit then delegate to *call_next*."""
        if not self._enabled:
            return call_next(request, tool_name, arguments)

        key = self._resolve_key(request)
        now = time.monotonic()

        with self._lock:
            count, window_start = self._counters.get(key, (0, now))
            if now - window_start >= self._window:
                count = 0
                window_start = now
            count += 1
            self._counters[key] = (count, window_start)
            exceeded = count > self._limit

        if exceeded:
            raise PermissionError("Rate limit exceeded")

        return call_next(request, tool_name, arguments)
