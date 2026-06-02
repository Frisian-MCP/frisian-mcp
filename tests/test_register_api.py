"""Tests for the module-level register() API (imperative tool registration)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from rest_framework.permissions import BasePermission

import frisian_mcp
from frisian_mcp.registry import ToolRegistry, register, tool_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
}


def _echo(arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
    """Tool handler that echoes its arguments."""
    return arguments


def _request() -> MagicMock:
    """Build a minimal mock request."""
    req = MagicMock()
    req.user = MagicMock()
    req.user.is_authenticated = True
    return req


# ---------------------------------------------------------------------------
# Module-level register() function
# ---------------------------------------------------------------------------


class TestRegisterFunction:
    """Tests for frisian_mcp.registry.register()."""

    def test_register_is_importable_from_registry(self) -> None:
        """Register is importable directly from frisian_mcp.registry."""
        assert callable(register)

    def test_register_is_importable_from_package(self) -> None:
        """Register is importable from the frisian_mcp package namespace."""
        assert callable(frisian_mcp.register)

    def test_register_adds_tool_to_global_registry(self) -> None:
        """register() adds the tool to tool_registry so it appears in tools/list."""
        before = {t["name"] for t in tool_registry.list_tools()}
        register("test.echo_global", "Echo globally", _SCHEMA, _echo)
        after = {t["name"] for t in tool_registry.list_tools()}
        assert "test.echo_global" in after - before

    def test_register_tool_is_dispatchable(self) -> None:
        """A tool registered via register() can be dispatched through tool_registry."""
        register("test.dispatch_me", "Dispatch me", {}, _echo)
        result = tool_registry.dispatch(_request(), "test.dispatch_me", {})
        assert result == {}

    def test_register_stores_description(self) -> None:
        """register() stores the description in the tool manifest."""
        register("test.described", "A fine description", {}, _echo)
        tools = {t["name"]: t for t in tool_registry.list_tools()}
        assert tools["test.described"]["description"] == "A fine description"

    def test_register_stores_input_schema(self) -> None:
        """register() stores the input schema in the tool manifest."""
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        register("test.schema_stored", "Schema test", schema, _echo)
        tools = {t["name"]: t for t in tool_registry.list_tools()}
        assert tools["test.schema_stored"]["inputSchema"] == schema


# ---------------------------------------------------------------------------
# Manual registration against isolated ToolRegistry
# ---------------------------------------------------------------------------


class TestManualRegistration:
    """Manual registration with an isolated ToolRegistry."""

    def test_registered_tool_appears_in_list(self, registry: ToolRegistry) -> None:
        """A manually registered tool appears in list_tools()."""
        registry.register("api.ping", _echo, "Ping", {})
        names = [t["name"] for t in registry.list_tools()]
        assert "api.ping" in names

    def test_handler_receives_arguments(self, registry: ToolRegistry) -> None:
        """The handler receives the arguments dict at dispatch time."""
        captured: list[dict] = []

        def _capture(arguments: dict[str, Any], _req: Any) -> None:
            captured.append(dict(arguments))

        registry.register("api.capture", _capture, "Capture", {})
        registry.dispatch(_request(), "api.capture", {"x": 1})
        assert captured == [{"x": 1}]

    def test_duplicate_name_overwrites(self, registry: ToolRegistry) -> None:
        """Registering the same name twice keeps only the latest handler."""

        def _v2(_arguments: dict[str, Any], _req: Any) -> str:
            """Return v2 marker."""
            return "v2"

        registry.register("api.dup", _echo, "First", {})
        registry.register("api.dup", _v2, "Second", {})
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0]["description"] == "Second"
        assert registry.dispatch(_request(), "api.dup", {}) == "v2"


# ---------------------------------------------------------------------------
# Mixed registration: auto-discovered + manually registered
# ---------------------------------------------------------------------------


class TestMixedRegistration:
    """Manually registered tools coexist with decorator-registered tools."""

    def test_manual_and_mcp_tool_both_listed(self, registry: ToolRegistry) -> None:
        """Tools registered manually and via register() both appear in list_tools()."""
        registry.register("auto.tool", _echo, "Auto", {})
        registry.register("manual.tool", _echo, "Manual", {})
        names = {t["name"] for t in registry.list_tools()}
        assert "auto.tool" in names
        assert "manual.tool" in names

    def test_manual_tool_dispatch_does_not_affect_others(
        self, registry: ToolRegistry
    ) -> None:
        """Dispatching a manually registered tool doesn't affect other tools."""
        results: list[str] = []

        def _a(_arguments: dict, _req: Any) -> str:
            """Tool A handler."""
            results.append("a")
            return "a"

        def _b(_arguments: dict, _req: Any) -> str:
            """Tool B handler."""
            results.append("b")
            return "b"

        registry.register("tool.a", _a, "A", {})
        registry.register("tool.b", _b, "B", {})
        registry.dispatch(_request(), "tool.a", {})
        assert results == ["a"]

    def test_total_count_is_sum_of_all_registrations(self, registry: ToolRegistry) -> None:
        """list_tools() count equals the total of all registrations."""
        for i in range(3):
            registry.register(f"auto.{i}", _echo, f"Auto {i}", {})
        for i in range(2):
            registry.register(f"manual.{i}", _echo, f"Manual {i}", {})
        assert len(registry.list_tools()) == 5


# ---------------------------------------------------------------------------
# register() with permission_classes
# ---------------------------------------------------------------------------


class TestRegisterPermissions:
    """Permission_classes are honoured by tools registered via register()."""

    def test_register_with_no_permissions_is_open(self, registry: ToolRegistry) -> None:
        """A tool registered without permission_classes is callable by anyone."""
        registry.register("open.tool", _echo, "Open", {}, permission_classes=None)
        unauthenticated = MagicMock()
        unauthenticated.user.is_authenticated = False
        result = registry.dispatch(unauthenticated, "open.tool", {})
        assert result == {}

    @pytest.mark.django_db
    def test_register_with_deny_permission_raises(self, registry: ToolRegistry) -> None:
        """A tool registered with a denying permission raises PermissionError."""

        class DenyAll(BasePermission):
            """Always deny."""

            def has_permission(self, request: Any, view: Any) -> bool:
                """Deny unconditionally."""
                return False

        registry.register("guarded.tool", _echo, "Guarded", {}, permission_classes=[DenyAll])
        with pytest.raises(PermissionError):
            registry.dispatch(_request(), "guarded.tool", {})
