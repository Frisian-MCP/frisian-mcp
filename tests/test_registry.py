"""Tests for ToolRegistry — register, list_tools, dispatch, and permission enforcement."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from rest_framework.permissions import BasePermission

from friese_mcp.registry import ToolInputError, ToolNotFoundError, ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
}


def _echo_tool(arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
    """Tool callable that echoes back its arguments."""
    return arguments


def _build_request(authenticated: bool = True) -> Any:
    """Build a minimal mock HTTP request."""
    req = MagicMock()
    req.user = MagicMock()
    req.user.is_authenticated = authenticated
    return req


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    """Tests for ToolRegistry.register()."""

    def test_register_stores_tool(self, registry: ToolRegistry) -> None:
        """A registered tool appears in list_tools()."""
        registry.register(
            name="test.ping",
            fn=_echo_tool,
            description="Echo tool",
            input_schema=_SIMPLE_SCHEMA,
        )
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "test.ping"
        assert tools[0]["description"] == "Echo tool"

    def test_register_overwrites_duplicate_name(self, registry: ToolRegistry) -> None:
        """Registering the same name twice keeps only the latest entry."""

        def _v2(_arguments: dict[str, Any], _request: Any) -> str:
            """Second version."""
            return "v2"

        registry.register("tool.x", _echo_tool, "first", {})
        registry.register("tool.x", _v2, "second", {})
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0]["description"] == "second"

    def test_list_tools_returns_input_schema(self, registry: ToolRegistry) -> None:
        """list_tools() exposes inputSchema for each registered tool."""
        registry.register("tool.a", _echo_tool, "A", _SIMPLE_SCHEMA)
        result = registry.list_tools()
        assert result[0]["inputSchema"] == _SIMPLE_SCHEMA

    def test_multiple_tools_listed(self, registry: ToolRegistry) -> None:
        """All registered tools are returned by list_tools()."""
        registry.register("tool.a", _echo_tool, "A", {})
        registry.register("tool.b", _echo_tool, "B", {})
        registry.register("tool.c", _echo_tool, "C", {})
        assert len(registry.list_tools()) == 3


# ---------------------------------------------------------------------------
# Dispatch — success paths
# ---------------------------------------------------------------------------


class TestDispatchSuccess:
    """Tests for ToolRegistry.dispatch() on the happy path."""

    def test_dispatch_calls_fn_and_returns_result(self, registry: ToolRegistry) -> None:
        """dispatch() calls the tool fn and returns its result."""
        registry.register("echo", _echo_tool, "Echo", _SIMPLE_SCHEMA)
        req = _build_request()
        result = registry.dispatch(req, "echo", {"value": "hello"})
        assert result == {"value": "hello"}

    def test_dispatch_with_empty_schema(self, registry: ToolRegistry) -> None:
        """dispatch() accepts empty arguments when the schema has no required fields."""
        registry.register("noop", _echo_tool, "Noop", {"type": "object"})
        req = _build_request()
        result = registry.dispatch(req, "noop", {})
        assert result == {}

    def test_dispatch_with_no_permission_classes(self, registry: ToolRegistry) -> None:
        """dispatch() succeeds when no permission classes are configured."""
        registry.register("open", _echo_tool, "Open", {}, permission_classes=None)
        req = _build_request(authenticated=False)
        assert registry.dispatch(req, "open", {}) == {}


# ---------------------------------------------------------------------------
# Dispatch — error paths
# ---------------------------------------------------------------------------


class TestDispatchErrors:
    """Tests for ToolRegistry.dispatch() error cases."""

    def test_unknown_tool_raises_tool_not_found(self, registry: ToolRegistry) -> None:
        """dispatch() raises ToolNotFoundError for an unregistered tool name."""
        req = _build_request()
        with pytest.raises(ToolNotFoundError):
            registry.dispatch(req, "no.such.tool", {})

    def test_tool_not_found_is_lookup_error(self, registry: ToolRegistry) -> None:
        """ToolNotFoundError is a LookupError (caught by views.py)."""
        req = _build_request()
        with pytest.raises(LookupError):
            registry.dispatch(req, "missing", {})

    def test_invalid_schema_raises_tool_input_error(self, registry: ToolRegistry) -> None:
        """dispatch() raises ToolInputError when arguments fail JSON Schema validation."""
        registry.register("strict", _echo_tool, "Strict", _SIMPLE_SCHEMA)
        req = _build_request()
        with pytest.raises(ToolInputError):
            registry.dispatch(req, "strict", {})  # missing required "value"

    def test_wrong_type_raises_tool_input_error(self, registry: ToolRegistry) -> None:
        """dispatch() raises ToolInputError when an argument has the wrong type."""
        registry.register("typed", _echo_tool, "Typed", _SIMPLE_SCHEMA)
        req = _build_request()
        with pytest.raises(ToolInputError):
            registry.dispatch(req, "typed", {"value": 42})  # must be string

    def test_tool_input_error_is_value_error(self, registry: ToolRegistry) -> None:
        """ToolInputError is a ValueError."""
        registry.register("v", _echo_tool, "V", _SIMPLE_SCHEMA)
        req = _build_request()
        with pytest.raises(ValueError):
            registry.dispatch(req, "v", {"value": []})


# ---------------------------------------------------------------------------
# Dispatch — permission enforcement
# ---------------------------------------------------------------------------


class _AllowAll(BasePermission):
    """Permission that always grants access."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Grant access unconditionally."""
        return True


class _DenyAll(BasePermission):
    """Permission that always denies access."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Deny access unconditionally."""
        return False


class TestDispatchPermissions:
    """Tests for per-tool permission enforcement inside dispatch()."""

    def test_allow_permission_passes(self, registry: ToolRegistry) -> None:
        """dispatch() proceeds when all permission classes allow."""
        registry.register("p", _echo_tool, "P", {}, permission_classes=[_AllowAll])
        assert registry.dispatch(_build_request(), "p", {}) == {}

    def test_deny_permission_raises(self, registry: ToolRegistry) -> None:
        """dispatch() raises PermissionError when a permission class denies."""
        registry.register("q", _echo_tool, "Q", {}, permission_classes=[_DenyAll])
        with pytest.raises(PermissionError):
            registry.dispatch(_build_request(), "q", {})

    def test_first_deny_short_circuits(self, registry: ToolRegistry) -> None:
        """dispatch() raises on the first denying permission (short-circuit)."""
        registry.register(
            "r",
            _echo_tool,
            "R",
            {},
            permission_classes=[_DenyAll, _AllowAll],
        )
        with pytest.raises(PermissionError):
            registry.dispatch(_build_request(), "r", {})

    def test_all_allow_succeeds(self, registry: ToolRegistry) -> None:
        """dispatch() succeeds when multiple permission classes all allow."""
        registry.register(
            "s",
            _echo_tool,
            "S",
            {},
            permission_classes=[_AllowAll, _AllowAll],
        )
        assert registry.dispatch(_build_request(), "s", {}) == {}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Smoke test for concurrent registration and listing."""

    def test_concurrent_register_is_safe(self, registry: ToolRegistry) -> None:
        """Concurrent registration from multiple threads does not raise."""
        errors: list[Exception] = []

        def _register(n: int) -> None:
            try:
                registry.register(f"tool.{n}", _echo_tool, f"Tool {n}", {})
            except Exception as exc:  # pylint: disable=broad-exception-caught
                errors.append(exc)

        threads = [threading.Thread(target=_register, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(registry.list_tools()) == 20
