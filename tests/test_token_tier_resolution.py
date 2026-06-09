"""
Tests for FRISIAN_MCP_RESOLVE_TIER hook + FRISIAN_MCP_TOKEN_TIER_MAP role map.

Covers PKG-15 — bridging host-app tokens (which lack ``.permission``) to a
frisian-mcp tier so authenticated users are not silently capped at ``"read"``.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory

from frisian_mcp.registry import _resolve_request_tier
from frisian_mcp.views import _get_token_permission

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def rf() -> RequestFactory:
    """Django RequestFactory."""
    return RequestFactory()


def _build_request(
    rf: RequestFactory,
    *,
    auth: Any = None,
    user: Any = None,
) -> Any:
    """Build a minimal POST request with optional auth/user attributes."""
    req = rf.post("/mcp/", content_type="application/json")
    req.auth = auth  # type: ignore[attr-defined]
    if user is not None:
        req.user = user  # type: ignore[attr-defined]
    return req


def _user(
    *,
    is_superuser: bool = False,
    is_staff: bool = False,
    is_authenticated: bool = True,
) -> Any:
    """Return a MagicMock with the given user-permission attributes."""
    user = MagicMock()
    user.is_superuser = is_superuser
    user.is_staff = is_staff
    user.is_authenticated = is_authenticated
    return user


# ---------------------------------------------------------------------------
# Baseline behaviour (no settings) — must match PKG-11 contract
# ---------------------------------------------------------------------------


class TestBaselineBackwardsCompat:
    """With no PKG-15 settings configured, behaviour matches the prior contract."""

    def test_unauthenticated_uses_unauth_setting(self, rf: RequestFactory, settings: Any) -> None:
        """request.auth=None falls through to FRISIAN_MCP_UNAUTHENTICATED_TIER."""
        settings.FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"
        assert _resolve_request_tier(_build_request(rf, auth=None)) == "read"

    def test_unauthenticated_default_when_setting_absent(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Default fallback is 'read' when FRISIAN_MCP_UNAUTHENTICATED_TIER is unset."""
        if hasattr(settings, "FRISIAN_MCP_UNAUTHENTICATED_TIER"):
            del settings.FRISIAN_MCP_UNAUTHENTICATED_TIER
        assert _resolve_request_tier(_build_request(rf, auth=None)) == "read"

    def test_token_with_permission_attr_wins(self, rf: RequestFactory) -> None:
        """The historical request.auth.permission convention is preserved."""
        auth = MagicMock()
        auth.permission = "admin"
        assert _resolve_request_tier(_build_request(rf, auth=auth)) == "admin"

    def test_token_without_permission_attr_returns_read(self, rf: RequestFactory) -> None:
        """A token object that lacks .permission falls through to 'read'."""
        auth = MagicMock(spec=["__class__"])  # No .permission attribute
        assert _resolve_request_tier(_build_request(rf, auth=auth)) == "read"

    def test_views_shim_delegates_to_registry(self, rf: RequestFactory) -> None:
        """views._get_token_permission is now a thin shim with identical behaviour."""
        auth = MagicMock()
        auth.permission = "read_write"
        req = _build_request(rf, auth=auth)
        assert _get_token_permission(req) == _resolve_request_tier(req)


# ---------------------------------------------------------------------------
# FRISIAN_MCP_RESOLVE_TIER callable hook
# ---------------------------------------------------------------------------


def _hook_returns_admin(_request: Any) -> str:
    """Module-level callable used to test dotted-path resolution."""
    return "admin"


def _hook_returns_none(_request: Any) -> str | None:
    """Module-level callable that returns None to force fall-through."""
    return None


def _hook_raises(_request: Any) -> str:
    """Module-level callable that raises so fall-through can be exercised."""
    raise RuntimeError("boom")


