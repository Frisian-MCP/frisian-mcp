"""Tests for permission-aware discovery, backend_action, and E002/E003 checks."""
# pylint: disable=redefined-outer-name
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from frisian_mcp.backends.dispatcher import ActionEntry
from frisian_mcp.decorators import mcp_action
from frisian_mcp.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(arguments: dict[str, Any], _request: Any) -> dict[str, Any]:
    """No-op tool callable for registry fixtures."""
    return arguments


def _build_request(is_superuser: bool = False, perms: set[str] | None = None) -> Any:
    """Build a minimal mock request with a user stub."""
    req = MagicMock()
    req.user = MagicMock()
    req.user.is_authenticated = True
    req.user.is_superuser = is_superuser
    req.user.get_all_permissions = lambda: perms or set()
    req._mcp_max_tier = None
    req.auth = None
    return req


def _registry_with_tool(
    name: str = "device_list",
    perm_app_label: str | None = "dcim",
    perm_model: str | None = "device",
    perm_drf_action: str | None = "list",
    is_write: bool = False,
    is_dispatcher: bool = False,
) -> ToolRegistry:
    """Return a ToolRegistry pre-loaded with a single tool entry."""
    reg = ToolRegistry()
    reg.register(
        name=name,
        fn=_noop,
        description="Test tool",
        input_schema={"type": "object"},
        perm_app_label=perm_app_label,
        perm_model=perm_model,
        perm_drf_action=perm_drf_action,
        is_write=is_write,
        is_dispatcher=is_dispatcher,
    )
    return reg


# ---------------------------------------------------------------------------
# T2: backend_action on @mcp_action + ActionEntry
# ---------------------------------------------------------------------------


class TestBackendAction:
    """``backend_action`` field on ``@mcp_action`` and ``ActionEntry``."""

    def test_mcp_action_stores_backend_action(self) -> None:
        """@mcp_action stores backend_action in _mcp_action_meta."""

        @mcp_action(name="sync", description="Sync resource", backend_action="add")
        def sync_fn(self: Any, request: Any, params: dict) -> dict:
            return {}

        meta = sync_fn._mcp_action_meta  # type: ignore[attr-defined]
        assert meta["backend_action"] == "add"

    def test_mcp_action_defaults_backend_action_to_none(self) -> None:
        """@mcp_action without backend_action stores None."""

        @mcp_action(name="list", description="List resources")
        def list_fn(self: Any, request: Any, params: dict) -> dict:
            return {}

        meta = list_fn._mcp_action_meta  # type: ignore[attr-defined]
        assert meta["backend_action"] is None

    def test_action_entry_backend_action_field(self) -> None:
        """ActionEntry accepts and stores backend_action."""
        entry = ActionEntry(
            name="custom",
            description="Custom action",
            params={},
            input_schema=None,
            method=lambda *a: {},
            backend_action="view",
        )
        assert entry.backend_action == "view"

    def test_action_entry_backend_action_defaults_none(self) -> None:
        """ActionEntry.backend_action defaults to None."""
        entry = ActionEntry(
            name="list",
            description="List",
            params={},
            input_schema=None,
            method=lambda *a: {},
        )
        assert entry.backend_action is None


# ---------------------------------------------------------------------------
# T1: _ToolEntry perm metadata slots
# ---------------------------------------------------------------------------


class TestToolEntryPermSlots:
    """Permission metadata slots on ``_ToolEntry`` populated by ``register()``."""

    def test_register_stores_perm_fields(self) -> None:
        """tool_registry.register() stores perm_app_label, perm_model, perm_drf_action."""
        reg = _registry_with_tool()
        entry = reg.get_entry("device_list")
        assert entry is not None
        assert entry.perm_app_label == "dcim"
        assert entry.perm_model == "device"
        assert entry.perm_drf_action == "list"

    def test_register_perm_fields_default_none(self) -> None:
        """Perm fields default to None when not supplied."""
        reg = ToolRegistry()
        reg.register(
            name="custom_tool",
            fn=_noop,
            description="Custom",
            input_schema={"type": "object"},
        )
        entry = reg.get_entry("custom_tool")
        assert entry is not None
        assert entry.perm_app_label is None
        assert entry.perm_model is None
        assert entry.perm_drf_action is None


