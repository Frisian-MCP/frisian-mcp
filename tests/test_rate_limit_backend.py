"""Tests for the RateLimitMiddleware pluggable backend interface."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from frisian_mcp.contrib.middleware import (
    AbstractRateLimitBackend,
    InMemoryRateLimitBackend,
    RateLimitMiddleware,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_rf = RequestFactory()


def _noop_next(_request: Any, _tool_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
    return {"called": True}


class _CountingBackend(AbstractRateLimitBackend):
    """Stub backend that records calls and always allows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def allow_request(self, key: str, limit: int, window: int) -> bool:
        self.calls.append((key, limit, window))
        return True


class _BlockingBackend(AbstractRateLimitBackend):
    """Stub backend that always denies."""

    def allow_request(self, key: str, limit: int, window: int) -> bool:
        return False


# ---------------------------------------------------------------------------
# InMemoryRateLimitBackend — unit tests
# ---------------------------------------------------------------------------


class TestInMemoryRateLimitBackend:
    """InMemoryRateLimitBackend correctly tracks counts within a window."""

    def test_first_request_is_allowed(self) -> None:
        """The first request in a window is always allowed."""
        backend = InMemoryRateLimitBackend()
        assert backend.allow_request("user:1", limit=3, window=60) is True

    def test_within_limit_all_allowed(self) -> None:
        """Requests up to the limit are all allowed."""
        backend = InMemoryRateLimitBackend()
        for _ in range(5):
            assert backend.allow_request("user:1", limit=5, window=60) is True

    def test_exceeding_limit_returns_false(self) -> None:
        """The (limit + 1)th request returns False."""
        backend = InMemoryRateLimitBackend()
        for _ in range(3):
            backend.allow_request("k", limit=3, window=60)
        assert backend.allow_request("k", limit=3, window=60) is False

    def test_different_keys_are_independent(self) -> None:
        """Exhausting one key does not affect another key."""
        backend = InMemoryRateLimitBackend()
        for _ in range(2):
            backend.allow_request("a", limit=2, window=60)
        assert backend.allow_request("a", limit=2, window=60) is False
        assert backend.allow_request("b", limit=2, window=60) is True

    def test_window_reset_allows_new_requests(self) -> None:
        """After the window elapses, the counter resets and requests are allowed."""
        backend = InMemoryRateLimitBackend()
        with patch("frisian_mcp.contrib.middleware.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            backend.allow_request("k", limit=1, window=1)
            assert backend.allow_request("k", limit=1, window=1) is False

            mock_time.monotonic.return_value = 1002.0
            assert backend.allow_request("k", limit=1, window=1) is True

    def test_is_abstract_backend_subclass(self) -> None:
        """InMemoryRateLimitBackend is a proper AbstractRateLimitBackend."""
        assert isinstance(InMemoryRateLimitBackend(), AbstractRateLimitBackend)


# ---------------------------------------------------------------------------
# AbstractRateLimitBackend — interface contract
# ---------------------------------------------------------------------------


class TestAbstractRateLimitBackend:
    """AbstractRateLimitBackend cannot be instantiated directly."""

    def test_cannot_instantiate_abstract(self) -> None:
        """Directly instantiating AbstractRateLimitBackend raises TypeError."""
        with pytest.raises(TypeError):
            AbstractRateLimitBackend()  # type: ignore[abstract]  # pylint: disable=abstract-class-instantiated

    def test_custom_subclass_satisfies_interface(self) -> None:
        """A concrete subclass implementing allow_request can be instantiated."""
        backend = _CountingBackend()
        assert isinstance(backend, AbstractRateLimitBackend)

    def test_custom_subclass_allow_request_is_called(self) -> None:
        """allow_request on a custom subclass receives key, limit, window."""
        backend = _CountingBackend()
        backend.allow_request("mykey", 10, 60)
        assert backend.calls == [("mykey", 10, 60)]


# ---------------------------------------------------------------------------
# RateLimitMiddleware — backend injection via settings
# ---------------------------------------------------------------------------


class TestRateLimitMiddlewareBackendSetting:
    """FRISIAN_MCP_RATE_LIMIT['backend'] loads a custom backend class."""

    @override_settings(
        FRISIAN_MCP_RATE_LIMIT={
            "rate": "10/s",
            "key": "user_id",
            "backend": "tests.test_rate_limit_backend._CountingBackend",
        }
    )
    def test_custom_backend_is_instantiated(self) -> None:
        """Setting 'backend' loads and instantiates the named class."""
        mw = RateLimitMiddleware()
        assert isinstance(mw._backend, _CountingBackend)  # pylint: disable=protected-access

    @override_settings(
        FRISIAN_MCP_RATE_LIMIT={
            "rate": "10/s",
            "key": "user_id",
            "backend": "tests.test_rate_limit_backend._CountingBackend",
        }
    )
    def test_custom_backend_allow_request_is_called(self) -> None:
        """The middleware delegates to the custom backend's allow_request."""
        mw = RateLimitMiddleware()
        backend: _CountingBackend = mw._backend  # type: ignore[assignment]  # pylint: disable=protected-access
        req = _rf.get("/")
        req.user = AnonymousUser()
        mw(req, "tool", {}, _noop_next)
        assert len(backend.calls) == 1

    @override_settings(
        FRISIAN_MCP_RATE_LIMIT={
            "rate": "5/s",
            "key": "user_id",
            "backend": "tests.test_rate_limit_backend._BlockingBackend",
        }
    )
    def test_blocking_backend_raises_permission_error(self) -> None:
        """A backend that always returns False causes PermissionError."""
        mw = RateLimitMiddleware()
        req = _rf.get("/")
        req.user = AnonymousUser()
        with pytest.raises(PermissionError, match="Rate limit exceeded"):
            mw(req, "tool", {}, _noop_next)

    @override_settings(
        FRISIAN_MCP_RATE_LIMIT={
            "rate": "5/s",
            "key": "user_id",
        }
    )
    def test_no_backend_setting_uses_in_memory(self) -> None:
        """Omitting 'backend' key defaults to InMemoryRateLimitBackend."""
        mw = RateLimitMiddleware()
        assert isinstance(mw._backend, InMemoryRateLimitBackend)  # pylint: disable=protected-access

    @override_settings(
        FRISIAN_MCP_RATE_LIMIT={
            "rate": "5/s",
            "key": "user_id",
            "backend": "not_a_valid.module.path",
        }
    )
    def test_invalid_backend_path_raises(self) -> None:
        """An importable but nonexistent dotted path raises an import error."""
        with pytest.raises((ImportError, ModuleNotFoundError)):
            RateLimitMiddleware()

    @override_settings(
        FRISIAN_MCP_RATE_LIMIT={
            "rate": "5/s",
            "key": "user_id",
            "backend": "nodot",
        }
    )
    def test_backend_without_dot_raises_improperly_configured(self) -> None:
        """A backend path with no dots raises ImproperlyConfigured."""
        with pytest.raises(ImproperlyConfigured):
            RateLimitMiddleware()
