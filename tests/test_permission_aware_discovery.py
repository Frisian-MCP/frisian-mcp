"""Tests for permission-aware discovery, backend_action, and E003 checks."""

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
        dcim_only = reg.list_tools(entry_filter=lambda e: e.perm_app_label == "dcim")
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
    def test_drf_action_to_perm_verb_mapping(self, drf_action: str, expected_verb: str) -> None:
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
# Group dispatcher visibility filtering
# ---------------------------------------------------------------------------


class TestGroupDispatcherVisibility:
    """Group dispatchers are hidden when the user has no capabilities for any child tool."""

    _GROUP = "_grp_test_net"
    _CHILD_VIEW = "_grp_test_net_list"
    _CHILD_WRITE = "_grp_test_net_create"
    _OTHER_GROUP = "_grp_test_other"
    _OTHER_CHILD = "_grp_test_other_list"

    def _post_tools_list(self, request: Any) -> dict[str, Any]:
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
        http_req.user = request.user
        http_req.auth = request.auth
        response = _parse_and_dispatch(http_req)
        return json.loads(response.content)

    def _setup(self) -> None:
        from frisian_mcp.registry import tool_registry

        # Child tools for the group under test.
        tool_registry.register(
            name=self._CHILD_VIEW,
            fn=_noop,
            description="net list",
            input_schema={"type": "object"},
            perm_app_label="net",
            perm_model="network",
            perm_drf_action="list",
            hidden=True,
        )
        tool_registry.register(
            name=self._CHILD_WRITE,
            fn=_noop,
            description="net create",
            input_schema={"type": "object"},
            perm_app_label="net",
            perm_model="network",
            perm_drf_action="create",
            hidden=True,
        )
        # Group dispatcher that bundles those two tools.
        tool_registry.register(
            name=self._GROUP,
            fn=_noop,
            description="net group",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({self._CHILD_VIEW, self._CHILD_WRITE}),
        )
        # Separate group with its own child — used to confirm only the
        # matching group is hidden, not all groups.
        tool_registry.register(
            name=self._OTHER_CHILD,
            fn=_noop,
            description="other list",
            input_schema={"type": "object"},
            perm_app_label="other",
            perm_model="thing",
            perm_drf_action="list",
            hidden=True,
        )
        tool_registry.register(
            name=self._OTHER_GROUP,
            fn=_noop,
            description="other group",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({self._OTHER_CHILD}),
        )

    def _teardown(self) -> None:
        from frisian_mcp.registry import tool_registry

        for name in (
            self._CHILD_VIEW,
            self._CHILD_WRITE,
            self._GROUP,
            self._OTHER_CHILD,
            self._OTHER_GROUP,
        ):
            tool_registry._tools.pop(name, None)

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER="frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter",
    )
    def test_group_hidden_when_user_has_no_child_capabilities(self) -> None:
        """Group dispatcher absent from tools/list when user has zero matching capabilities."""
        self._setup()
        try:
            req = _build_request(perms=set())
            body = self._post_tools_list(req)
            names = [t["name"] for t in body["result"]["tools"]]
            assert self._GROUP not in names
            assert self._OTHER_GROUP not in names
        finally:
            self._teardown()

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER="frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter",
    )
    def test_group_visible_when_user_has_at_least_one_child_capability(self) -> None:
        """Group dispatcher appears when the user has view capability for any child tool."""
        self._setup()
        try:
            req = _build_request(perms={"net.view_network"})
            body = self._post_tools_list(req)
            names = [t["name"] for t in body["result"]["tools"]]
            assert self._GROUP in names
            assert self._OTHER_GROUP not in names
        finally:
            self._teardown()

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER="frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter",
    )
    def test_two_groups_each_visible_when_user_has_capabilities_for_both(self) -> None:
        """Both groups appear when user has at least one capability in each."""
        self._setup()
        try:
            req = _build_request(perms={"net.view_network", "other.view_thing"})
            body = self._post_tools_list(req)
            names = [t["name"] for t in body["result"]["tools"]]
            assert self._GROUP in names
            assert self._OTHER_GROUP in names
        finally:
            self._teardown()

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=False)
    def test_group_always_visible_when_flag_off(self) -> None:
        """Group dispatcher is always visible when permission-aware discovery is disabled."""
        self._setup()
        try:
            req = _build_request(perms=set())
            body = self._post_tools_list(req)
            names = [t["name"] for t in body["result"]["tools"]]
            assert self._GROUP in names
            assert self._OTHER_GROUP in names
        finally:
            self._teardown()

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER="frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter",
    )
    def test_group_always_visible_for_superuser(self) -> None:
        """Superusers bypass capability filtering — all groups visible."""
        self._setup()
        try:
            req = _build_request(is_superuser=True, perms=set())
            body = self._post_tools_list(req)
            names = [t["name"] for t in body["result"]["tools"]]
            assert self._GROUP in names
            assert self._OTHER_GROUP in names
        finally:
            self._teardown()

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER="frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter",
    )
    def test_perm_less_child_does_not_keep_group_visible(self) -> None:
        """
        A group is hidden when its only perm-aware children fail the filter.

        Perm-less children (no perm_app_label / perm_model, e.g. napalm, notes)
        always pass _make_perm_entry_filter.  Counting them caused groups to
        remain visible for users with no matching capabilities.
        """
        from frisian_mcp.registry import tool_registry

        group_name = "_grp_permless_test"
        perm_child = "_grp_permless_net_list"
        permless_child = "_grp_permless_napalm"

        tool_registry.register(
            name=perm_child,
            fn=_noop,
            description="net list",
            input_schema={"type": "object"},
            perm_app_label="net",
            perm_model="network",
            perm_drf_action="list",
            hidden=True,
        )
        tool_registry.register(
            name=permless_child,
            fn=_noop,
            description="napalm — no perm metadata",
            input_schema={"type": "object"},
            # No perm_app_label / perm_model — simulates napalm/notes tools
            hidden=True,
        )
        tool_registry.register(
            name=group_name,
            fn=_noop,
            description="net group",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({perm_child, permless_child}),
        )

        try:
            # User has no permissions → perm-aware child fails → group hidden.
            req = _build_request(perms=set())
            body = self._post_tools_list(req)
            names = [t["name"] for t in body["result"]["tools"]]
            assert (
                group_name not in names
            ), "Group should be hidden when user lacks capabilities for perm-aware children"

            # User has the matching permission → group visible.
            req2 = _build_request(perms={"net.view_network"})
            body2 = self._post_tools_list(req2)
            names2 = [t["name"] for t in body2["result"]["tools"]]
            assert group_name in names2
        finally:
            for name in (perm_child, permless_child, group_name):
                tool_registry._tools.pop(name, None)