# ---------------------------------------------------------------------------
# T1: DjangoPermissionAdapter
# ---------------------------------------------------------------------------


class TestDjangoPermissionAdapter:
    """``DjangoPermissionAdapter`` delegates to ``user.get_all_permissions()``."""

    def test_get_capabilities_returns_frozenset(self) -> None:
        """get_capabilities returns a frozenset of permission strings."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        adapter = DjangoPermissionAdapter()
        user = MagicMock()
        user.get_all_permissions.return_value = {"dcim.view_device", "ipam.view_prefix"}
        caps = adapter.get_capabilities(user)
        assert isinstance(caps, frozenset)
        assert "dcim.view_device" in caps
        assert "ipam.view_prefix" in caps

    def test_get_capabilities_handles_error(self) -> None:
        """get_capabilities returns frozenset() when get_all_permissions raises."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        adapter = DjangoPermissionAdapter()
        user = MagicMock()
        user.get_all_permissions.side_effect = RuntimeError("db down")
        caps = adapter.get_capabilities(user)
        assert caps == frozenset()

    def test_is_unrestricted_superuser(self) -> None:
        """is_unrestricted returns True for superusers."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        adapter = DjangoPermissionAdapter()
        user = MagicMock()
        user.is_superuser = True
        assert adapter.is_unrestricted(user) is True

    def test_is_unrestricted_regular_user(self) -> None:
        """is_unrestricted returns False for non-superusers."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        adapter = DjangoPermissionAdapter()
        user = MagicMock()
        user.is_superuser = False
        assert adapter.is_unrestricted(user) is False


# ---------------------------------------------------------------------------
# T1: entry_filter in list_tools()
# ---------------------------------------------------------------------------


class TestListToolsEntryFilter:
    """``entry_filter`` param on ``ToolRegistry.list_tools()``."""

    def test_entry_filter_excludes_tool(self) -> None:
        """entry_filter=lambda _: False hides every tool."""
        reg = _registry_with_tool()
        result = reg.list_tools(entry_filter=lambda _: False)
        assert result == []

    def test_entry_filter_includes_all(self) -> None:
        """entry_filter=None (default) includes all tools."""
        reg = _registry_with_tool()
        result = reg.list_tools()
        assert len(result) == 1

    def test_entry_filter_by_perm_app_label(self) -> None:
        """Filter by perm_app_label correctly partitions tools."""
        reg = ToolRegistry()
        reg.register(
            name="dcim_device_list",
            fn=_noop,
            description="d",
            input_schema={"type": "object"},
            perm_app_label="dcim",
            perm_model="device",
            perm_drf_action="list",
        )
        reg.register(
            name="ipam_prefix_list",
            fn=_noop,
            description="i",
            input_schema={"type": "object"},
            perm_app_label="ipam",
            perm_model="prefix",
            perm_drf_action="list",
        )
        dcim_only = reg.list_tools(
            entry_filter=lambda e: e.perm_app_label == "dcim"
        )
        assert len(dcim_only) == 1
        assert dcim_only[0]["name"] == "dcim_device_list"


# ---------------------------------------------------------------------------
# T1: _make_perm_entry_filter logic
# ---------------------------------------------------------------------------


