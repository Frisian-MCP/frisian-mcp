"""Tests for per-tool permission enforcement via ToolRegistry.dispatch()."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from rest_framework.permissions import BasePermission

from frisian_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Reusable permission classes
# ---------------------------------------------------------------------------


class AllowAll(BasePermission):
    """Permission that always grants access."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Grant access unconditionally."""
        return True


class DenyAll(BasePermission):
    """Permission that always denies access."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Deny access unconditionally."""
        return False


class AllowIfFlag(BasePermission):
    """Permission that grants access based on a flag on the request."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Grant access when request.allow_flag is True."""
        return bool(getattr(request, "allow_flag", False))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool(_arguments: dict[str, Any], _request: Any) -> str:
    """Simple tool callable."""
    return "ok"


def _req(allow_flag: bool = True) -> Any:
    """Build a mock request with an optional allow_flag attribute."""
    req = MagicMock()
    req.allow_flag = allow_flag
    return req


# ---------------------------------------------------------------------------
# Permission tests
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """Tests for dispatch() permission enforcement logic."""

    def test_no_permission_classes_always_passes(self, registry: ToolRegistry) -> None:
        """A tool with no permission_classes is accessible to all callers."""
        registry.register("open", _tool, "Open", {}, permission_classes=None)
        assert registry.dispatch(_req(), "open", {}) == "ok"

    def test_empty_permission_list_passes(self, registry: ToolRegistry) -> None:
        """An empty permission_classes list is treated as unrestricted."""
        registry.register("open2", _tool, "Open2", {}, permission_classes=[])
        assert registry.dispatch(_req(), "open2", {}) == "ok"

    def test_single_allow_permission_passes(self, registry: ToolRegistry) -> None:
        """A single AllowAll permission class permits dispatch."""
        registry.register("a", _tool, "A", {}, permission_classes=[AllowAll])
        assert registry.dispatch(_req(), "a", {}) == "ok"

    def test_single_deny_permission_raises(self, registry: ToolRegistry) -> None:
        """A single DenyAll permission class prevents dispatch."""
        registry.register("b", _tool, "B", {}, permission_classes=[DenyAll])
        with pytest.raises(PermissionError):
            registry.dispatch(_req(), "b", {})

    def test_deny_before_allow_raises(self, registry: ToolRegistry) -> None:
        """Deny before Allow still raises (permission classes checked in order)."""
        registry.register(
            "c",
            _tool,
            "C",
            {},
            permission_classes=[DenyAll, AllowAll],
        )
        with pytest.raises(PermissionError):
            registry.dispatch(_req(), "c", {})

    def test_allow_before_deny_raises(self, registry: ToolRegistry) -> None:
        """Allow before Deny still raises — all classes must allow."""
        registry.register(
            "d",
            _tool,
            "D",
            {},
            permission_classes=[AllowAll, DenyAll],
        )
        with pytest.raises(PermissionError):
            registry.dispatch(_req(), "d", {})

    def test_multiple_allow_permissions_pass(self, registry: ToolRegistry) -> None:
        """Multiple AllowAll classes all granting access succeeds."""
        registry.register(
            "e",
            _tool,
            "E",
            {},
            permission_classes=[AllowAll, AllowAll, AllowAll],
        )
        assert registry.dispatch(_req(), "e", {}) == "ok"

    def test_flag_based_permission_allows_when_flag_set(self, registry: ToolRegistry) -> None:
        """AllowIfFlag permits dispatch when request.allow_flag is True."""
        registry.register("f", _tool, "F", {}, permission_classes=[AllowIfFlag])
        assert registry.dispatch(_req(allow_flag=True), "f", {}) == "ok"

    def test_flag_based_permission_denies_when_flag_unset(self, registry: ToolRegistry) -> None:
        """AllowIfFlag denies dispatch when request.allow_flag is False."""
        registry.register("g", _tool, "G", {}, permission_classes=[AllowIfFlag])
        with pytest.raises(PermissionError):
            registry.dispatch(_req(allow_flag=False), "g", {})

    def test_permission_error_message_names_class(self, registry: ToolRegistry) -> None:
        """The PermissionError message names the denying permission class."""
        registry.register("h", _tool, "H", {}, permission_classes=[DenyAll])
        with pytest.raises(PermissionError, match="DenyAll"):
            registry.dispatch(_req(), "h", {})

    def test_permission_checked_before_fn_called(self, registry: ToolRegistry) -> None:
        """The tool function is never called when a permission class denies."""
        called: list[bool] = []

        def _sensitive(_arguments: dict[str, Any], _request: Any) -> str:
            """Sensitive tool that records calls."""
            called.append(True)
            return "secret"

        registry.register("s", _sensitive, "S", {}, permission_classes=[DenyAll])
        with pytest.raises(PermissionError):
            registry.dispatch(_req(), "s", {})
        assert not called
