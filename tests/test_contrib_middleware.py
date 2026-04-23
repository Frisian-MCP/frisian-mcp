"""Tests for friese_mcp.contrib.middleware.RateLimitMiddleware."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from friese_mcp.contrib.middleware import RateLimitMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_rf = RequestFactory()


def _make_request(user: Any = None, remote_addr: str = "127.0.0.1") -> Any:
    req = _rf.get("/", REMOTE_ADDR=remote_addr)
    req.user = user if user is not None else AnonymousUser()
    return req


def _make_user(pk: int, tenant_id: int | None = None) -> Any:
    """Return a minimal user-like object."""

    class _User:
        is_anonymous = False

        def __init__(self, pk: int, tenant_id: int | None) -> None:
            self.pk = pk
            if tenant_id is not None:
                self.tenant_id = tenant_id

    return _User(pk, tenant_id)


def _noop_next(request: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"called": True}


# ---------------------------------------------------------------------------
# TestRateLimitMiddlewareNotConfigured
# ---------------------------------------------------------------------------


class TestRateLimitMiddlewareNotConfigured:
    """When FRIESE_MCP_RATE_LIMIT is absent, the middleware is a no-op."""

    @override_settings(FRIESE_MCP_RATE_LIMIT=None)
    def test_no_config_passes_through(self) -> None:
        """Unconfigured middleware calls call_next unconditionally."""
        mw = RateLimitMiddleware()
        result = mw(_make_request(), "tool", {}, _noop_next)
        assert result == {"called": True}

    def test_missing_setting_passes_through(self) -> None:
        """Missing FRIESE_MCP_RATE_LIMIT setting calls call_next unconditionally."""
        with patch.object(
            type(RateLimitMiddleware()), "__init__", RateLimitMiddleware.__init__
        ):
            pass  # just verify no error on import
        mw = RateLimitMiddleware()
        result = mw(_make_request(), "tool", {}, _noop_next)
        # Default: no config → no-op (depends on test settings not having the key)
        assert result == {"called": True}


# ---------------------------------------------------------------------------
# TestRateLimitMiddlewareConfiguration
# ---------------------------------------------------------------------------


class TestRateLimitMiddlewareConfiguration:
    """Settings parsing and validation."""

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "bad", "key": "user_id"})
    def test_invalid_rate_raises_improperly_configured(self) -> None:
        """An unrecognised rate string raises ImproperlyConfigured at init."""
        with pytest.raises(ImproperlyConfigured, match="invalid rate"):
            RateLimitMiddleware()

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "10", "key": "user_id"})
    def test_rate_without_period_raises(self) -> None:
        """A rate with no period separator raises ImproperlyConfigured."""
        with pytest.raises(ImproperlyConfigured, match="invalid rate"):
            RateLimitMiddleware()

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "10/x", "key": "user_id"})
    def test_unknown_period_character_raises(self) -> None:
        """An unknown period character raises ImproperlyConfigured."""
        with pytest.raises(ImproperlyConfigured, match="invalid rate"):
            RateLimitMiddleware()

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "5/s", "key": "user_id"})
    def test_valid_seconds_rate_accepted(self) -> None:
        """A valid per-second rate is accepted without error."""
        mw = RateLimitMiddleware()
        assert mw._limit == 5  # pylint: disable=protected-access
        assert mw._window == 1  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "100/m", "key": "user_id"})
    def test_valid_minutes_rate_accepted(self) -> None:
        """A valid per-minute rate is parsed correctly."""
        mw = RateLimitMiddleware()
        assert mw._limit == 100  # pylint: disable=protected-access
        assert mw._window == 60  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "1000/h", "key": "user_id"})
    def test_valid_hours_rate_accepted(self) -> None:
        """A valid per-hour rate is parsed correctly."""
        mw = RateLimitMiddleware()
        assert mw._limit == 1000  # pylint: disable=protected-access
        assert mw._window == 3600  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# TestRateLimitMiddlewareEnforcement
# ---------------------------------------------------------------------------


class TestRateLimitMiddlewareEnforcement:
    """Core rate-limiting behaviour."""

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "3/s", "key": "user_id"})
    def test_within_limit_calls_next(self) -> None:
        """Requests within the limit pass through to call_next."""
        mw = RateLimitMiddleware()
        req = _make_request()
        for _ in range(3):
            result = mw(req, "tool", {}, _noop_next)
            assert result == {"called": True}

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "2/s", "key": "user_id"})
    def test_exceeding_limit_raises_permission_error(self) -> None:
        """The (limit+1)th request raises PermissionError."""
        mw = RateLimitMiddleware()
        req = _make_request()
        mw(req, "tool", {}, _noop_next)
        mw(req, "tool", {}, _noop_next)
        with pytest.raises(PermissionError, match="Rate limit exceeded"):
            mw(req, "tool", {}, _noop_next)

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "1/s", "key": "user_id"})
    def test_different_keys_tracked_independently(self) -> None:
        """User A being throttled does not affect user B."""
        mw = RateLimitMiddleware()
        req_a = _make_request(user=_make_user(pk=1))
        req_b = _make_request(user=_make_user(pk=2))

        mw(req_a, "tool", {}, _noop_next)
        with pytest.raises(PermissionError):
            mw(req_a, "tool", {}, _noop_next)

        result = mw(req_b, "tool", {}, _noop_next)
        assert result == {"called": True}

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "1/s", "key": "user_id"})
    def test_window_resets_after_period_expires(self) -> None:
        """After the window expires, the counter resets and requests are allowed."""
        mw = RateLimitMiddleware()
        req = _make_request()

        t0 = 1000.0
        with patch("friese_mcp.contrib.middleware.time") as mock_time:
            mock_time.monotonic.return_value = t0
            mw(req, "tool", {}, _noop_next)
            with pytest.raises(PermissionError):
                mw(req, "tool", {}, _noop_next)

            # Advance past the window
            mock_time.monotonic.return_value = t0 + 2.0
            result = mw(req, "tool", {}, _noop_next)
            assert result == {"called": True}


# ---------------------------------------------------------------------------
# TestRateLimitMiddlewareKeyResolvers
# ---------------------------------------------------------------------------


class TestRateLimitMiddlewareKeyResolvers:
    """Key resolution strategies."""

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "5/s", "key": "user_id"})
    def test_user_id_key_authenticated(self) -> None:
        """Authenticated users are keyed by str(user.pk)."""
        mw = RateLimitMiddleware()
        req = _make_request(user=_make_user(pk=42))
        assert mw._resolve_key(req) == "42"  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "5/s", "key": "user_id"})
    def test_user_id_key_anonymous(self) -> None:
        """Anonymous users resolve to 'anonymous'."""
        mw = RateLimitMiddleware()
        req = _make_request(user=AnonymousUser())
        assert mw._resolve_key(req) == "anonymous"  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "5/s", "key": "tenant_id"})
    def test_tenant_id_key_uses_tenant(self) -> None:
        """tenant_id key uses user.tenant_id when available."""
        mw = RateLimitMiddleware()
        req = _make_request(user=_make_user(pk=1, tenant_id=99))
        assert mw._resolve_key(req) == "99"  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "5/s", "key": "tenant_id"})
    def test_tenant_id_key_falls_back_to_user_id(self) -> None:
        """tenant_id key falls back to user.pk when tenant_id is absent."""
        mw = RateLimitMiddleware()
        req = _make_request(user=_make_user(pk=7))
        assert mw._resolve_key(req) == "7"  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "5/s", "key": "ip"})
    def test_ip_key_uses_remote_addr(self) -> None:
        """The ip key uses REMOTE_ADDR from request.META."""
        mw = RateLimitMiddleware()
        req = _make_request(remote_addr="203.0.113.5")
        assert mw._resolve_key(req) == "203.0.113.5"  # pylint: disable=protected-access

    @override_settings(FRIESE_MCP_RATE_LIMIT={"rate": "1/s", "key": "ip"})
    def test_ip_key_throttles_by_address(self) -> None:
        """Two different IPs are throttled independently."""
        mw = RateLimitMiddleware()
        req_a = _make_request(remote_addr="1.2.3.4")
        req_b = _make_request(remote_addr="5.6.7.8")

        mw(req_a, "tool", {}, _noop_next)
        with pytest.raises(PermissionError):
            mw(req_a, "tool", {}, _noop_next)

        result = mw(req_b, "tool", {}, _noop_next)
        assert result == {"called": True}
