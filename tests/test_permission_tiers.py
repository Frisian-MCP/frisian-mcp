"""Tests for permission-tier filtering across decorators, dispatcher, and tools/list."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings

from friese_mcp.decorators import mcp_action, mcp_dispatcher, mcp_tool
from friese_mcp.registry import ToolRegistry, _apply_max_tier_cap
from friese_mcp.views import McpView, _get_token_permission

_rf = RequestFactory()
_view = McpView.as_view()


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

    def test_unauthenticated_cannot_call_write_action(self) -> None:
        """Unauthenticated caller (request.auth is None) cannot invoke a write action."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="read_write"):
            reg.dispatch(request, "tools", {"action": "delete"})

    def test_unauthenticated_cannot_call_admin_action(self) -> None:
        """Unauthenticated caller (request.auth is None) cannot invoke an admin action."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="admin"):
            reg.dispatch(request, "tools", {"action": "nuke"})

    def test_unauthenticated_can_call_read_action(self) -> None:
        """Unauthenticated caller can still invoke a read-tier action (default tier)."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        assert reg.dispatch(request, "tools", {"action": "list"}) == {"items": []}

    def test_request_without_auth_attr_cannot_call_write_action(self) -> None:
        """Request lacking the .auth attribute entirely is treated as unauthenticated."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        # Do not set request.auth at all — _resolve_request_tier must handle this.
        with pytest.raises(PermissionError, match="read_write"):
            reg.dispatch(request, "tools", {"action": "delete"})

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="admin")
    def test_unauthenticated_tier_admin_setting_allows_write_action(self) -> None:
        """FRIESE_MCP_UNAUTHENTICATED_TIER='admin' lifts the unauthenticated tier ceiling."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        assert reg.dispatch(request, "tools", {"action": "delete"}) == {"deleted": True}
        assert reg.dispatch(request, "tools", {"action": "nuke"}) == {"nuked": True}

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="read_write")
    def test_unauthenticated_tier_read_write_allows_write_blocks_admin(self) -> None:
        """FRIESE_MCP_UNAUTHENTICATED_TIER='read_write' allows write but not admin."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        assert reg.dispatch(request, "tools", {"action": "delete"}) == {"deleted": True}
        with pytest.raises(PermissionError, match="admin"):
            reg.dispatch(request, "tools", {"action": "nuke"})


# ---------------------------------------------------------------------------
# Plain @mcp_tool / @mcp_heavy execution-time tier enforcement
# ---------------------------------------------------------------------------


class TestPlainToolExecutionTierEnforcement:
    """
    Plain @mcp_tool(write/admin) tools are blocked at dispatch time below tier.

    Previously the only enforcement was filtering the tool out of
    ``tools/list``, which meant any caller that knew (or guessed) the tool name
    could still invoke it.  Now ``ToolRegistry.dispatch`` re-checks the tier so
    the listing filter cannot be bypassed by name guessing.
    """

    def _build_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_tool(name="things.read", description="Read.", input_schema={"type": "object"})
            def _r(_a: Any, _r2: Any) -> dict[str, Any]:
                return {"ok": "read"}

            @mcp_tool(
                name="things.write",
                description="Write.",
                input_schema={"type": "object"},
                write=True,
            )
            def _w(_a: Any, _r2: Any) -> dict[str, Any]:
                return {"ok": "write"}

            @mcp_tool(
                name="things.admin",
                description="Admin.",
                input_schema={"type": "object"},
                admin=True,
            )
            def _ad(_a: Any, _r2: Any) -> dict[str, Any]:
                return {"ok": "admin"}

        return reg

    def test_unauthenticated_cannot_call_write_tool(self) -> None:
        """Unauthenticated caller cannot invoke a write-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="read_write"):
            reg.dispatch(request, "things.write", {})

    def test_unauthenticated_cannot_call_admin_tool(self) -> None:
        """Unauthenticated caller cannot invoke an admin-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="admin"):
            reg.dispatch(request, "things.admin", {})

    def test_unauthenticated_can_call_read_tool(self) -> None:
        """Unauthenticated caller can still invoke a read-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        assert reg.dispatch(request, "things.read", {}) == {"ok": "read"}

    def test_read_token_cannot_call_write_tool(self) -> None:
        """Read-tier token cannot invoke a write-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = _fake_auth("read")  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="read_write"):
            reg.dispatch(request, "things.write", {})

    def test_read_write_token_can_call_write_tool(self) -> None:
        """read_write token can invoke a write-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = _fake_auth("read_write")  # type: ignore[attr-defined]
        assert reg.dispatch(request, "things.write", {}) == {"ok": "write"}

    def test_read_write_token_cannot_call_admin_tool(self) -> None:
        """read_write token cannot invoke an admin-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = _fake_auth("read_write")  # type: ignore[attr-defined]
        with pytest.raises(PermissionError, match="admin"):
            reg.dispatch(request, "things.admin", {})

    def test_admin_token_can_call_admin_tool(self) -> None:
        """Admin token can invoke an admin-tier @mcp_tool."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = _fake_auth("admin")  # type: ignore[attr-defined]
        assert reg.dispatch(request, "things.admin", {}) == {"ok": "admin"}

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="admin")
    def test_unauthenticated_tier_admin_lifts_block(self) -> None:
        """FRIESE_MCP_UNAUTHENTICATED_TIER='admin' allows unauthenticated write/admin calls."""
        reg = self._build_registry()
        request = _rf.get("/")
        request.auth = None  # type: ignore[attr-defined]
        assert reg.dispatch(request, "things.write", {}) == {"ok": "write"}
        assert reg.dispatch(request, "things.admin", {}) == {"ok": "admin"}