# ---------------------------------------------------------------------------
# T3: E002 check
# ---------------------------------------------------------------------------


class TestE002Check:
    """
    E002 constant is retained for backward compat but the check no longer fires.

    OAuth clients with no linked Django user are treated as service principals
    (``_mcp_is_service_principal=True``) and bypass capability filtering — tier
    is the sole gate.  Clients with a linked user get full ObjectPermission
    filtering.  There is no configuration gap that E002 needs to guard against.
    """

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=False)
    def test_flag_off_no_errors(self) -> None:
        """No errors when flag is off."""
        from frisian_mcp.checks import check_permission_aware_discovery

        errors = check_permission_aware_discovery()
        assert errors == []

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_OAUTH_SERVICE_USER=None,
    )
    def test_oauth_installed_no_service_user_no_e002(self) -> None:
        """E002 does not fire when OAuth is installed without FRISIAN_MCP_OAUTH_SERVICE_USER."""
        from frisian_mcp.checks import E002_OAUTH_IDENTITY_GAP, check_permission_aware_discovery

        errors = check_permission_aware_discovery()
        e002 = [e for e in errors if e.id == E002_OAUTH_IDENTITY_GAP]
        assert e002 == []

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_OAUTH_SERVICE_USER="service_account",
    )
    def test_oauth_installed_with_service_user_no_e002(self) -> None:
        """No E002 when OAuth is installed and FRISIAN_MCP_OAUTH_SERVICE_USER is set."""
        from frisian_mcp.checks import E002_OAUTH_IDENTITY_GAP, check_permission_aware_discovery

        errors = check_permission_aware_discovery()
        e002 = [e for e in errors if e.id == E002_OAUTH_IDENTITY_GAP]
        assert e002 == []

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_oauth_not_installed_no_e002(self) -> None:
        """No E002 regardless of whether frisian_mcp.contrib.oauth is installed."""
        from frisian_mcp.checks import E002_OAUTH_IDENTITY_GAP, check_permission_aware_discovery

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


