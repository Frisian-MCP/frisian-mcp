"""Tests for @mcp_tool and @mcp_ignore decorators."""

# pylint: disable=protected-access
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.permissions import IsAuthenticated

from friese_mcp.decorators import mcp_ignore, mcp_tool
from friese_mcp.registry import ToolRegistry, tool_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(_arguments: dict[str, Any], _request: Any) -> None:
    """No-op tool callable used in decorator tests."""


# ---------------------------------------------------------------------------
# @mcp_tool
# ---------------------------------------------------------------------------


class TestMcpTool:
    """Tests for the @mcp_tool decorator."""

    def test_registers_into_global_registry(self) -> None:
        """@mcp_tool registers the decorated function in the global tool_registry."""
        schema: dict[str, Any] = {"type": "object"}

        with patch.object(tool_registry, "register") as mock_register:

            @mcp_tool(name="test.decorated", description="Test", input_schema=schema)
            def _decorated(_arguments: dict[str, Any], _request: Any) -> None:
                """Decorated test tool placeholder."""

            mock_register.assert_called_once_with(
                name="test.decorated",
                fn=_decorated,
                description="Test",
                input_schema=schema,
                permission_classes=None,
            )

    def test_returns_original_callable(self) -> None:
        """@mcp_tool returns the original function unmodified."""
        isolated = ToolRegistry()

        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_tool(name="ret.test", description="Return test", input_schema={})
            def _fn(_arguments: dict[str, Any], _request: Any) -> str:
                """Test function."""
                return "result"

            assert _fn({}, None) == "result"

    def test_with_permission_classes(self) -> None:
        """@mcp_tool forwards permission_classes to the registry."""
        isolated = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_tool(
                name="perm.test",
                description="Perm test",
                input_schema={},
                permission_classes=[IsAuthenticated],
            )
            def _secured(_arguments: dict[str, Any], _request: Any) -> None:
                """Secured tool placeholder."""

        tools = isolated.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "perm.test"

    def test_tool_callable_via_dispatch(self) -> None:
        """A tool registered via @mcp_tool can be dispatched successfully."""
        isolated = ToolRegistry()

        with patch("friese_mcp.decorators.tool_registry", isolated):

            @mcp_tool(name="dispatch.test", description="Dispatch", input_schema={})
            def _ret(_arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
                """Return a fixed result."""
                return {"ok": True}

        req = MagicMock()
        result = isolated.dispatch(req, "dispatch.test", {})
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# @mcp_ignore
# ---------------------------------------------------------------------------


class TestMcpIgnore:
    """Tests for the @mcp_ignore decorator."""

    def test_sets_mcp_ignore_attribute_on_function(self) -> None:
        """@mcp_ignore sets _mcp_ignore = True on a function."""

        @mcp_ignore
        def _hidden(_arguments: dict[str, Any], _request: Any) -> None:
            """Hidden function."""

        assert getattr(_hidden, "_mcp_ignore", False) is True

    def test_sets_mcp_ignore_attribute_on_class(self) -> None:
        """@mcp_ignore sets _mcp_ignore = True on a class."""

        @mcp_ignore
        class _HiddenView:
            """Hidden view class."""

        assert getattr(_HiddenView, "_mcp_ignore", False) is True

    def test_returns_original_object_unchanged(self) -> None:
        """@mcp_ignore returns the original object (identity preserved)."""
        original_fn = _noop
        result = mcp_ignore(original_fn)
        assert result is original_fn

    def test_decorated_function_still_callable(self) -> None:
        """A function decorated with @mcp_ignore remains callable."""

        @mcp_ignore
        def _fn(_arguments: dict[str, Any], _request: Any) -> str:
            """Return ok."""
            return "ok"

        assert _fn({}, None) == "ok"

    def test_without_decorator_no_ignore_flag(self) -> None:
        """A plain function does not have _mcp_ignore set."""

        def _plain(_arguments: dict[str, Any], _request: Any) -> None:
            """Plain function."""

        assert getattr(_plain, "_mcp_ignore", False) is False

    @pytest.mark.parametrize("value", [True])
    def test_ignore_flag_is_true_not_truthy(self, value: bool) -> None:
        """_mcp_ignore is exactly True, not just truthy."""

        @mcp_ignore
        def _fn2(_arguments: dict[str, Any], _request: Any) -> None:
            """Fn2 placeholder."""

        assert _fn2._mcp_ignore is value  # type: ignore[attr-defined]
