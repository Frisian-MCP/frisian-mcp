"""Contrib middleware for the friese-mcp tool call middleware system."""

from __future__ import annotations

import abc
import importlib
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_PERIOD_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600}
_RATE_RE: re.Pattern[str] = re.compile(r"^(\d+)/([smh])$")


class AbstractRateLimitBackend(abc.ABC):
    """
    Interface for pluggable rate-limit counter backends.

    Implementations must be thread-safe and must track counts per *key*
    within a sliding window of *window* seconds.
    """

    @abc.abstractmethod
    def allow_request(self, key: str, limit: int, window: int) -> bool:
        """
        Decide whether the request identified by *key* is within the limit.

        Increments the counter for *key* then returns ``True`` if the
        updated count is within *limit* for the current *window* (seconds).
        Returns ``False`` when the limit has been exceeded.

        Args:
            key: Rate-limit bucket identifier (e.g. user ID or IP).
            limit: Maximum number of requests allowed per window.
            window: Duration of the sliding window in seconds.

        """


class InMemoryRateLimitBackend(AbstractRateLimitBackend):
    """
    In-process sliding-window counter (default backend).

    State is kept in a plain dict protected by a :class:`threading.Lock`.
    Each worker process maintains its own counters, so the effective rate
    under multi-worker deployments is ``limit × N``.  Use a shared backend
    (e.g. Redis) when cross-worker accuracy is required.
    """

    def __init__(self) -> None:
        """Initialise an empty counter store."""
        self._counters: dict[str, tuple[int, float]] = {}
        self._lock: threading.Lock = threading.Lock()

    def allow_request(self, key: str, limit: int, window: int) -> bool:
        """Increment counter for *key* and return whether it is within *limit*."""
        now = time.monotonic()
        with self._lock:
            count, window_start = self._counters.get(key, (0, now))
            if now - window_start >= window:
                count = 0
                window_start = now
            count += 1
            self._counters[key] = (count, window_start)
            return count <= limit


def _load_backend(dotted_path: str) -> AbstractRateLimitBackend:
    """Import *dotted_path* and return an instance of the backend class."""
    try:
        module_path, cls_name = dotted_path.rsplit(".", 1)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"FRIESE_MCP_RATE_LIMIT 'backend' must be a dotted import path, "
            f"got {dotted_path!r}."
        ) from exc
    module = importlib.import_module(module_path)
    cls = getattr(module, cls_name)
    return cls()


class RateLimitMiddleware:
    """
    Sliding-window rate limiter for MCP tool calls.

    Plugs into ``FRIESE_MCP_TOOL_MIDDLEWARE`` and reads configuration from
    ``FRIESE_MCP_RATE_LIMIT``::

        FRIESE_MCP_TOOL_MIDDLEWARE = [
            "friese_mcp.contrib.middleware.RateLimitMiddleware",
        ]

        FRIESE_MCP_RATE_LIMIT = {
            "rate": "10/s",     # <count>/<period> — s=second, m=minute, h=hour
            "key": "user_id",   # "user_id" | "tenant_id" | "ip"
            # optional — dotted import path to an AbstractRateLimitBackend subclass:
            "backend": "myapp.backends.RedisRateLimitBackend",
        }

    When ``FRIESE_MCP_RATE_LIMIT`` is absent the middleware is a no-op.

    Key resolution:
    - ``"user_id"``   — ``str(request.user.pk)``; ``"anonymous"`` for unauthenticated.
    - ``"tenant_id"`` — ``str(request.user.tenant_id)`` if the attribute exists,
                        otherwise falls back to ``user_id`` resolution.
    - ``"ip"``        — ``request.META["REMOTE_ADDR"]``.

    The ``backend`` key selects the counter storage.  Omitting it uses
    :class:`InMemoryRateLimitBackend` (in-process, no external dependency).
    Supply a dotted path to an :class:`AbstractRateLimitBackend` subclass
    (e.g. a Redis-backed implementation) for shared cross-worker counters.

    Raises:
        :exc:`~django.core.exceptions.ImproperlyConfigured`: When
            ``FRIESE_MCP_RATE_LIMIT`` is present but ``rate`` has an invalid
            format or ``backend`` cannot be imported.

    """

    def __init__(self) -> None:
        """Read settings, parse the rate string, and instantiate the backend."""
        config: dict[str, Any] | None = getattr(settings, "FRIESE_MCP_RATE_LIMIT", None)
        self._enabled: bool = config is not None
        self._limit: int = 0
        self._window: int = 1
        self._key_type: str = "user_id"
        self._backend: AbstractRateLimitBackend | None = None

        self._proxy_count: int = getattr(settings, "FRIESE_MCP_TRUSTED_PROXY_COUNT", 0)

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

            backend_path: str | None = config.get("backend")
            self._backend = (
                _load_backend(backend_path)
                if backend_path
                else InMemoryRateLimitBackend()
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_key(self, request: Any) -> str:
        """Derive the rate-limit bucket key from *request*."""
        if self._key_type == "ip":
            if self._proxy_count > 0:
                xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
                parts = [p.strip() for p in xff.split(",") if p.strip()]
                if len(parts) >= self._proxy_count:
                    return parts[-self._proxy_count]
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
        if not self._enabled or self._backend is None:
            return call_next(request, tool_name, arguments)

        key = self._resolve_key(request)
        if not self._backend.allow_request(key, self._limit, self._window):
            raise PermissionError("Rate limit exceeded")

        return call_next(request, tool_name, arguments)