class TestResolveTierHook:
    """settings.FRISIAN_MCP_RESOLVE_TIER is the highest-priority resolver."""

    def test_callable_hook_wins_over_token_permission(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """A callable hook overrides request.auth.permission."""
        settings.FRISIAN_MCP_RESOLVE_TIER = lambda request: "admin"
        auth = MagicMock()
        auth.permission = "read"  # would lose to the hook
        assert _resolve_request_tier(_build_request(rf, auth=auth)) == "admin"

    def test_dotted_path_resolves(self, rf: RequestFactory, settings: Any) -> None:
        """A dotted-path string is import_string()-resolved."""
        settings.FRISIAN_MCP_RESOLVE_TIER = "tests.test_token_tier_resolution._hook_returns_admin"
        assert _resolve_request_tier(_build_request(rf, auth=None)) == "admin"

    def test_hook_returning_none_falls_through(self, rf: RequestFactory, settings: Any) -> None:
        """When the hook returns None, the next layer is consulted."""
        settings.FRISIAN_MCP_RESOLVE_TIER = _hook_returns_none
        auth = MagicMock()
        auth.permission = "read_write"
        assert _resolve_request_tier(_build_request(rf, auth=auth)) == "read_write"

    def test_hook_raising_falls_through_safely(
        self, rf: RequestFactory, settings: Any, caplog: Any
    ) -> None:
        """A raising hook is logged and treated as fall-through, not 500."""
        settings.FRISIAN_MCP_RESOLVE_TIER = _hook_raises
        auth = MagicMock()
        auth.permission = "read"
        with caplog.at_level(logging.ERROR, logger="frisian_mcp.registry"):
            tier = _resolve_request_tier(_build_request(rf, auth=auth))
        assert tier == "read"
        assert any("FRISIAN_MCP_RESOLVE_TIER hook raised" in r.message for r in caplog.records)

    def test_unimportable_dotted_path_logged_and_ignored(
        self, rf: RequestFactory, settings: Any, caplog: Any
    ) -> None:
        """A bad dotted path is logged at ERROR and the chain continues."""
        settings.FRISIAN_MCP_RESOLVE_TIER = "nonexistent.module.fn"
        with caplog.at_level(logging.ERROR, logger="frisian_mcp.registry"):
            tier = _resolve_request_tier(_build_request(rf, auth=None))
        assert tier == "read"
        assert any("could not be imported" in r.message for r in caplog.records)

    def test_non_callable_non_string_setting_ignored(
        self, rf: RequestFactory, settings: Any, caplog: Any
    ) -> None:
        """A bogus setting type (e.g. an int) is logged and ignored."""
        settings.FRISIAN_MCP_RESOLVE_TIER = 42
        with caplog.at_level(logging.ERROR, logger="frisian_mcp.registry"):
            tier = _resolve_request_tier(_build_request(rf, auth=None))
        assert tier == "read"
        assert any("must be a callable or dotted-path" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# FRISIAN_MCP_TOKEN_TIER_MAP role map
# ---------------------------------------------------------------------------


class TestTokenTierMap:
    """Static role map kicks in when no callable hook and no token .permission."""

    def test_superuser_gets_mapped_tier(self, rf: RequestFactory, settings: Any) -> None:
        """is_superuser=True maps to the 'superuser' entry."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"superuser": "admin", "default": "read"}
        auth = MagicMock(spec=["__class__"])  # No .permission so role map is consulted
        req = _build_request(rf, auth=auth, user=_user(is_superuser=True))
        assert _resolve_request_tier(req) == "admin"

    def test_staff_gets_mapped_tier(self, rf: RequestFactory, settings: Any) -> None:
        """is_staff=True (without superuser) maps to the 'staff' entry."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"staff": "read_write", "default": "read"}
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth, user=_user(is_staff=True))
        assert _resolve_request_tier(req) == "read_write"

    def test_authenticated_user_gets_default(self, rf: RequestFactory, settings: Any) -> None:
        """Plain authenticated users (no superuser/staff) get the 'default' entry."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"default": "read_write"}
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth, user=_user(is_authenticated=True))
        assert _resolve_request_tier(req) == "read_write"

    def test_anonymous_does_not_get_default(self, rf: RequestFactory, settings: Any) -> None:
        """Unauthenticated callers must NOT receive the role-map default."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"default": "admin"}
        settings.FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"
        # auth=None → unauthenticated path; default would be a security regression here.
        assert _resolve_request_tier(_build_request(rf, auth=None)) == "read"

    def test_superuser_falls_through_when_role_key_absent(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """A superuser without a 'superuser' entry falls back to 'default'."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"default": "read"}
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth, user=_user(is_superuser=True))
        assert _resolve_request_tier(req) == "read"

    def test_token_permission_takes_precedence_over_role_map(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """An explicit token .permission wins over the role map."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"superuser": "admin"}
        auth = MagicMock()
        auth.permission = "read"
        req = _build_request(rf, auth=auth, user=_user(is_superuser=True))
        assert _resolve_request_tier(req) == "read"

    def test_empty_role_map_is_no_op(self, rf: RequestFactory, settings: Any) -> None:
        """An empty {} role map skips the lookup entirely."""
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {}
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth, user=_user(is_superuser=True))
        assert _resolve_request_tier(req) == "read"


# ---------------------------------------------------------------------------
# Resolution order — full chain
# ---------------------------------------------------------------------------


class TestResolutionOrder:
    """The chain order is hook → token.permission → role map → fallback."""

    def test_hook_beats_role_map(self, rf: RequestFactory, settings: Any) -> None:
        """The callable hook is checked before the role map."""
        settings.FRISIAN_MCP_RESOLVE_TIER = lambda request: "admin"
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"default": "read"}
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth, user=_user(is_authenticated=True))
        assert _resolve_request_tier(req) == "admin"

    def test_hook_falls_through_to_role_map(self, rf: RequestFactory, settings: Any) -> None:
        """Hook returning None lets the role map resolve."""
        settings.FRISIAN_MCP_RESOLVE_TIER = _hook_returns_none
        settings.FRISIAN_MCP_TOKEN_TIER_MAP = {"superuser": "admin"}
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth, user=_user(is_superuser=True))
        assert _resolve_request_tier(req) == "admin"

    def test_full_fallback_when_nothing_set(self, rf: RequestFactory, settings: Any) -> None:
        """No hook + no token attr + no role map + no user → 'read'."""
        # Ensure no settings linger from prior tests.
        for attr in ("FRISIAN_MCP_RESOLVE_TIER", "FRISIAN_MCP_TOKEN_TIER_MAP"):
            if hasattr(settings, attr):
                delattr(settings, attr)
        auth = MagicMock(spec=["__class__"])
        req = _build_request(rf, auth=auth)
        assert _resolve_request_tier(req) == "read"
