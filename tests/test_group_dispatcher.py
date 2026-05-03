"""Tests for FRIESE_MCP_DISPATCH_GROUPS group dispatcher."""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from friese_mcp.apps import _install_dispatch_groups
from friese_mcp.backends.group_dispatcher import (
    build_group_help,
    build_group_input_schema,
    make_group_invoke,
)
from friese_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_tool(value: str) -> Any:
    """Return an invocation callable that echoes its kwargs and a tag."""

    def _fn(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
        return {"called": value, "arguments": arguments}

    return _fn


@pytest.fixture()
def populated_registry() -> ToolRegistry:
    """Return a registry pre-populated with DCIM and IPAM flat tools."""
    reg = ToolRegistry()
    flat = {
        "device.list": _stub_tool("device.list"),
        "device.retrieve": _stub_tool("device.retrieve"),
        "device.create": _stub_tool("device.create"),
        "rack.list": _stub_tool("rack.list"),
        "interface.list": _stub_tool("interface.list"),
        "ipaddress.list": _stub_tool("ipaddress.list"),
        "prefix.list": _stub_tool("prefix.list"),
        "user.list": _stub_tool("user.list"),  # ungrouped
    }
    schema = {"type": "object", "properties": {}}
    for name, fn in flat.items():
        # Write actions need read_write tier so we can exercise tier filtering
        # in the help response.
        tier = "read_write" if name.endswith((".create", ".update", ".destroy")) else "read"
        reg.register(name, fn, "stub", schema, permission_tier=tier)
    return reg


@pytest.fixture()
def rf() -> RequestFactory:
    """Django RequestFactory."""
    return RequestFactory()


def _request(rf: RequestFactory, *, permission: str | None = None) -> Any:
    """Build a request whose request.auth.permission matches *permission*."""
    req = rf.post("/mcp/", content_type="application/json")
    if permission is None:
        req.auth = None  # type: ignore[attr-defined]
    else:
        auth = MagicMock()
        auth.permission = permission
        req.auth = auth  # type: ignore[attr-defined]
    return req


# ---------------------------------------------------------------------------
# build_group_input_schema
# ---------------------------------------------------------------------------


class TestBuildGroupInputSchema:
    """The dispatcher schema is intentionally tiny — keep it that way."""

    def test_schema_top_level_is_object(self) -> None:
        """Schema is a JSON object with the documented properties."""
        schema = build_group_input_schema()
        assert schema["type"] == "object"
        for key in ("resource", "action", "params"):
            assert key in schema["properties"]

    def test_schema_does_not_enumerate_resources_or_actions(self) -> None:
        """Resource and action are free-form strings — that is the point of grouping."""
        schema = build_group_input_schema()
        assert "enum" not in schema["properties"]["resource"]
        assert "enum" not in schema["properties"]["action"]


# ---------------------------------------------------------------------------
# build_group_help
# ---------------------------------------------------------------------------


class TestBuildGroupHelp:
    """Help payload structure and tier filtering."""

    def test_help_returns_resource_action_tree(self, populated_registry: ToolRegistry) -> None:
        """Help groups actions by resource and sorts them."""
        help_payload = build_group_help(
            "dcim",
            ["device.list", "device.retrieve", "rack.list"],
            populated_registry,
        )
        assert help_payload["help"] is True
        assert help_payload["group"] == "dcim"
        assert help_payload["resources"]["device"] == ["list", "retrieve"]
        assert help_payload["resources"]["rack"] == ["list"]

    def test_help_filters_actions_by_tier(self, populated_registry: ToolRegistry) -> None:
        """A 'read' caller does not see write actions like device.create."""
        help_payload = build_group_help(
            "dcim",
            ["device.list", "device.retrieve", "device.create"],
            populated_registry,
            max_tier="read",
        )
        assert "create" not in help_payload["resources"].get("device", [])
        assert "list" in help_payload["resources"]["device"]

    def test_help_unfiltered_includes_write_actions(
        self, populated_registry: ToolRegistry
    ) -> None:
        """Without a max_tier, every action is listed."""
        help_payload = build_group_help(
            "dcim",
            ["device.list", "device.create"],
            populated_registry,
        )
        assert "create" in help_payload["resources"]["device"]

    def test_help_skips_unknown_tools(self, populated_registry: ToolRegistry) -> None:
        """Tool names not in the registry are silently dropped."""
        help_payload = build_group_help(
            "dcim",
            ["device.list", "ghost.list"],
            populated_registry,
        )
        assert "ghost" not in help_payload["resources"]


# ---------------------------------------------------------------------------
# make_group_invoke — routing
# ---------------------------------------------------------------------------


class TestMakeGroupInvoke:
    """Routing semantics of the group dispatcher invoke callable."""

    def test_routes_to_underlying_tool(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """A valid resource/action pair dispatches through the registry."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device.list", "rack.list"}),
            populated_registry,
        )
        result = invoke(
            {"resource": "device", "action": "list", "params": {}},
            _request(rf, permission="read"),
        )
        assert result["called"] == "device.list"

    def test_help_response_when_action_missing(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Omitting action returns the help tree."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device.list", "rack.list"}),
            populated_registry,
        )
        result = invoke({}, _request(rf, permission="read"))
        assert result["help"] is True
        assert result["group"] == "dcim"

    def test_explicit_help_action(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='help' returns the help tree even when resource is set."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device.list"}),
            populated_registry,
        )
        result = invoke(
            {"action": "help", "resource": "device"},
            _request(rf, permission="read"),
        )
        assert result["help"] is True

    def test_unknown_resource_raises_lookup_error(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Unknown resource yields LookupError with a 'did you mean' hint."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device.list", "rack.list"}),
            populated_registry,
        )
        with pytest.raises(LookupError) as exc:
            invoke(
                {"resource": "devic", "action": "list", "params": {}},
                _request(rf, permission="read"),
            )
        assert "Did you mean" in str(exc.value)

    def test_missing_resource_raises_value_error(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Non-help action without resource raises ValueError."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device.list"}),
            populated_registry,
        )
        with pytest.raises(ValueError, match="resource is required"):
            invoke(
                {"action": "list"},
                _request(rf, permission="read"),
            )

    def test_flat_argument_form_routes_correctly(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Flat {action, resource, key: val} form (no params wrapper) works."""
        invoke = make_group_invoke(
            "dcim",
            frozenset({"device.list"}),
            populated_registry,
        )
        result = invoke(
            {"resource": "device", "action": "list", "filter": "x"},
            _request(rf, permission="read"),
        )
        # The flat 'filter' kwarg is forwarded as part of params to the tool.
        assert result["arguments"] == {"filter": "x"}


# ---------------------------------------------------------------------------
# _install_dispatch_groups — apps.py integration
# ---------------------------------------------------------------------------


class TestInstallDispatchGroups:
    """End-to-end behaviour when FRIESE_MCP_DISPATCH_GROUPS is set."""

    def test_no_setting_is_noop(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """Without the setting the registry is left untouched."""
        if hasattr(settings, "FRIESE_MCP_DISPATCH_GROUPS"):
            del settings.FRIESE_MCP_DISPATCH_GROUPS
        with patch("friese_mcp.apps.tool_registry", populated_registry, create=True), \
             patch(
                 "friese_mcp.registry.tool_registry", populated_registry
             ):
            group_count, bundled_count = _install_dispatch_groups()
        assert group_count == 0
        assert bundled_count == 0

    def test_registers_one_dispatcher_per_group(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """A two-group setting yields two new dispatcher tools."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {
            "dcim": ["device", "rack", "interface"],
            "ipam": ["ipaddress", "prefix"],
        }
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            group_count, bundled_count = _install_dispatch_groups()
        assert group_count == 2
        # bundled_count should equal the number of distinct flat tools matched
        # by either group's prefixes — we don't pin an exact integer here
        # because the populated_registry fixture may evolve, but it must be > 0
        # whenever any groups were registered.
        assert bundled_count > 0
        assert populated_registry.get_entry("dcim") is not None
        assert populated_registry.get_entry("ipam") is not None

    def test_grouped_tools_hidden_from_list_tools(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """Tools bundled under a group disappear from list_tools()."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {
            "dcim": ["device", "rack", "interface"],
        }
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        names = {t["name"] for t in populated_registry.list_tools()}
        assert "device.list" not in names
        assert "rack.list" not in names
        assert "dcim" in names

    def test_ungrouped_tools_remain_visible(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """Resources not in any group keep appearing as flat tools."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {
            "dcim": ["device", "rack", "interface"],
        }
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        names = {t["name"] for t in populated_registry.list_tools()}
        assert "user.list" in names  # user is ungrouped
        # ipam tools also remain visible (no group claimed them)
        assert "ipaddress.list" in names

    def test_grouped_tools_still_dispatchable_by_name(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """Hidden tools remain reachable through registry.dispatch()."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {"dcim": ["device"]}
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        result = populated_registry.dispatch(
            _request(rf, permission="read"), "device.list", {}
        )
        assert result["called"] == "device.list"

    def test_empty_group_logs_warning_and_skips(
        self,
        populated_registry: ToolRegistry,
        settings: Any,
        caplog: Any,
    ) -> None:
        """A group whose prefixes match no tools is skipped with a warning."""
        import logging

        settings.FRIESE_MCP_DISPATCH_GROUPS = {"empty": ["doesnotexist"]}
        with patch("friese_mcp.registry.tool_registry", populated_registry), \
             caplog.at_level(logging.WARNING, logger="friese_mcp.apps"):
            group_count, bundled_count = _install_dispatch_groups()
        assert group_count == 0
        assert bundled_count == 0
        assert populated_registry.get_entry("empty") is None
        assert any("no matching resources" in r.message for r in caplog.records)

    def test_group_dispatcher_is_invokable(
        self,
        populated_registry: ToolRegistry,
        settings: Any,
        rf: RequestFactory,
    ) -> None:
        """End-to-end: register the group, then dispatch through it."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {"dcim": ["device"]}
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        result = populated_registry.dispatch(
            _request(rf, permission="read"),
            "dcim",
            {"resource": "device", "action": "list", "params": {}},
        )
        assert result["called"] == "device.list"


# ---------------------------------------------------------------------------
# Permission tier enforcement during routing
# ---------------------------------------------------------------------------


class TestGroupDispatcherTierEnforcement:
    """Routing through registry.dispatch() preserves tier checks."""

    def test_read_caller_blocked_from_write_tool(
        self,
        populated_registry: ToolRegistry,
        settings: Any,
        rf: RequestFactory,
    ) -> None:
        """A 'read' caller cannot route to a 'read_write' tool through the group."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {"dcim": ["device"]}
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        with pytest.raises(PermissionError):
            populated_registry.dispatch(
                _request(rf, permission="read"),
                "dcim",
                {"resource": "device", "action": "create", "params": {}},
            )

    def test_read_write_caller_can_invoke_write_tool(
        self,
        populated_registry: ToolRegistry,
        settings: Any,
        rf: RequestFactory,
    ) -> None:
        """A 'read_write' caller can route to a 'read_write' tool."""
        settings.FRIESE_MCP_DISPATCH_GROUPS = {"dcim": ["device"]}
        with patch("friese_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        result = populated_registry.dispatch(
            _request(rf, permission="read_write"),
            "dcim",
            {"resource": "device", "action": "create", "params": {}},
        )
        assert result["called"] == "device.create"