# ---------------------------------------------------------------------------
# T4: _make_perm_action_filter_factory — dispatcher action enum filtering
# ---------------------------------------------------------------------------


def _make_dispatcher_entry(
    app_label: str,
    model: str,
    actions: dict[str, ActionEntry],
) -> Any:
    """Return a mock _ToolEntry representing a dispatcher with the given perm metadata."""
    from frisian_mcp.backends.dispatcher import DispatcherMeta

    entry = MagicMock()
    entry.is_dispatcher = True
    entry.perm_app_label = app_label
    entry.perm_model = model
    entry.dispatcher_meta = DispatcherMeta(
        name=f"{app_label}_{model}",
        description="test dispatcher",
        actions=actions,
    )
    return entry


def _crud_actions() -> dict[str, ActionEntry]:
    """Return a minimal set of CRUD ActionEntry objects."""

    def _m(*_: Any) -> dict:  # noqa: ANN202
        return {}

    return {
        "list": ActionEntry(
            name="list", description="List", params={}, input_schema=None, method=_m
        ),
        "retrieve": ActionEntry(
            name="retrieve", description="Get one", params={}, input_schema=None, method=_m
        ),
        "create": ActionEntry(
            name="create", description="Create", params={}, input_schema=None, method=_m
        ),
        "update": ActionEntry(
            name="update", description="Update", params={}, input_schema=None, method=_m
        ),
        "destroy": ActionEntry(
            name="destroy", description="Delete", params={}, input_schema=None, method=_m
        ),
    }