class TestPermEntryFilter:
    """Unit tests for the ``_make_perm_entry_filter`` helper in ``views.py``."""

    def test_tool_without_perm_metadata_always_passes(self) -> None:
        """Tools without perm metadata are always visible regardless of capabilities."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="custom",
            fn=_noop,
            description="no perm metadata",
            input_schema={"type": "object"},
        )
        entry = reg.get_entry("custom")
        assert entry is not None
        filt = _make_perm_entry_filter(frozenset())
        assert filt(entry) is True

    def test_dispatcher_tool_always_passes(self) -> None:
        """Dispatcher tools are always visible; per-action filtering happens at call time."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="dcim",
            fn=_noop,
            description="dispatcher",
            input_schema={"type": "object"},
            is_dispatcher=True,
            perm_app_label="dcim",
            perm_model="device",
            perm_drf_action="list",
        )
        entry = reg.get_entry("dcim")
        assert entry is not None
        filt = _make_perm_entry_filter(frozenset())
        assert filt(entry) is True

    def test_tool_included_when_capability_present(self) -> None:
        """Tool is included when user has the required capability."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = _registry_with_tool()
        entry = reg.get_entry("device_list")
        assert entry is not None
        filt = _make_perm_entry_filter(frozenset({"dcim.view_device"}))
        assert filt(entry) is True

    def test_tool_excluded_when_capability_absent(self) -> None:
        """Tool is excluded when user lacks the required capability."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = _registry_with_tool()
        entry = reg.get_entry("device_list")
        assert entry is not None
        filt = _make_perm_entry_filter(frozenset({"ipam.view_prefix"}))
        assert filt(entry) is False

    @pytest.mark.parametrize(
        "drf_action, expected_verb",
        [
            ("list", "view"),
            ("retrieve", "view"),
            ("create", "add"),
            ("update", "change"),
            ("partial_update", "change"),
            ("destroy", "delete"),
            ("unknown_action", "view"),  # unknown → conservative default
        ],
    )
    def test_drf_action_to_perm_verb_mapping(
        self, drf_action: str, expected_verb: str
    ) -> None:
        """DRF action names map to the correct Django permission verb."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = _registry_with_tool(perm_drf_action=drf_action)
        entry = reg.get_entry("device_list")
        assert entry is not None
        required_cap = f"dcim.{expected_verb}_device"
        # With the right capability → included
        assert _make_perm_entry_filter(frozenset({required_cap}))(entry) is True
        # Without it → excluded
        assert _make_perm_entry_filter(frozenset())(entry) is False


# ---------------------------------------------------------------------------
# T1: tools/list endpoint integration
# ---------------------------------------------------------------------------


class TestToolsListPermAwareFilter:
    """Integration: tools/list respects FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY."""

    def _post_tools_list(self, request: Any) -> dict[str, Any]:
        """POST a tools/list request through _parse_and_dispatch."""
        from django.http import HttpRequest

        from frisian_mcp.views import _parse_and_dispatch

        http_req = HttpRequest()
        http_req.method = "POST"
        http_req._stream = None  # type: ignore[attr-defined]
        http_req._body = json.dumps(  # type: ignore[attr-defined]
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()
        http_req.META["HTTP_ACCEPT"] = "application/json"
        http_req._mcp_max_tier = None  # type: ignore[attr-defined]
        # Attach the mocked user from the passed request
        http_req.user = request.user
        http_req.auth = request.auth
        response = _parse_and_dispatch(http_req)
        return json.loads(response.content)

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=False)
    def test_flag_off_returns_all_tools(self) -> None:
        """When flag is off, all registered tools appear."""
        from frisian_mcp.registry import tool_registry

        tool_registry.register(
            name="_perm_test_flag_off",
            fn=_noop,
            description="test",
            input_schema={"type": "object"},
            perm_app_label="dcim",
            perm_model="testmodel",
            perm_drf_action="list",
        )
        req = _build_request(perms=set())
        body = self._post_tools_list(req)
        names = [t["name"] for t in body["result"]["tools"]]
        assert "_perm_test_flag_off" in names
        tool_registry._tools.pop("_perm_test_flag_off", None)

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER=(
            "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
        ),
    )
    def test_flag_on_filters_by_capability(self) -> None:
        """When flag is on, tools are filtered by user capabilities."""
        from frisian_mcp.registry import tool_registry

        tool_registry.register(
            name="_perm_test_visible",
            fn=_noop,
            description="visible",
            input_schema={"type": "object"},
            perm_app_label="dcim",
            perm_model="visiblemodel",
            perm_drf_action="list",
        )
        tool_registry.register(
            name="_perm_test_hidden",
            fn=_noop,
            description="hidden",
            input_schema={"type": "object"},
            perm_app_label="dcim",
            perm_model="hiddenmodel",
            perm_drf_action="list",
        )
        req = _build_request(perms={"dcim.view_visiblemodel"})
        body = self._post_tools_list(req)
        names = [t["name"] for t in body["result"]["tools"]]
        assert "_perm_test_visible" in names
        assert "_perm_test_hidden" not in names
        tool_registry._tools.pop("_perm_test_visible", None)
        tool_registry._tools.pop("_perm_test_hidden", None)

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER=(
            "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
        ),
    )
    def test_superuser_sees_all_tools(self) -> None:
        """Superusers bypass the capability filter."""
        from frisian_mcp.registry import tool_registry

        tool_registry.register(
            name="_perm_test_superuser",
            fn=_noop,
            description="superuser test",
            input_schema={"type": "object"},
            perm_app_label="dcim",
            perm_model="superusermodel",
            perm_drf_action="list",
        )
        req = _build_request(is_superuser=True, perms=set())
        body = self._post_tools_list(req)
        names = [t["name"] for t in body["result"]["tools"]]
        assert "_perm_test_superuser" in names
        tool_registry._tools.pop("_perm_test_superuser", None)


# ---------------------------------------------------------------------------
# T3: E002 check
# ---------------------------------------------------------------------------


class TestE002Check:
    """Django system check E002: OAuth installed without FRISIAN_MCP_OAUTH_SERVICE_USER."""

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=False)
    def test_flag_off_no_errors(self) -> None:
        """E002 check returns no errors when flag is off."""
        from frisian_mcp.checks import check_permission_aware_discovery

        errors = check_permission_aware_discovery()
        assert errors == []

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_OAUTH_SERVICE_USER=None,
    )
    def test_oauth_installed_no_service_user_raises_e002(self) -> None:
        """E002 fires when OAuth is installed and FRISIAN_MCP_OAUTH_SERVICE_USER is unset."""
        import frisian_mcp.checks as checks_mod
        from frisian_mcp.checks import E002_OAUTH_IDENTITY_GAP, check_permission_aware_discovery

        mock_apps = MagicMock()
        mock_apps.is_installed.return_value = True
        with patch.object(checks_mod, "django_apps", mock_apps):
            errors = check_permission_aware_discovery()
        e002 = [e for e in errors if e.id == E002_OAUTH_IDENTITY_GAP]
        assert len(e002) == 1

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_OAUTH_SERVICE_USER="service_account",
    )
    def test_oauth_installed_with_service_user_no_e002(self) -> None:
        """No E002 when OAuth is installed and FRISIAN_MCP_OAUTH_SERVICE_USER is set."""
        import frisian_mcp.checks as checks_mod
        from frisian_mcp.checks import E002_OAUTH_IDENTITY_GAP, check_permission_aware_discovery

        mock_apps = MagicMock()
        mock_apps.is_installed.return_value = True
        with patch.object(checks_mod, "django_apps", mock_apps):
            errors = check_permission_aware_discovery()
        e002 = [e for e in errors if e.id == E002_OAUTH_IDENTITY_GAP]
        assert e002 == []

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_oauth_not_installed_no_e002(self) -> None:
        """No E002 when frisian_mcp.contrib.oauth is not installed."""
        import frisian_mcp.checks as checks_mod
        from frisian_mcp.checks import E002_OAUTH_IDENTITY_GAP, check_permission_aware_discovery

        mock_apps = MagicMock()
        mock_apps.is_installed.return_value = False
        with patch.object(checks_mod, "django_apps", mock_apps):
            errors = check_permission_aware_discovery()
        e002 = [e for e in errors if e.id == E002_OAUTH_IDENTITY_GAP]
        assert e002 == []


# ---------------------------------------------------------------------------
# T3: E003 check
# ---------------------------------------------------------------------------


class TestE003Check:
    """Django system check E003: non-CRUD dispatcher action without backend_action."""

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=False)
    def test_flag_off_no_e003(self) -> None:
        """E003 check returns no errors when flag is off."""
        from frisian_mcp.checks import check_permission_aware_discovery

        errors = check_permission_aware_discovery()
        assert errors == []

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_dispatcher_crud_action_no_e003(self) -> None:
        """CRUD actions on a dispatcher do not trigger E003."""
        import frisian_mcp.checks as checks_mod
        from frisian_mcp.backends.dispatcher import ActionEntry, DispatcherMeta
        from frisian_mcp.checks import (
            E003_UNANNOTATED_CUSTOM_ACTION,
            check_permission_aware_discovery,
        )

        reg = ToolRegistry()
        meta = DispatcherMeta(
            name="devices",
            description="Devices dispatcher",
            actions={
                "list": ActionEntry(
                    name="list",
                    description="List",
                    params={},
                    input_schema=None,
                    method=_noop,
                    backend_action=None,
                )
            },
        )
        reg.register(
            name="devices",
            fn=_noop,
            description="d",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
        )
        with patch.object(checks_mod, "tool_registry", reg):
            errors = check_permission_aware_discovery()
        e003 = [e for e in errors if e.id == E003_UNANNOTATED_CUSTOM_ACTION]
        assert e003 == []

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_dispatcher_custom_action_without_backend_action_raises_e003(self) -> None:
        """Non-CRUD action without backend_action triggers E003."""
        import frisian_mcp.checks as checks_mod
        from frisian_mcp.backends.dispatcher import ActionEntry, DispatcherMeta
        from frisian_mcp.checks import (
            E003_UNANNOTATED_CUSTOM_ACTION,
            check_permission_aware_discovery,
        )

        reg = ToolRegistry()
        meta = DispatcherMeta(
            name="devices",
            description="Devices dispatcher",
            actions={
                "sync_config": ActionEntry(
                    name="sync_config",
                    description="Sync",
                    params={},
                    input_schema=None,
                    method=_noop,
                    backend_action=None,  # missing annotation → E003
                )
            },
        )
        reg.register(
            name="devices",
            fn=_noop,
            description="d",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
        )
        with patch.object(checks_mod, "tool_registry", reg):
            errors = check_permission_aware_discovery()
        e003 = [e for e in errors if e.id == E003_UNANNOTATED_CUSTOM_ACTION]
        assert len(e003) == 1
        assert "sync_config" in e003[0].msg

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_dispatcher_custom_action_with_backend_action_no_e003(self) -> None:
        """Non-CRUD action WITH backend_action does not trigger E003."""
        import frisian_mcp.checks as checks_mod
        from frisian_mcp.backends.dispatcher import ActionEntry, DispatcherMeta
        from frisian_mcp.checks import (
            E003_UNANNOTATED_CUSTOM_ACTION,
            check_permission_aware_discovery,
        )

        reg = ToolRegistry()
        meta = DispatcherMeta(
            name="devices",
            description="Devices dispatcher",
            actions={
                "sync_config": ActionEntry(
                    name="sync_config",
                    description="Sync",
                    params={},
                    input_schema=None,
                    method=_noop,
                    backend_action="change",
                )
            },
        )
        reg.register(
            name="devices",
            fn=_noop,
            description="d",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
        )
        with patch.object(checks_mod, "tool_registry", reg):
            errors = check_permission_aware_discovery()
        e003 = [e for e in errors if e.id == E003_UNANNOTATED_CUSTOM_ACTION]
        assert e003 == []
