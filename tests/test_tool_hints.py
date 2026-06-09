"""
PKG-26 — FRISIAN_MCP_TOOL_HINTS setting for dispatcher help text.

Operators can specify hint strings per tool name that surface in
group dispatcher ``action='help'`` responses, guiding agents on
prerequisite objects and setup steps.
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from frisian_mcp.backends.group_dispatcher import build_group_help, make_group_invoke
from frisian_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_group_dispatcher.py conventions)
# ---------------------------------------------------------------------------


def _stub_tool(value: str) -> Any:
    """Return a callable that echoes its tag."""

    def _fn(
        arguments: dict[str, Any], request: Any
    ) -> dict[str, Any]:  # pylint: disable=unused-argument
        return {"called": value}

    return _fn


@pytest.fixture()
def registry() -> ToolRegistry:
    """Registry with item and container tools."""
    reg = ToolRegistry()
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    for name in ("item_list", "item_create", "container_list"):
        tier = "read_write" if "create" in name else "read"
        reg.register(name, _stub_tool(name), "stub", schema, permission_tier=tier)
    return reg


@pytest.fixture()
def rf() -> RequestFactory:
    """Django RequestFactory."""
    return RequestFactory()


def _req(rf: RequestFactory, permission: str = "read_write") -> Any:
    """Build a stub request with a given permission."""
    req = rf.post("/mcp/", content_type="application/json")
    auth = MagicMock()
    auth.permission = permission
    req.auth = auth  # type: ignore[attr-defined]
    return req


# ---------------------------------------------------------------------------
# build_group_help — hints param
# ---------------------------------------------------------------------------


class TestBuildGroupHelpHints:
    """build_group_help surfaces hints in the response when supplied."""

    def test_hints_included_in_full_help(self, registry: ToolRegistry) -> None:
        """Hints dict is included in the full-group help payload."""
        hints = {"item_create": "Requires setup to exist first."}
        result = build_group_help("svc", ["item_list", "item_create"], registry, hints=hints)
        assert result["hints"] == hints

    def test_no_hints_key_when_hints_not_supplied(self, registry: ToolRegistry) -> None:
        """When hints is None, the 'hints' key is absent from the response."""
        result = build_group_help("svc", ["item_list"], registry)
        assert "hints" not in result

    def test_empty_hints_dict_suppressed(self, registry: ToolRegistry) -> None:
        """An empty hints dict does not add a 'hints' key."""
        result = build_group_help("svc", ["item_list"], registry, hints={})
        assert "hints" not in result

    def test_multiple_hints_all_present(self, registry: ToolRegistry) -> None:
        """All provided hints appear in the full-group response."""
        hints = {
            "item_create": "Needs role.",
            "container_list": "Check location_type content_types.",
        }
        result = build_group_help(
            "svc", ["item_list", "item_create", "container_list"], registry, hints=hints
        )
        assert result["hints"]["item_create"] == "Needs role."
        assert result["hints"]["container_list"] == "Check location_type content_types."


# ---------------------------------------------------------------------------
# build_group_help — resource-scoped help
# ---------------------------------------------------------------------------


class TestBuildGroupHelpResourceScoped:
    """Resource-scoped help (resource param) returns a tighter payload."""

    def test_resource_scoped_returns_resource_key(self, registry: ToolRegistry) -> None:
        """resource= produces 'resource' and 'actions' keys instead of 'resources'."""
        result = build_group_help(
            "svc", ["item_list", "item_create", "container_list"], registry, resource="item"
        )
        assert result["resource"] == "item"
        assert "actions" in result
        assert "resources" not in result

    def test_resource_scoped_actions_match_resource(self, registry: ToolRegistry) -> None:
        """Only actions for the requested resource are listed."""
        result = build_group_help(
            "svc", ["item_list", "item_create", "container_list"], registry, resource="item"
        )
        assert set(result["actions"]) == {"list", "create"}

    def test_resource_scoped_hints_filtered_to_resource(self, registry: ToolRegistry) -> None:
        """Only hints whose key starts with '{resource}.' are included."""
        hints = {
            "item_create": "Needs role.",
            "container_list": "Check content_types.",
        }
        result = build_group_help(
            "svc",
            ["item_list", "item_create", "container_list"],
            registry,
            hints=hints,
            resource="item",
        )
        assert "item_create" in result["hints"]
        assert "container_list" not in result["hints"]

    def test_resource_scoped_no_hints_when_no_matches(self, registry: ToolRegistry) -> None:
        """If no hints match this resource, 'hints' key is absent."""
        hints = {"container_list": "Check content_types."}
        result = build_group_help(
            "svc", ["item_list", "container_list"], registry, hints=hints, resource="item"
        )
        assert "hints" not in result

    def test_resource_scoped_help_is_true(self, registry: ToolRegistry) -> None:
        """Resource-scoped payload still has help=True."""
        result = build_group_help("svc", ["item_list"], registry, resource="item")
        assert result["help"] is True

    def test_unknown_resource_returns_empty_actions(self, registry: ToolRegistry) -> None:
        """An unrecognized resource name returns an empty actions list gracefully."""
        result = build_group_help("svc", ["item_list"], registry, resource="ghost")
        assert result["actions"] == []


# ---------------------------------------------------------------------------
# make_group_invoke — FRISIAN_MCP_TOOL_HINTS setting integration
# ---------------------------------------------------------------------------


class TestMakeGroupInvokeHints:
    """make_group_invoke reads FRISIAN_MCP_TOOL_HINTS and surfaces them via help."""

    def test_hints_from_setting_appear_in_help(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """FRISIAN_MCP_TOOL_HINTS entries for group tools appear in help payload."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list", "item_create"}),
            registry,
        )
        hints_setting = {"item_create": "Create setup first."}
        with patch("frisian_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_TOOL_HINTS = hints_setting
            mock_settings.FRISIAN_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert result["hints"]["item_create"] == "Create setup first."

    def test_hints_for_other_groups_filtered_out(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Hints for tools not in this group are not surfaced."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
            registry,
        )
        hints_setting = {
            "item_list": "Svc hint.",
            "prefix.create": "IPAM hint — different group.",
        }
        with patch("frisian_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_TOOL_HINTS = hints_setting
            mock_settings.FRISIAN_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert "prefix.create" not in result.get("hints", {})
        assert result["hints"]["item_list"] == "Svc hint."

    def test_no_hints_setting_produces_no_hints_key(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """When FRISIAN_MCP_TOOL_HINTS is absent, 'hints' key is absent."""
        invoke = make_group_invoke("svc", frozenset({"item_list"}), registry)
        with patch("frisian_mcp.backends.group_dispatcher.settings") as mock_settings:
            del mock_settings.FRISIAN_MCP_TOOL_HINTS
            mock_settings.FRISIAN_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert "hints" not in result

    def test_none_hints_setting_produces_no_hints_key(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """FRISIAN_MCP_TOOL_HINTS=None is treated as absent."""
        invoke = make_group_invoke("svc", frozenset({"item_list"}), registry)
        with patch("frisian_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_TOOL_HINTS = None
            mock_settings.FRISIAN_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert "hints" not in result

    def test_resource_scoped_help_with_hints(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """resource= on action=help returns scoped payload including matching hints."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list", "item_create", "container_list"}),
            registry,
        )
        hints_setting = {"item_create": "Needs role.", "container_list": "Check location_type."}
        with patch("frisian_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRISIAN_MCP_TOOL_HINTS = hints_setting
            mock_settings.FRISIAN_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help", "resource": "item"}, _req(rf))
        assert result["resource"] == "item"
        assert "item_create" in result["hints"]
        assert "container_list" not in result.get("hints", {})