class TestPermActionFilterFactory:
    """Unit and integration tests for ``_make_perm_action_filter_factory``."""

    def test_factory_returns_none_for_entry_without_perm_metadata(self) -> None:
        """Factory returns None when the dispatcher has no perm_app_label/perm_model."""
        from frisian_mcp.views import _make_perm_action_filter_factory

        entry = MagicMock()
        entry.perm_app_label = None
        entry.perm_model = None
        factory = _make_perm_action_filter_factory(frozenset({"dcim.view_device"}))
        assert factory(entry) is None

    def test_factory_returns_callable_when_perm_metadata_present(self) -> None:
        """Factory returns a callable when perm_app_label and perm_model are set."""
        from frisian_mcp.views import _make_perm_action_filter_factory

        entry = _make_dispatcher_entry("dcim", "device", _crud_actions())
        factory = _make_perm_action_filter_factory(frozenset({"dcim.view_device"}))
        result = factory(entry)
        assert callable(result)

    @pytest.mark.parametrize(
        "action_name, backend_action, cap, expected",
        [
            # Standard CRUD verbs
            ("list", None, "dcim.view_device", True),
            ("retrieve", None, "dcim.view_device", True),
            ("create", None, "dcim.add_device", True),
            ("update", None, "dcim.change_device", True),
            ("destroy", None, "dcim.delete_device", True),
            # Unknown action name defaults to "view"
            ("custom_action", None, "dcim.view_device", True),
            # backend_action overrides action name mapping
            ("sync", "add", "dcim.add_device", True),
            ("sync", "add", "dcim.view_device", False),
            # Missing capability → excluded
            ("list", None, "dcim.add_device", False),  # has add, not view
        ],
    )
    def test_action_filter_predicate(
        self,
        action_name: str,
        backend_action: str | None,
        cap: str,
        expected: bool,
    ) -> None:
        """action_filter predicate correctly allows/blocks each case."""
        from frisian_mcp.views import _make_perm_action_filter_factory

        action_entry = ActionEntry(
            name=action_name,
            description="test",
            params={},
            input_schema=None,
            method=lambda *_: {},
            backend_action=backend_action,
        )
        entry = _make_dispatcher_entry("dcim", "device", {action_name: action_entry})
        factory = _make_perm_action_filter_factory(frozenset({cap}))
        predicate = factory(entry)
        assert predicate is not None
        assert predicate(action_name, action_entry) is expected

    def test_view_only_user_sees_read_actions_not_write(self) -> None:
        """A user with only view_device sees list/retrieve but not create/update/destroy."""
        from frisian_mcp.backends.dispatcher import _build_dispatcher_input_schema
        from frisian_mcp.views import _make_perm_action_filter_factory

        actions = _crud_actions()
        entry = _make_dispatcher_entry("dcim", "device", actions)
        factory = _make_perm_action_filter_factory(frozenset({"dcim.view_device"}))
        predicate = factory(entry)

        schema = _build_dispatcher_input_schema(entry.dispatcher_meta, action_filter=predicate)
        visible = schema["properties"]["action"]["enum"]
        assert "list" in visible
        assert "retrieve" in visible
        assert "create" not in visible
        assert "update" not in visible
        assert "destroy" not in visible

    def test_full_crud_user_sees_all_actions(self) -> None:
        """A user with all permissions sees every action in the enum."""
        from frisian_mcp.backends.dispatcher import _build_dispatcher_input_schema
        from frisian_mcp.views import _make_perm_action_filter_factory

        actions = _crud_actions()
        entry = _make_dispatcher_entry("dcim", "device", actions)
        caps = frozenset(
            {
                "dcim.view_device",
                "dcim.add_device",
                "dcim.change_device",
                "dcim.delete_device",
            }
        )
        factory = _make_perm_action_filter_factory(caps)
        predicate = factory(entry)

        schema = _build_dispatcher_input_schema(entry.dispatcher_meta, action_filter=predicate)
        visible = set(schema["properties"]["action"]["enum"])
        assert visible == {"list", "retrieve", "create", "update", "destroy"}

    def test_no_capabilities_user_sees_no_actions(self) -> None:
        """A user with zero permissions sees an empty action enum."""
        from frisian_mcp.backends.dispatcher import _build_dispatcher_input_schema
        from frisian_mcp.views import _make_perm_action_filter_factory

        actions = _crud_actions()
        entry = _make_dispatcher_entry("dcim", "device", actions)
        factory = _make_perm_action_filter_factory(frozenset())
        predicate = factory(entry)

        schema = _build_dispatcher_input_schema(entry.dispatcher_meta, action_filter=predicate)
        assert schema["properties"]["action"]["enum"] == []

    @override_settings(
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
        FRISIAN_MCP_PERMISSION_ADAPTER=(
            "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
        ),
    )
    def test_list_tools_action_filter_factory_wired(self) -> None:
        """list_tools passes action_filter_factory into dispatcher schema when flag is on."""
        from frisian_mcp.backends.dispatcher import DispatcherMeta
        from frisian_mcp.registry import ToolRegistry

        def _m(*_: Any) -> dict:  # noqa: ANN202
            return {}

        reg = ToolRegistry()
        meta = DispatcherMeta(
            name="_paf_test_dispatcher",
            description="test",
            actions={
                "list": ActionEntry(
                    name="list", description="List", params={}, input_schema=None, method=_m
                ),
                "create": ActionEntry(
                    name="create", description="Create", params={}, input_schema=None, method=_m
                ),
            },
        )
        reg.register(
            name="_paf_test_dispatcher",
            fn=_m,
            description="test dispatcher",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
            perm_app_label="dcim",
            perm_model="paftestmodel",
        )

        view_only_caps = frozenset({"dcim.view_paftestmodel"})
        action_filter_factory = __import__(
            "frisian_mcp.views", fromlist=["_make_perm_action_filter_factory"]
        )._make_perm_action_filter_factory(view_only_caps)

        tools = reg.list_tools(
            entry_filter=lambda _e: True,
            action_filter_factory=lambda e: action_filter_factory(e),
        )
        dispatcher_tool = next((t for t in tools if t["name"] == "_paf_test_dispatcher"), None)
        assert dispatcher_tool is not None
        enum = dispatcher_tool["inputSchema"]["properties"]["action"]["enum"]
        assert "list" in enum
        assert "create" not in enum

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


# ---------------------------------------------------------------------------
# Help-bypass fix: action="help" must respect Django permission filtering
# ---------------------------------------------------------------------------


def _make_ae(name: str, method: Any) -> ActionEntry:
    """Build a minimal ActionEntry for help-bypass tests."""
    return ActionEntry(
        name=name,
        description=name.capitalize(),
        params={},
        input_schema=None,
        method=method,
    )


