"""
PKG-26 — FRIESE_MCP_TOOL_HINTS setting for dispatcher help text.

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

from friese_mcp.backends.group_dispatcher import build_group_help, make_group_invoke
from friese_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_group_dispatcher.py conventions)
# ---------------------------------------------------------------------------


def _stub_tool(value: str) -> Any:
    """Return a callable that echoes its tag."""

    def _fn(arguments: dict[str, Any], request: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
        return {"called": value}

    return _fn


@pytest.fixture()
def registry() -> ToolRegistry:
    """Registry with device and rack tools."""
    reg = ToolRegistry()
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    for name in ("device_list", "device_create", "rack_list"):
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
        hints = {"device_create": "Requires extras.role to exist first."}
        result = build_group_help("dcim", ["device_list", "device_create"], registry, hints=hints)
        assert result["hints"] == hints

    def test_no_hints_key_when_hints_not_supplied(self, registry: ToolRegistry) -> None:
        """When hints is None, the 'hints' key is absent from the response."""
        result = build_group_help("dcim", ["device_list"], registry)
        assert "hints" not in result

    def test_empty_hints_dict_suppressed(self, registry: ToolRegistry) -> None:
        """An empty hints dict does not add a 'hints' key."""
        result = build_group_help("dcim", ["device_list"], registry, hints={})
        assert "hints" not in result

    def test_multiple_hints_all_present(self, registry: ToolRegistry) -> None:
        """All provided hints appear in the full-group response."""
        hints = {
            "device_create": "Needs role.",
            "rack_list": "Check location_type content_types.",
        }
        result = build_group_help(
            "dcim", ["device_list", "device_create", "rack_list"], registry, hints=hints
        )
        assert result["hints"]["device_create"] == "Needs role."
        assert result["hints"]["rack_list"] == "Check location_type content_types."


# ---------------------------------------------------------------------------
# build_group_help — resource-scoped help
# ---------------------------------------------------------------------------


class TestBuildGroupHelpResourceScoped:
    """Resource-scoped help (resource param) returns a tighter payload."""

    def test_resource_scoped_returns_resource_key(self, registry: ToolRegistry) -> None:
        """resource= produces 'resource' and 'actions' keys instead of 'resources'."""
        result = build_group_help(
            "dcim", ["device_list", "device_create", "rack_list"], registry, resource="device"
        )
        assert result["resource"] == "device"
        assert "actions" in result
        assert "resources" not in result

    def test_resource_scoped_actions_match_resource(self, registry: ToolRegistry) -> None:
        """Only actions for the requested resource are listed."""
        result = build_group_help(
            "dcim", ["device_list", "device_create", "rack_list"], registry, resource="device"
        )
        assert set(result["actions"]) == {"list", "create"}

    def test_resource_scoped_hints_filtered_to_resource(self, registry: ToolRegistry) -> None:
        """Only hints whose key starts with '{resource}.' are included."""
        hints = {
            "device_create": "Needs role.",
            "rack_list": "Check content_types.",
        }
        result = build_group_help(
            "dcim",
            ["device_list", "device_create", "rack_list"],
            registry,
            hints=hints,
            resource="device",
        )
        assert "device_create" in result["hints"]
        assert "rack_list" not in result["hints"]

    def test_resource_scoped_no_hints_when_no_matches(self, registry: ToolRegistry) -> None:
        """If no hints match this resource, 'hints' key is absent."""
        hints = {"rack_list": "Check content_types."}
        result = build_group_help(
            "dcim", ["device_list", "rack_list"], registry, hints=hints, resource="device"
        )
        assert "hints" not in result

    def test_resource_scoped_help_is_true(self, registry: ToolRegistry) -> None:
        """Resource-scoped payload still has help=True."""
        result = build_group_help("dcim", ["device_list"], registry, resource="device")
        assert result["help"] is True

    def test_unknown_resource_returns_empty_actions(self, registry: ToolRegistry) -> None:
        """An unrecognized resource name returns an empty actions list gracefully."""
        result = build_group_help("dcim", ["device_list"], registry, resource="ghost")
        assert result["actions"] == []


# ---------------------------------------------------------------------------
# make_group_invoke — FRIESE_MCP_TOOL_HINTS setting integration
# ---------------------------------------------------------------------------


class TestMakeGroupInvokeHints:
    """make_group_invoke reads FRIESE_MCP_TOOL_HINTS and surfaces them via help."""

    def test_hints_from_setting_appear_in_help(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """FRIESE_MCP_TOOL_HINTS entries for group tools appear in help payload."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device_list", "device_create"}),
            registry,
        )
        hints_setting = {"device_create": "Create extras.role first."}
        with patch("friese_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRIESE_MCP_TOOL_HINTS = hints_setting
            mock_settings.FRIESE_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert result["hints"]["device_create"] == "Create extras.role first."

    def test_hints_for_other_groups_filtered_out(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Hints for tools not in this group are not surfaced."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device_list"}),
            registry,
        )
        hints_setting = {
            "device_list": "DCIM hint.",
            "prefix.create": "IPAM hint — different group.",
        }
        with patch("friese_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRIESE_MCP_TOOL_HINTS = hints_setting
            mock_settings.FRIESE_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert "prefix.create" not in result.get("hints", {})
        assert result["hints"]["device_list"] == "DCIM hint."

    def test_no_hints_setting_produces_no_hints_key(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """When FRIESE_MCP_TOOL_HINTS is absent, 'hints' key is absent."""
        invoke = make_group_invoke("dcim", frozenset({"device_list"}), registry)
        with patch("friese_mcp.backends.group_dispatcher.settings") as mock_settings:
            del mock_settings.FRIESE_MCP_TOOL_HINTS
            mock_settings.FRIESE_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert "hints" not in result

    def test_none_hints_setting_produces_no_hints_key(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """FRIESE_MCP_TOOL_HINTS=None is treated as absent."""
        invoke = make_group_invoke("dcim", frozenset({"device_list"}), registry)
        with patch("friese_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRIESE_MCP_TOOL_HINTS = None
            mock_settings.FRIESE_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help"}, _req(rf))
        assert "hints" not in result

    def test_resource_scoped_help_with_hints(
        self, registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """resource= on action=help returns scoped payload including matching hints."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device_list", "device_create", "rack_list"}),
            registry,
        )
        hints_setting = {"device_create": "Needs role.", "rack_list": "Check location_type."}
        with patch("friese_mcp.backends.group_dispatcher.settings") as mock_settings:
            mock_settings.FRIESE_MCP_TOOL_HINTS = hints_setting
            mock_settings.FRIESE_MCP_TOOL_NAME_SEPARATOR = "_"
            result = invoke({"action": "help", "resource": "device"}, _req(rf))
        assert result["resource"] == "device"
        assert "device_create" in result["hints"]
        assert "rack_list" not in result.get("hints", {})