# ---------------------------------------------------------------------------
# Dispatcher action-list filtering in tools/list (no tier-leak via inputSchema)
# ---------------------------------------------------------------------------


class TestDispatcherInputSchemaTierFiltering:
    """tools/list must not leak write/admin action names to lower-tier callers."""

    def _build_registry_with_mixed_dispatcher(self) -> ToolRegistry:
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("ops", description="Mixed-tier ops dispatcher.")
            class _Ops:
                @mcp_action("status", description="Read status.")
                def status(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {"ok": True}

                @mcp_action("create", description="Create resource.", write=True)
                def create(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {"created": True}

                @mcp_action("purge", description="Purge everything.", admin=True)
                def purge(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {"purged": True}

        return reg

    def _action_enum(self, tools: list[dict[str, Any]], name: str) -> list[str]:
        for t in tools:
            if t["name"] == name:
                enum: list[str] = t["inputSchema"]["properties"]["action"]["enum"]
                return enum
        raise AssertionError(f"Tool {name!r} not found in tools list")

    def test_unauthenticated_read_tier_sees_only_read_actions(self) -> None:
        """Read-tier callers must see only read-tier action names in dispatcher enum."""
        reg = self._build_registry_with_mixed_dispatcher()
        tools = reg.list_tools(max_tier="read")
        actions = self._action_enum(tools, "ops")
        assert actions == ["status"]

    def test_read_write_tier_sees_read_and_write_actions(self) -> None:
        """read_write callers see read + read_write actions but not admin."""
        reg = self._build_registry_with_mixed_dispatcher()
        tools = reg.list_tools(max_tier="read_write")
        actions = self._action_enum(tools, "ops")
        assert set(actions) == {"status", "create"}

    def test_admin_tier_sees_all_actions(self) -> None:
        """Admin callers see every action name."""
        reg = self._build_registry_with_mixed_dispatcher()
        tools = reg.list_tools(max_tier="admin")
        actions = self._action_enum(tools, "ops")
        assert set(actions) == {"status", "create", "purge"}

    def test_no_max_tier_returns_full_action_enum(self) -> None:
        """list_tools(max_tier=None) returns the full enum (legacy / cache-key path)."""
        reg = self._build_registry_with_mixed_dispatcher()
        tools = reg.list_tools(max_tier=None)
        actions = self._action_enum(tools, "ops")
        assert set(actions) == {"status", "create", "purge"}

    def test_dispatcher_with_only_privileged_actions_hidden_from_lower_tier(
        self,
    ) -> None:
        """A dispatcher whose every action is admin-only is hidden from read callers."""
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("admin_only", description="Admin-only navigation.")
            class _AdminOnly:
                @mcp_action("a", description="A.", admin=True)
                def a(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {}

                @mcp_action("b", description="B.", admin=True)
                def b(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {}

        names_read = {t["name"] for t in reg.list_tools(max_tier="read")}
        names_admin = {t["name"] for t in reg.list_tools(max_tier="admin")}
        assert "admin_only" not in names_read
        assert "admin_only" in names_admin

    def test_registered_input_schema_unchanged_for_execution(self) -> None:
        """The stored entry.input_schema keeps the full enum for dispatch validation."""
        reg = self._build_registry_with_mixed_dispatcher()
        entry = reg.get_entry("ops")
        assert entry is not None
        enum: list[str] = entry.input_schema["properties"]["action"]["enum"]
        assert set(enum) == {"status", "create", "purge"}


# ---------------------------------------------------------------------------
# Dispatcher action="help" filtering by caller tier
# ---------------------------------------------------------------------------


class TestDispatcherHelpResponseTierFiltering:
    """action='help' must not enumerate actions the caller is not allowed to see."""

    def _build_registry_with_dispatcher(self) -> ToolRegistry:
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("widgets", description="Widget operations.")
            class _Widgets:
                @mcp_action("get", description="Get widget.")
                def get(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {}

                @mcp_action("update", description="Update widget.", write=True)
                def update(self, request: Any, params: dict[str, Any]) -> dict[str, Any]:
                    return {}

                @mcp_action("delete_all", description="Delete all.", admin=True)
                def delete_all(
                    self, request: Any, params: dict[str, Any]
                ) -> dict[str, Any]:
                    return {}

        return reg

    def _help_action_names(self, reg: ToolRegistry, auth: Any) -> set[str]:
        request = _rf.get("/")
        if auth is not None:
            request.auth = auth  # type: ignore[attr-defined]
        result = reg.dispatch(request, "widgets", {"action": "help"})
        return {a["name"] for a in result["actions"]}

    def test_unauthenticated_help_returns_only_read_actions(self) -> None:
        """request.auth=None → help lists only read-tier actions."""
        reg = self._build_registry_with_dispatcher()
        names = self._help_action_names(reg, auth=None)
        assert names == {"get"}

    def test_read_write_help_returns_read_and_write_actions(self) -> None:
        """read_write token → help lists read + read_write actions."""
        reg = self._build_registry_with_dispatcher()
        names = self._help_action_names(reg, auth=_fake_auth("read_write"))
        assert names == {"get", "update"}

    def test_admin_help_returns_all_actions(self) -> None:
        """Admin token → help lists every action."""
        reg = self._build_registry_with_dispatcher()
        names = self._help_action_names(reg, auth=_fake_auth("admin"))
        assert names == {"get", "update", "delete_all"}

    @override_settings(FRIESE_MCP_UNAUTHENTICATED_TIER="admin")
    def test_unauthenticated_tier_setting_overrides_help_filter(self) -> None:
        """FRIESE_MCP_UNAUTHENTICATED_TIER='admin' exposes all actions in help."""
        reg = self._build_registry_with_dispatcher()
        names = self._help_action_names(reg, auth=None)
        assert names == {"get", "update", "delete_all"}

    def test_omitted_action_returns_filtered_help(self) -> None:
        """Omitting 'action' is equivalent to 'help' and must also be tier-filtered."""
        reg = self._build_registry_with_dispatcher()
        request = _rf.get("/")
        request.auth = _fake_auth("read")  # type: ignore[attr-defined]
        result = reg.dispatch(request, "widgets", {})
        assert {a["name"] for a in result["actions"]} == {"get"}

    def test_help_response_does_not_leak_privileged_action_descriptions(self) -> None:
        """
        Help response must not embed descriptions of filtered-out actions.

        Regression: filtering must remove BOTH the enum entry and the descriptive
        text for write/admin actions. A read-tier caller who sees the help payload
        should not be able to recover privileged action names from any string
        field in the response (name, description, params, or input_schema).
        """
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("ops", description="Mixed-tier operations.")
            class _Ops:  # pylint: disable=unused-variable
                @mcp_action(
                    "status",
                    description="Read status.",
                    params={"id": "Resource id."},
                )
                def status(
                    self, request: Any, _params: dict[str, Any]
                ) -> dict[str, Any]:
                    """Read-tier action; body is irrelevant for this test."""
                    return {}

                @mcp_action(
                    "annihilate",
                    description="Annihilate the resource permanently.",
                    params={"target": "Annihilate this resource."},
                    write=True,
                )
                def annihilate(
                    self, request: Any, _params: dict[str, Any]
                ) -> dict[str, Any]:
                    """Write-tier action; must be filtered out for read callers."""
                    return {}

                @mcp_action(
                    "godmode",
                    description="Grant godmode privileges.",
                    params={"user": "Grant godmode to user."},
                    admin=True,
                )
                def godmode(
                    self, request: Any, _params: dict[str, Any]
                ) -> dict[str, Any]:
                    """Admin-tier action; must be filtered out for read callers."""
                    return {}

        request = _rf.get("/")  # auth=None → read tier
        result = reg.dispatch(request, "ops", {"action": "help"})
        payload = json.dumps(result)
        # Privileged action names and descriptions must not appear anywhere.
        assert "annihilate" not in payload, "write-tier action name leaked in help"
        assert "Annihilate" not in payload, "write-tier description leaked in help"
        assert "godmode" not in payload, "admin-tier action name leaked in help"
        assert "godmode privileges" not in payload, "admin-tier description leaked"

    def test_tools_list_input_schema_description_is_generic(self) -> None:
        """
        Verify the 'action' parameter description does not name any actions.

        Regression: a generic boilerplate description ('Operation to perform...')
        is required so the schema does not enumerate filtered-out names through
        a side channel. If a future contributor changes the boilerplate to
        include action examples, this test fails.
        """
        reg = ToolRegistry()
        with patch("friese_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("svc", description="Service operations.")
            class _Svc:  # pylint: disable=unused-variable
                @mcp_action("ping", description="Ping.")
                def ping(
                    self, request: Any, _params: dict[str, Any]
                ) -> dict[str, Any]:
                    """Read-tier action used to prove the dispatcher is built."""
                    return {}

                @mcp_action("destroy", description="Destroy.", admin=True)
                def destroy(
                    self, request: Any, _params: dict[str, Any]
                ) -> dict[str, Any]:
                    """Admin-tier action whose name must not leak into schema text."""
                    return {}

        tools = reg.list_tools(max_tier="read")
        svc = next(t for t in tools if t["name"] == "svc")
        action_desc = svc["inputSchema"]["properties"]["action"]["description"]
        # The description must not name any specific action — must stay generic.
        assert "destroy" not in action_desc
        assert "ping" not in action_desc


# ---------------------------------------------------------------------------
# FRIESE_MCP_MAX_TIER endpoint-level cap
# ---------------------------------------------------------------------------


class TestEndpointMaxTierCap:
    r"""
    FRIESE_MCP_MAX_TIER caps the effective permission tier at an endpoint.

    The cap is stamped on ``request._mcp_max_tier`` by :meth:`McpView.post`
    and applied inside ``_resolve_request_tier`` via ``_apply_max_tier_cap``.
    A subclass can override ``_effective_max_tier`` to disable or change the
    cap per-endpoint (the auto-registered protected endpoint does this).
    """

    # ------------------------------------------------------------------
    # _apply_max_tier_cap — unit tests for the clamping primitive
    # ------------------------------------------------------------------

    def _req_with_cap(self, cap: str | None) -> Any:
        class _R:
            pass

        r = _R()
        r._mcp_max_tier = cap  # type: ignore[attr-defined]
        return r

    def test_no_cap_returns_tier_unchanged(self) -> None:
        """_apply_max_tier_cap with cap=None is a no-op for any tier."""
        req = self._req_with_cap(None)
        assert _apply_max_tier_cap("admin", req) == "admin"
        assert _apply_max_tier_cap("read_write", req) == "read_write"
        assert _apply_max_tier_cap("read", req) == "read"

    def test_cap_clamps_higher_tier_down(self) -> None:
        """When the token tier exceeds the cap, the cap is returned."""
        req = self._req_with_cap("read")
        assert _apply_max_tier_cap("read_write", req) == "read"
        assert _apply_max_tier_cap("admin", req) == "read"

    def test_cap_does_not_elevate_lower_tier(self) -> None:
        """When cap > token tier, the token tier is returned unchanged."""
        req = self._req_with_cap("admin")
        assert _apply_max_tier_cap("read", req) == "read"
        assert _apply_max_tier_cap("read_write", req) == "read_write"

    def test_cap_equal_to_tier_is_no_op(self) -> None:
        """When cap == token tier, the tier is returned unchanged."""
        req = self._req_with_cap("read_write")
        assert _apply_max_tier_cap("read_write", req) == "read_write"

    def test_read_write_cap_clamps_admin_but_not_read_write(self) -> None:
        """read_write cap clamps admin but leaves read and read_write alone."""
        req = self._req_with_cap("read_write")
        assert _apply_max_tier_cap("admin", req) == "read_write"
        assert _apply_max_tier_cap("read_write", req) == "read_write"
        assert _apply_max_tier_cap("read", req) == "read"

    # ------------------------------------------------------------------
    # _get_token_permission — cap applied through full resolution chain
    # ------------------------------------------------------------------

    def _token_req(self, permission: str, max_tier: str | None = None) -> Any:
        class _Auth:
            pass

        class _R:
            pass

        auth = _Auth()
        auth.permission = permission  # type: ignore[attr-defined]
        r = _R()
        r.auth = auth  # type: ignore[attr-defined]
        if max_tier is not None:
            r._mcp_max_tier = max_tier  # type: ignore[attr-defined]
        return r

    def test_read_write_token_capped_to_read(self) -> None:
        """read_write token with _mcp_max_tier='read' resolves to 'read'."""
        req = self._token_req("read_write", max_tier="read")
        assert _get_token_permission(req) == "read"

    def test_admin_token_capped_to_read_write(self) -> None:
        """admin token with _mcp_max_tier='read_write' resolves to 'read_write'."""
        req = self._token_req("admin", max_tier="read_write")
        assert _get_token_permission(req) == "read_write"

    def test_admin_token_capped_to_read(self) -> None:
        """admin token with _mcp_max_tier='read' resolves to 'read'."""
        req = self._token_req("admin", max_tier="read")
        assert _get_token_permission(req) == "read"

    def test_cap_does_not_elevate_read_token(self) -> None:
        """admin cap does not elevate a read token."""
        req = self._token_req("read", max_tier="admin")
        assert _get_token_permission(req) == "read"

    def test_no_max_tier_attribute_returns_full_token_tier(self) -> None:
        """Absent _mcp_max_tier attribute leaves token tier intact."""
        req = self._token_req("read_write")
        assert _get_token_permission(req) == "read_write"

    # ------------------------------------------------------------------
    # Via view — FRIESE_MCP_MAX_TIER setting caps tools/list response
    # ------------------------------------------------------------------

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="read_write",
        FRIESE_MCP_MAX_TIER="read",
    )
    def test_setting_caps_read_write_caller_to_read_tools_only(self) -> None:
        """FRIESE_MCP_MAX_TIER='read' shows only read tools even for a read_write caller."""
        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        view = McpView.as_view()
        with patch("friese_mcp.views.tool_registry", reg):
            resp = view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert "read.tool" in names
        assert "write.tool" not in names
        assert "admin.tool" not in names

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="read_write",
    )
    def test_omitting_max_tier_setting_exposes_full_tier_tools(self) -> None:
        """Without FRIESE_MCP_MAX_TIER, a read_write caller sees all read+write tools."""
        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        view = McpView.as_view()
        with patch("friese_mcp.views.tool_registry", reg):
            resp = view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert "read.tool" in names
        assert "write.tool" in names
        assert "admin.tool" not in names

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="admin",
        FRIESE_MCP_MAX_TIER="read_write",
    )
    def test_read_write_cap_on_admin_caller_shows_read_and_write_tools(self) -> None:
        """FRIESE_MCP_MAX_TIER='read_write' clamps admin caller to read+write tools."""
        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        view = McpView.as_view()
        with patch("friese_mcp.views.tool_registry", reg):
            resp = view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert "read.tool" in names
        assert "write.tool" in names
        assert "admin.tool" not in names

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="admin",
        FRIESE_MCP_MAX_TIER="read",
    )
    def test_read_cap_on_unauthenticated_admin_tier_shows_read_only(self) -> None:
        """FRIESE_MCP_MAX_TIER='read' caps even an unauthenticated admin-tier request."""
        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        view = McpView.as_view()
        with patch("friese_mcp.views.tool_registry", reg):
            resp = view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert names == {"read.tool"}

    # ------------------------------------------------------------------
    # _effective_max_tier subclass override — disables global cap
    # ------------------------------------------------------------------

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="read_write",
        FRIESE_MCP_MAX_TIER="read",
    )
    def test_subclass_override_none_disables_global_cap(self) -> None:
        """Subclass returning None from _effective_max_tier ignores FRIESE_MCP_MAX_TIER."""

        class _NoCap(McpView):
            def _effective_max_tier(self) -> str | None:
                return None

        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
        )
        view = _NoCap.as_view()
        with patch("friese_mcp.views.tool_registry", reg):
            resp = view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert "read.tool" in names
        assert "write.tool" in names  # cap was bypassed — full tier honored

    @override_settings(
        FRIESE_MCP_AUTHENTICATION_CLASSES=[],
        FRIESE_MCP_PERMISSION_CLASSES=[],
        FRIESE_MCP_UNAUTHENTICATED_TIER="admin",
    )
    def test_subclass_override_read_cap_independent_of_setting(self) -> None:
        """Subclass returning 'read' caps callers even when FRIESE_MCP_MAX_TIER is absent."""

        class _ReadCap(McpView):
            def _effective_max_tier(self) -> str | None:
                return "read"

        reg = _isolated_registry(
            ("read.tool", "read"),
            ("write.tool", "read_write"),
            ("admin.tool", "admin"),
        )
        view = _ReadCap.as_view()
        with patch("friese_mcp.views.tool_registry", reg):
            resp = view(_tools_list_request())
        names = {t["name"] for t in json.loads(resp.content)["result"]["tools"]}
        assert names == {"read.tool"}