class TestHelpBypassFix:
    """
    Verify ``action="help"`` on dispatchers respects Django-permission filtering.

    Agents could previously enumerate write/delete actions via help even when
    they lacked those permissions.  The fix attaches capabilities to the request
    so both ``@mcp_dispatcher`` and group dispatchers apply the same filtering
    as ``tools/list``.
    """

    def _req(self, caps: frozenset[str] | None) -> Any:
        """Return a mock request with ``_mcp_capabilities`` pre-attached."""
        req = MagicMock()
        req._mcp_capabilities = caps
        req._mcp_perm_entry_filter = None
        return req

    def test_dispatcher_help_filters_write_actions_for_view_only_user(self) -> None:
        """action="help" on @mcp_dispatcher hides create/destroy for view-only users."""
        from frisian_mcp.backends.dispatcher import (
            DispatcherMeta,
            _build_help_response,
            _build_perm_action_filter_from_request,
        )
        from frisian_mcp.registry import ToolRegistry

        def _m(*_: Any) -> dict[str, Any]:
            return {}

        reg = ToolRegistry()
        meta = DispatcherMeta(
            name="_hb_disp",
            description="test",
            actions={
                "list": _make_ae("list", _m),
                "create": _make_ae("create", _m),
                "destroy": _make_ae("destroy", _m),
            },
        )
        reg.register(
            name="_hb_disp",
            fn=_m,
            description="test",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
            perm_app_label="dcim",
            perm_model="hbmodel",
        )

        req = self._req(frozenset({"dcim.view_hbmodel"}))
        with patch("frisian_mcp.registry.tool_registry", reg):
            af = _build_perm_action_filter_from_request(req, "_hb_disp")

        names = {a["name"] for a in _build_help_response(meta, action_filter=af)["actions"]}
        assert "list" in names
        assert "create" not in names
        assert "destroy" not in names

    def test_dispatcher_help_shows_all_for_unrestricted_user(self) -> None:
        """action="help" shows all actions when capabilities is None (superuser/flag off)."""
        from frisian_mcp.backends.dispatcher import (
            DispatcherMeta,
            _build_help_response,
            _build_perm_action_filter_from_request,
        )

        def _m(*_: Any) -> dict[str, Any]:
            return {}

        meta = DispatcherMeta(
            name="_hb_unrestricted",
            description="test",
            actions={"list": _make_ae("list", _m), "create": _make_ae("create", _m)},
        )
        req = self._req(None)  # None = unrestricted/disabled
        af = _build_perm_action_filter_from_request(req, "_hb_unrestricted")
        assert af is None

        names = {a["name"] for a in _build_help_response(meta, action_filter=None)["actions"]}
        assert names == {"list", "create"}

    def test_group_help_filters_write_tools_for_view_only_user(self) -> None:
        """build_group_help hides write tools when entry_filter is supplied."""
        from frisian_mcp.backends.group_dispatcher import build_group_help
        from frisian_mcp.registry import ToolRegistry
        from frisian_mcp.views import _make_perm_entry_filter

        def _m(*_: Any) -> dict[str, Any]:
            return {}

        reg = ToolRegistry()
        # Use "zone_<action>" so the default "_" separator splits as (resource="zone", action=...).
        for act, drf in [
            ("list", "list"),
            ("retrieve", "retrieve"),
            ("create", "create"),
            ("destroy", "destroy"),
        ]:
            reg.register(
                name=f"zone_{act}",
                fn=_m,
                description=act,
                input_schema={"type": "object"},
                perm_app_label="dns",
                perm_model="zone",
                perm_drf_action=drf,
            )

        filt = _make_perm_entry_filter(frozenset({"dns.view_zone"}))
        tnames = ["zone_list", "zone_retrieve", "zone_create", "zone_destroy"]
        result = build_group_help("dns", tnames, reg, entry_filter=filt)
        visible = set(result["resources"]["zone"])
        assert "list" in visible
        assert "retrieve" in visible
        assert "create" not in visible
        assert "destroy" not in visible

    def test_group_help_hints_filtered_by_entry_filter(self) -> None:
        """
        build_group_help strips hints for tools the entry_filter rejects.

        A user with only view_zone should not see hints for zone_create or
        zone_destroy even if those hints are present in FRISIAN_MCP_TOOL_HINTS.
        """
        from frisian_mcp.backends.group_dispatcher import build_group_help
        from frisian_mcp.registry import ToolRegistry
        from frisian_mcp.views import _make_perm_entry_filter

        def _m(*_: Any) -> dict[str, Any]:
            return {}

        reg = ToolRegistry()
        for act, drf in [
            ("list", "list"),
            ("retrieve", "retrieve"),
            ("create", "create"),
            ("destroy", "destroy"),
        ]:
            reg.register(
                name=f"zone_{act}",
                fn=_m,
                description=act,
                input_schema={"type": "object"},
                perm_app_label="dns",
                perm_model="zone",
                perm_drf_action=drf,
            )

        hints = {
            "zone_list": "List all zones.",
            "zone_retrieve": "Get one zone.",
            "zone_create": "Create a zone.",
            "zone_destroy": "Delete a zone.",
        }
        filt = _make_perm_entry_filter(frozenset({"dns.view_zone"}))
        tnames = ["zone_list", "zone_retrieve", "zone_create", "zone_destroy"]
        result = build_group_help("dns", tnames, reg, entry_filter=filt, hints=hints)
        returned_hints = result.get("hints", {})
        assert "zone_list" in returned_hints
        assert "zone_retrieve" in returned_hints
        assert "zone_create" not in returned_hints
        assert "zone_destroy" not in returned_hints

    def test_group_dispatch_raises_permission_error_for_filtered_tool(self) -> None:
        """
        make_group_invoke raises PermissionError when the entry_filter rejects the target.

        A caller who knows a resource/action name cannot bypass
        FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY by calling the group dispatcher
        directly.
        """
        from frisian_mcp.backends.group_dispatcher import make_group_invoke
        from frisian_mcp.registry import ToolRegistry
        from frisian_mcp.views import _make_perm_entry_filter

        def _m(*_: Any) -> dict[str, Any]:
            return {}

        reg = ToolRegistry()
        reg.register(
            name="zone_list",
            fn=_m,
            description="list zones",
            input_schema={"type": "object"},
            perm_app_label="dns",
            perm_model="zone",
            perm_drf_action="list",
        )
        reg.register(
            name="zone_create",
            fn=_m,
            description="create zone",
            input_schema={"type": "object"},
            perm_app_label="dns",
            perm_model="zone",
            perm_drf_action="create",
        )

        # User has view only — zone_create should be blocked at dispatch.
        filt = _make_perm_entry_filter(frozenset({"dns.view_zone"}))
        invoke = make_group_invoke(
            "dns",
            frozenset({"zone_list", "zone_create"}),
            reg,
        )

        req = MagicMock()
        req.user = MagicMock()
        req.user.is_superuser = False
        req._mcp_perm_entry_filter = filt
        req._mcp_capabilities = frozenset({"dns.view_zone"})

        with pytest.raises(PermissionError):
            invoke({"resource": "zone", "action": "create", "params": {}}, req)

    def test_ensure_perm_context_idempotent(self) -> None:
        """_ensure_perm_context_on_request is a no-op on the second call."""
        from frisian_mcp.views import _ensure_perm_context_on_request

        req = MagicMock(spec=[])
        req.user = MagicMock()
        req.user.is_superuser = False
        req.user.get_all_permissions.return_value = {"dcim.view_device"}

        with override_settings(
            FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
            FRISIAN_MCP_PERMISSION_ADAPTER=(
                "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
            ),
        ):
            _ensure_perm_context_on_request(req)
            caps_first = req._mcp_capabilities
            req.user.get_all_permissions.return_value = {"ipam.view_prefix"}
            _ensure_perm_context_on_request(req)
            caps_second = req._mcp_capabilities

        assert caps_first == caps_second


