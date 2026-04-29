"""Tests for permission-tier filtering across decorators, dispatcher, and tools/list."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings

from friese_mcp.decorators import mcp_action, mcp_dispatcher, mcp_tool
from friese_mcp.registry import ToolRegistry
from friese_mcp.views import McpEndpointView, _get_token_permission

_rf = RequestFactory()
_view = McpEndpointView.as_view()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tools_list_request(token: Any = None) -> Any:
    """Build a tools/list JSON-RPC POST, optionally with a mock auth object."""
    req = _rf.post(
        "/mcp/",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
        content_type="application/json",
    )
    if token is not None:
        req.auth = token  # type: ignore[attr-defined]
    return req


def _fake_auth(permission: str) -> Any:
    """Return a minimal auth object with a permission attribute."""

    class _Auth:
        pass

    a = _Auth()
    a.permission = permission  # type: ignore[attr-defined]
    return a


def _isolated_registry(*names_and_tiers: tuple[str, str]) -> ToolRegistry:
    """Build an isolated registry with tools at specified tiers."""
    reg = ToolRegistry()
    for name, tier in names_and_tiers:
        reg.register(name, lambda a, r: {"ok": True}, f"Tool {name}", {}, permission_tier=tier)
    return reg


# ---------------------------------------------------------------------------
# @mcp_action permission_tier from write/admin kwargs
# ---------------------------------------------------------------------------


class TestMcpActionPermissionTier:
    """@mcp_action(write=True) / (admin=True) sets permission_tier on ActionEntry."""

    def test_default_action_is_read_tier(self) -> None:
        """@mcp_action with no write/admin kwargs defaults to permission_tier='read'."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("d1", description="Test dispatcher.")
            class _D:
                @mcp_action("ping", description="Ping.")
                def ping(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {}

        entry = reg.get_entry("d1")
        assert entry is not None
        assert _D.ping._mcp_action_meta["permission_tier"] == "read"  # type: ignore[attr-defined]

    def test_write_true_sets_read_write_tier(self) -> None:
        """@mcp_action(write=True) sets permission_tier='read_write' on the action."""

        class _D:
            @mcp_action("create", description="Create.", write=True)
            def create(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                return {}

        assert _D.create._mcp_action_meta["permission_tier"] == "read_write"  # type: ignore[attr-defined]

    def test_admin_true_sets_admin_tier(self) -> None:
        """@mcp_action(admin=True) sets permission_tier='admin' on the action."""

        class _D:
            @mcp_action("nuke", description="Nuke.", admin=True)
            def nuke(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                return {}

        assert _D.nuke._mcp_action_meta["permission_tier"] == "admin"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# @mcp_tool permission_tier from write/admin kwargs
# ---------------------------------------------------------------------------


class TestMcpToolPermissionTier:
    """@mcp_tool(write=True) / (admin=True) registers with the correct permission_tier."""

    def test_write_true_registers_read_write_tier(self) -> None:
        """@mcp_tool(write=True) calls register with permission_tier='read_write'."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="write.tool", description="Write tool.", input_schema={}, write=True)
            def _fn(_a: Any, _r: Any) -> dict[str, Any]:
                return {}

        entry = reg.get_entry("write.tool")
        assert entry is not None
        assert entry.permission_tier == "read_write"

    def test_admin_true_registers_admin_tier(self) -> None:
        """@mcp_tool(admin=True) calls register with permission_tier='admin'."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="admin.tool", description="Admin tool.", input_schema={}, admin=True)
            def _fn(_a: Any, _r: Any) -> dict[str, Any]:
                return {}

        entry = reg.get_entry("admin.tool")
        assert entry is not None
        assert entry.permission_tier == "admin"

    def test_no_kwargs_registers_read_tier(self) -> None:
        """@mcp_tool with no write/admin defaults to permission_tier='read'."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="read.tool", description="Read tool.", input_schema={})
            def _fn(_a: Any, _r: Any) -> dict[str, Any]:
                return {}

        entry = reg.get_entry("read.tool")
        assert entry is not None
        assert entry.permission_tier == "read"


# ---------------------------------------------------------------------------
# tools/list tier filtering — registry level
# ---------------------------------------------------------------------------


class TestToolsListTierFiltering:
    """registry.list_tools(max_tier=...) respects the permission tier rank."""

    def _make_registry(self) -> ToolRegistry:
        return _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )

    def test_no_max_tier_returns_all_tools(self) -> None:
        """list_tools(max_tier=None) returns all tools regardless of tier."""
        reg = self._make_registry()
        names = {t["name"] for t in reg.list_tools(max_tier=None)}
        assert names == {"read.tool", "write.tool", "admin.tool"}

    def test_read_tier_returns_only_read_tools(self) -> None:
        """list_tools(max_tier='read') returns only read-tier tools."""
        reg = self._make_registry()
        names = {t["name"] for t in reg.list_tools(max_tier="read")}
        assert "read.tool" in names
        assert "write.tool" not in names
        assert "admin.tool" not in names

    def test_read_write_tier_returns_read_and_write_tools(self) -> None:
        """list_tools(max_tier='read_write') returns read and read_write tools."""
        reg = self._make_registry()
        names = {t["name"] for t in reg.list_tools(max_tier="read_write")}
        assert "read.tool" in names
        assert "write.tool" in names
        assert "admin.tool" not in names

    def test_admin_tier_returns_all_tools(self) -> None:
        """list_tools(max_tier='admin') returns tools at all tiers."""
        reg = self._make_registry()
        names = {t["name"] for t in reg.list_tools(max_tier="admin")}
        assert names == {"read.tool", "write.tool", "admin.tool"}

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
    )
    def test_view_passes_max_tier_to_list_tools(self) -> None:
        """_handle_tools_list calls list_tools with max_tier from _get_token_permission."""
        reg = self._make_registry()
        with (
            patch("friese_mcp.views.tool_registry", reg),
            patch("friese_mcp.views._get_token_permission", return_value="read"),
        ):
            resp = _view(_tools_list_request())
        data = json.loads(resp.content)
        names = {t["name"] for t in data["result"]["tools"]}
        assert "read.tool" in names
        assert "write.tool" not in names
        assert "admin.tool" not in names


# ---------------------------------------------------------------------------
# _get_token_permission — unauthenticated tier and fallback behavior
# ---------------------------------------------------------------------------


class TestGetTokenPermission:
    """_get_token_permission returns the correct tier in every auth scenario."""

    def _req(self, auth: Any = None) -> Any:
        """Build a minimal request-like object."""

        class _R:
            pass

        r = _R()
        if auth is not None:
            r.auth = auth  # type: ignore[attr-defined]
        return r

    def test_no_auth_attr_returns_read_by_default(self) -> None:
        """Request with no .auth attribute defaults to 'read' (unauthenticated)."""
        req = self._req()
        assert _get_token_permission(req) == "read"

    def test_auth_none_returns_read_by_default(self) -> None:
        """request.auth=None (unauthenticated) defaults to 'read'."""
        req = self._req()
        req.auth = None  # type: ignore[attr-defined]
        assert _get_token_permission(req) == "read"

    def test_auth_without_permission_attr_returns_read(self) -> None:
        """Authenticated request whose auth object lacks .permission falls back to 'read'."""

        class _OldAuth:
            pass

        req = self._req(_OldAuth())
        assert _get_token_permission(req) == "read"

    def test_auth_with_permission_returns_it(self) -> None:
        """Authenticated request with .permission='admin' returns 'admin'."""
        req = self._req(_fake_auth("admin"))
        assert _get_token_permission(req) == "admin"

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="admin")
    def test_unauthenticated_tier_setting_overrides_default(self) -> None:
        """FRIESE_MCP_UNAUTHENTICATED_TIER overrides the 'read' default for anon requests."""
        req = self._req()
        req.auth = None  # type: ignore[attr-defined]
        assert _get_token_permission(req) == "admin"

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
    )
    def test_unauthenticated_view_request_defaults_to_read_tier(self) -> None:
        """Unauthenticated tools/list only returns read-tier tools by default."""
        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        with patch("friese_mcp.views.tool_registry", reg):
            resp = _view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert "read.tool" in names
        assert "write.tool" not in names
        assert "admin.tool" not in names

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="admin",
    )
    def test_unauthenticated_tier_admin_setting_exposes_all_tools(self) -> None:
        """FRIESE_MCP_UNAUTHENTICATED_TIER='admin' makes all tools visible unauthenticated."""
        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        with patch("friese_mcp.views.tool_registry", reg):
            resp = _view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert names == {"read.tool", "write.tool", "admin.tool"}


# ---------------------------------------------------------------------------
# Dispatcher action-level permission tier enforcement
# ---------------------------------------------------------------------------


class TestDispatcherActionPermissionTier:
    """Dispatcher actions enforce permission_tier at execution time."""

    def _build_registry_with_dispatcher(self) -> ToolRegistry:
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("tools", description="Mixed-tier dispatcher.")
            class _Tools:
                @mcp_action("list", description="List tools.")
                def list_items(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {"items": []}

                @mcp_action("delete", description="Delete tool.", write=True)
                def delete_item(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {"deleted": True}

                @mcp_action("nuke", description="Nuke everything.", admin=True)
                def nuke_all(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {"nuked": True}

        return reg

    def test_read_token_can_call_read_action(self) -> None:
        """Read-tier token can invoke a read-tier action."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = _fake_auth("read")  # type: ignore[attr-defined]
        result = reg.dispatch(request, "tools", {"action": "list"})
        assert result == {"items": []}

    def test_read_token_cannot_call_write_action(self) -> None:
        """Read-tier token calling a read_write action raises PermissionError."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = _fake_auth("read")  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="read_write"):
            reg.dispatch(request, "tools", {"action": "delete"})

    def test_read_write_token_cannot_call_admin_action(self) -> None:
        """Read_write token calling an admin action raises PermissionError."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = _fake_auth("read_write")  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="admin"):
            reg.dispatch(request, "tools", {"action": "nuke"})

    def test_admin_token_can_call_any_action(self) -> None:
        """Admin token can call actions at any tier."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = _fake_auth("admin")  # type: ignore[attr-defined]
        assert reg.dispatch(request, "tools", {"action": "list"}) == {"items": []}
        assert reg.dispatch(request, "tools", {"action": "delete"}) == {"deleted": True}
        assert reg.dispatch(request, "tools", {"action": "nuke"}) == {"nuked": True}

    def test_dispatcher_itself_always_visible_in_tools_list(self) -> None:
        """@mcp_dispatcher is always registered with tier='read' (navigation tool)."""
        reg = self._build_registry_with_dispatcher()
        entry = reg.get_entry("tools")
        assert entry is not None
        assert entry.permission_tier == "read"