# ---------------------------------------------------------------------------
# ExemptViewPermissionAdapter — EXEMPT_EXCLUDE_MODELS
# ---------------------------------------------------------------------------


class TestExemptViewAdapterExcludeModels:
    """
    ExemptViewPermissionAdapter + EXEMPT_EXCLUDE_MODELS.

    When EXEMPT_VIEW_PERMISSIONS is the wildcard form (``"__all__"`` or ``"*"``),
    models in EXEMPT_EXCLUDE_MODELS must NOT be synthesized as view-capable.
    """

    def _make_user(self, perms: set[str] | None = None) -> Any:
        user = MagicMock()
        user.is_anonymous = False
        user.is_superuser = False
        user.get_all_permissions.return_value = perms or set()
        return user

    @override_settings(
        EXEMPT_VIEW_PERMISSIONS="__all__",
        EXEMPT_EXCLUDE_MODELS=[("auth", "group"), ("auth", "permission")],
    )
    def test_exempt_all_excludes_listed_models(self) -> None:
        """Excluded models are NOT synthesized as view-capable with '__all__'."""
        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        adapter = ExemptViewPermissionAdapter()
        caps = adapter.get_capabilities(self._make_user())
        assert "auth.view_group" not in caps
        assert "auth.view_permission" not in caps

    @override_settings(
        EXEMPT_VIEW_PERMISSIONS="*",
        EXEMPT_EXCLUDE_MODELS=[("auth", "group")],
    )
    def test_exempt_star_wildcard_excludes_listed_models(self) -> None:
        """Excluded models are NOT synthesized as view-capable with '*'."""
        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        adapter = ExemptViewPermissionAdapter()
        caps = adapter.get_capabilities(self._make_user())
        assert "auth.view_group" not in caps

    @override_settings(
        EXEMPT_VIEW_PERMISSIONS="__all__",
        EXEMPT_EXCLUDE_MODELS=[],
    )
    def test_exempt_all_empty_exclude_includes_all(self) -> None:
        """Empty EXEMPT_EXCLUDE_MODELS means all models are synthesized."""
        from django.apps import apps

        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        adapter = ExemptViewPermissionAdapter()
        caps = adapter.get_capabilities(self._make_user())
        # Every installed model should appear.
        for model in apps.get_models():
            meta = model._meta  # pylint: disable=protected-access
            assert f"{meta.app_label}.view_{meta.model_name}" in caps

    @override_settings(
        EXEMPT_VIEW_PERMISSIONS=["dcim.device"],
        EXEMPT_EXCLUDE_MODELS=[("dcim", "device")],
    )
    def test_explicit_list_form_unaffected_by_exclude_models(self) -> None:
        """EXEMPT_EXCLUDE_MODELS only applies to the wildcard path, not the list path."""
        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        adapter = ExemptViewPermissionAdapter()
        caps = adapter.get_capabilities(self._make_user())
        # Explicit list path still synthesizes the capability regardless of exclude list.
        assert "dcim.view_device" in caps


# ---------------------------------------------------------------------------
# _ensure_perm_context_on_request — service principal bypass
# ---------------------------------------------------------------------------


class TestServicePrincipalBypass:
    """
    Service principal capability bypass.

    OAuth service principals (``_mcp_is_service_principal=True``) must bypass
    capability filtering entirely — tier is the sole gate.
    """

    def test_service_principal_sets_null_filter(self) -> None:
        """_mcp_perm_entry_filter is None for service principal regardless of permissions."""
        from frisian_mcp.views import _ensure_perm_context_on_request

        req = MagicMock(spec=[])
        user = MagicMock()
        user.is_superuser = False
        user._mcp_is_service_principal = True
        user.get_all_permissions.return_value = set()
        req.user = user

        with override_settings(
            FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
            FRISIAN_MCP_PERMISSION_ADAPTER=(
                "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
            ),
        ):
            _ensure_perm_context_on_request(req)

        assert req._mcp_capabilities is None
        assert req._mcp_perm_entry_filter is None

    def test_service_principal_sees_all_tools_in_list(self) -> None:
        """tools/list returns all tools for a service principal even with no capabilities."""
        from frisian_mcp.registry import ToolRegistry
        from frisian_mcp.views import _ensure_perm_context_on_request

        reg = ToolRegistry()
        for name, app, model in [
            ("device_list", "dcim", "device"),
            ("prefix_list", "ipam", "prefix"),
        ]:
            reg.register(
                name=name,
                fn=_noop,
                description=name,
                input_schema={"type": "object"},
                perm_app_label=app,
                perm_model=model,
                perm_drf_action="list",
            )

        req = MagicMock(spec=[])
        user = MagicMock()
        user.is_superuser = False
        user._mcp_is_service_principal = True
        user.get_all_permissions.return_value = set()
        req.user = user

        with override_settings(
            FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True,
            FRISIAN_MCP_PERMISSION_ADAPTER=(
                "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
            ),
        ):
            _ensure_perm_context_on_request(req)
            tools = reg.list_tools(
                max_tier=None,
                entry_filter=req._mcp_perm_entry_filter,
            )

        names = {t["name"] for t in tools}
        assert "device_list" in names
        assert "prefix_list" in names
