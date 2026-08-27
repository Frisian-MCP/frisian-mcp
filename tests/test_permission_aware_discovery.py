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
    """Build a minimal mock request with a user stub.

    ``has_perm`` MUST be stubbed to answer from *perms*.  Since v1.1.1 the
    default adapter resolves capabilities through ``user.has_perm()`` (V11-11),
    and a bare ``MagicMock`` returns a truthy ``Mock`` for every permission —
    i.e. it would silently grant EVERYTHING and make these filter tests vacuous.
    A faithful stub answers exactly as a real permission backend would.
    """
    granted = perms or set()
    req = MagicMock()
    req.user = MagicMock()
    req.user.is_authenticated = True
    req.user.is_superuser = is_superuser
    req.user.get_all_permissions = lambda: set(granted)
    req.user.has_perm = lambda perm, obj=None: perm in granted
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
    """``DjangoPermissionAdapter`` resolves capabilities via ``user.has_perm()`` (V11-11)."""

    def test_get_capabilities_answers_membership_from_has_perm(self) -> None:
        """Capabilities is a membership container answering from the host predicate."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        adapter = DjangoPermissionAdapter()
        granted = {"dcim.view_device", "ipam.view_prefix"}
        user = MagicMock()
        user.has_perm = lambda perm, obj=None: perm in granted
        caps = adapter.get_capabilities(user)
        assert "dcim.view_device" in caps
        assert "ipam.view_prefix" in caps
        assert "dcim.delete_device" not in caps

    def test_get_capabilities_handles_error(self) -> None:
        """A raising permission backend denies every permission (fail-closed, C6)."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        adapter = DjangoPermissionAdapter()
        user = MagicMock()
        user.has_perm.side_effect = RuntimeError("db down")
        caps = adapter.get_capabilities(user)
        assert "dcim.view_device" not in caps

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

    def test_tool_without_perm_metadata_is_hidden(self) -> None:
        """
        H3 INVERTED: indeterminate capability now HIDES the tool.

        This previously asserted that a tool with no perm metadata was "always
        visible regardless of capabilities" — a discovery control failing open
        the moment metadata was absent.  Absence of evidence is not evidence of
        permission, so the entry is hidden until someone states otherwise.
        """
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
        assert filt(entry) is False

    def test_declared_capability_makes_a_perm_less_tool_filterable(self) -> None:
        """A decorator tool can declare its capability and be filtered on it."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="custom",
            fn=_noop,
            description="declares its capability",
            input_schema={"type": "object"},
            capability="orders.view_order",
        )
        entry = reg.get_entry("custom")
        assert entry is not None

        assert _make_perm_entry_filter(frozenset({"orders.view_order"}))(entry) is True
        assert _make_perm_entry_filter(frozenset({"orders.add_order"}))(entry) is False

    def test_universal_discovery_is_the_explicit_opt_in(self) -> None:
        """
        Universal visibility is reachable, but only by stating it.

        This is the replacement for the old fail-open behaviour: the same
        outcome, now a deliberate declaration rather than the accidental result
        of missing metadata.
        """
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="healthcheck",
            fn=_noop,
            description="intentionally universal",
            input_schema={"type": "object"},
            universal_discovery=True,
        )
        entry = reg.get_entry("healthcheck")
        assert entry is not None
        assert _make_perm_entry_filter(frozenset())(entry) is True

    def test_dispatcher_is_not_blanket_visible(self) -> None:
        """
        H3 INVERTED: a dispatcher earns visibility like anything else.

        Previously dispatchers returned ``True`` unconditionally on the
        rationale that "per-action filtering happens at call time" — but
        tools/list is precisely where the caller learns the tool exists, so
        deferring to call time disclosed every dispatcher to everyone.
        """
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
        assert _make_perm_entry_filter(frozenset())(entry) is False
        assert _make_perm_entry_filter(frozenset({"dcim.view_device"}))(entry) is True

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
    # H3: a real _ToolEntry defaults these to None/False.  MagicMock would
    # auto-create them as truthy mocks, so the entry would read as "declares
    # universal discovery" and the factory would skip filtering entirely — the
    # fixture, not the code, deciding the outcome.
    entry.capability = None
    entry.universal_discovery = False
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
        entry.capability = None
        entry.universal_discovery = False
        factory = _make_perm_action_filter_factory(frozenset({"dcim.view_device"}))

        # H3 INVERTED: this used to return None, meaning "no filtering", which
        # published the dispatcher's FULL action enum — every write and admin
        # action name — to a caller whose capabilities were unknown.  An
        # indeterminate dispatcher now hides every action, and list_tools then
        # drops the dispatcher as an empty navigation shell.
        action_filter = factory(entry)
        assert action_filter is not None
        assert action_filter("list", MagicMock(backend_action=None)) is False

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

        # H7: pin the MCP tier attributes so this reaches the permission
        # filter under test rather than being denied by the tier gate first.
        req = MagicMock(_mcp_effective_tier=None, _mcp_max_tier=None, auth=None)
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
    The exempt adapter is a DEPRECATED no-op since v1.1.1 (V11-11).

    It used to synthesize ``view_*`` capabilities from ``EXEMPT_VIEW_PERMISSIONS``
    (honouring ``EXEMPT_EXCLUDE_MODELS``).  The package no longer parses either
    setting: capabilities come from ``user.has_perm()``, and the HOST decides
    what is exempt and what is excluded — including its own exclusion list.
    Asking the host is what makes this correct for *any* host mechanism rather
    than one hand-patched setting.
    """

    def _make_user(self, granted: set[str] | None = None, exempt_views: bool = False) -> Any:
        """A user whose has_perm mirrors a host backend: exempt ∨ granted."""
        allowed = granted or set()

        def has_perm(perm: str, obj: Any = None) -> bool:
            # The host excludes auth.* from its exemption; everything else is exempt.
            if exempt_views and perm.startswith("dcim.view_"):
                return True
            return perm in allowed

        user = MagicMock()
        user.is_anonymous = False
        user.is_superuser = False
        user.has_perm = has_perm
        return user

    def test_host_exclusion_decision_is_honoured(self) -> None:
        """A model the HOST excludes from its exemption stays denied."""
        from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

        caps = DjangoPermissionAdapter().get_capabilities(self._make_user(exempt_views=True))
        assert "dcim.view_device" in caps  # host says exempt -> invocable -> visible
        assert "auth.view_group" not in caps  # host excludes it -> denied
        assert "auth.view_permission" not in caps

    def test_deprecated_adapter_defers_to_the_default(self) -> None:
        """The old adapter still boots (warned) and adds no synthesis of its own."""
        import warnings

        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            adapter = ExemptViewPermissionAdapter()

        caps = adapter.get_capabilities(self._make_user(exempt_views=True))
        assert "dcim.view_device" in caps
        assert "auth.view_group" not in caps


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


class TestH3FailClosedGaps:
    """
    The three gaps H3 names, each previously fail-open.

    The branch that already worked — a group dispatcher with perm-aware
    children — is covered by ``TestGroupDispatcherVisibility`` and is
    deliberately not re-tested here.
    """

    def test_group_of_only_perm_less_tools_is_hidden(self) -> None:
        """
        GAP: a group whose children all lack perm metadata was universally visible.

        ``list_tools`` used to consider only children with perm_app_label AND
        perm_model.  A group assembled entirely from perm-less tools had none,
        so the guard never fired and the group was advertised to every caller
        no matter what its children required.
        """
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="permless_child",
            fn=_noop,
            description="no perm metadata",
            input_schema={"type": "object"},
            hidden=True,
        )
        reg.register(
            name="permless_group",
            fn=_noop,
            description="group of perm-less tools",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({"permless_child"}),
        )

        tools = reg.list_tools(entry_filter=_make_perm_entry_filter(frozenset()))
        assert "permless_group" not in {t["name"] for t in tools}

    def test_group_visible_when_a_perm_less_child_declares_itself(self) -> None:
        """A declared child is a legitimate reason to show the group."""
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="permless_child",
            fn=_noop,
            description="declares a capability",
            input_schema={"type": "object"},
            capability="catalog.view_item",
            hidden=True,
        )
        reg.register(
            name="permless_group",
            fn=_noop,
            description="group of perm-less tools",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({"permless_child"}),
        )

        holder = _make_perm_entry_filter(frozenset({"catalog.view_item"}))
        assert "permless_group" in {t["name"] for t in reg.list_tools(entry_filter=holder)}

        nonholder = _make_perm_entry_filter(frozenset({"catalog.add_item"}))
        assert "permless_group" not in {t["name"] for t in reg.list_tools(entry_filter=nonholder)}

    def _class_dispatcher_registry(self, **register_kwargs: Any) -> ToolRegistry:
        from frisian_mcp.backends.dispatcher import DispatcherMeta

        reg = ToolRegistry()
        meta = DispatcherMeta(
            name="items",
            description="class dispatcher",
            actions=_crud_actions(),
        )
        reg.register(
            name="items",
            fn=_noop,
            description="class dispatcher",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
            **register_kwargs,
        )
        return reg

    def test_indeterminate_class_dispatcher_is_dropped_not_published_unfiltered(self) -> None:
        """
        GAP: a class dispatcher with no perm metadata published its FULL action enum.

        The action-filter factory returned ``None`` — "no filtering" — so a
        caller whose capabilities were unknown was handed the names of every
        write and admin action on the resource.  Now every action is hidden and
        the dispatcher is dropped as the empty navigation shell it would be.
        """
        from frisian_mcp.views import _make_perm_action_filter_factory, _make_perm_entry_filter

        reg = self._class_dispatcher_registry()
        tools = reg.list_tools(
            entry_filter=_make_perm_entry_filter(frozenset()),
            action_filter_factory=_make_perm_action_filter_factory(frozenset()),
        )
        assert "items" not in {t["name"] for t in tools}

    def test_declared_class_dispatcher_filters_its_action_enum(self) -> None:
        """A declared capability base resolves per-action verbs, so the enum narrows."""
        from frisian_mcp.views import _make_perm_action_filter_factory, _make_perm_entry_filter

        reg = self._class_dispatcher_registry(capability="catalog.item")
        caps = frozenset({"catalog.view_item"})
        tools = reg.list_tools(
            entry_filter=_make_perm_entry_filter(caps),
            action_filter_factory=_make_perm_action_filter_factory(caps),
        )

        listed = {t["name"]: t for t in tools}
        assert "items" in listed
        actions = set(listed["items"]["inputSchema"]["properties"]["action"]["enum"])
        # view-only: the read actions survive, the write ones do not.
        assert {"list", "retrieve"} <= actions
        assert not actions & {"create", "update", "destroy"}

    def test_universal_class_dispatcher_publishes_every_action(self) -> None:
        """Declaring universal discovery is what publishing the full enum now requires."""
        from frisian_mcp.views import _make_perm_action_filter_factory, _make_perm_entry_filter

        reg = self._class_dispatcher_registry(universal_discovery=True)
        tools = reg.list_tools(
            entry_filter=_make_perm_entry_filter(frozenset()),
            action_filter_factory=_make_perm_action_filter_factory(frozenset()),
        )

        listed = {t["name"]: t for t in tools}
        assert "items" in listed
        actions = set(listed["items"]["inputSchema"]["properties"]["action"]["enum"])
        assert {"list", "retrieve", "create", "update", "destroy"} <= actions


class TestW015IndeterminateCapability:
    """
    H3 item 5: hiding must be loud.

    Fail-closed turns a misconfiguration into a tool that silently stops
    appearing in ``tools/list``.  That is the safe direction but a miserable one
    to debug from the outside, so the operator is told at startup.
    """

    _NAME = "_w015_probe_tool"

    def _register(self, **kwargs: Any) -> None:
        from frisian_mcp.registry import tool_registry

        tool_registry.register(
            name=self._NAME,
            fn=_noop,
            description="probe",
            input_schema={"type": "object"},
            **kwargs,
        )

    def _unregister(self) -> None:
        from frisian_mcp.registry import tool_registry

        tool_registry._tools.pop(self._NAME, None)  # pylint: disable=protected-access

    def _w015(self) -> list[Any]:
        from frisian_mcp.checks import (
            W015_INDETERMINATE_CAPABILITY,
            check_permission_aware_discovery,
        )

        return [
            e for e in check_permission_aware_discovery() if e.id == W015_INDETERMINATE_CAPABILITY
        ]

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_indeterminate_entry_is_reported(self) -> None:
        """A tool with no derivable and no declared capability is named."""
        self._register()
        try:
            warnings = self._w015()
            assert len(warnings) == 1
            assert self._NAME in warnings[0].msg
            assert "HIDDEN" in warnings[0].msg
        finally:
            self._unregister()

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_declared_capability_clears_the_warning(self) -> None:
        """Declaring the capability is one of the two ways to resolve it."""
        self._register(capability="catalog.view_item")
        try:
            assert not [w for w in self._w015() if self._NAME in w.msg]
        finally:
            self._unregister()

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True)
    def test_universal_discovery_clears_the_warning(self) -> None:
        """So is stating that the tool is meant to be universally visible."""
        self._register(universal_discovery=True)
        try:
            assert not [w for w in self._w015() if self._NAME in w.msg]
        finally:
            self._unregister()

    @override_settings(FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=False)
    def test_silent_when_the_feature_is_off(self) -> None:
        """Nothing is hidden when the filter never runs, so nothing is reported."""
        self._register()
        try:
            assert self._w015() == []
        finally:
            self._unregister()


class TestH3OneLensAllConsumers:
    """
    H3 REOPENED: every consumer of "what can this caller see" gives ONE answer.

    The first H3 pass fixed only ``tools/list`` and left three consumers on a
    sibling helper whose indeterminate branch returned ``None`` — meaning *no
    filtering*.  On the same indeterminate dispatcher ``tools/list`` denied
    every action while ``action="help"`` published every action: not a gap, the
    exact inverse of the ruling, reachable on one request.

    These tests pin agreement rather than each consumer's behaviour in
    isolation, because four independently-correct copies is how the defect
    recurred in the first place.
    """

    def _dispatcher(self, **register_kwargs: Any) -> tuple[ToolRegistry, Any]:
        from frisian_mcp.backends.dispatcher import DispatcherMeta

        reg = ToolRegistry()
        meta = DispatcherMeta(name="items", description="class dispatcher", actions=_crud_actions())
        reg.register(
            name="items",
            fn=_noop,
            description="class dispatcher",
            input_schema={"type": "object"},
            is_dispatcher=True,
            dispatcher_meta=meta,
            **register_kwargs,
        )
        return reg, meta

    def _tools_list_actions(self, reg: ToolRegistry, caps: frozenset[str]) -> set[str] | None:
        """Return the action enum tools/list publishes, or None when it drops the tool."""
        from frisian_mcp.views import _make_perm_action_filter_factory, _make_perm_entry_filter

        listed = {
            t["name"]: t
            for t in reg.list_tools(
                entry_filter=_make_perm_entry_filter(caps),
                action_filter_factory=_make_perm_action_filter_factory(caps),
            )
        }
        if "items" not in listed:
            return None
        return set(listed["items"]["inputSchema"]["properties"]["action"]["enum"])

    def _help_actions(self, reg: ToolRegistry, caps: frozenset[str]) -> set[str]:
        """Return the action set `action="help"` discloses."""
        from frisian_mcp.backends.dispatcher import (
            _build_perm_action_filter_from_request,
            _visible_actions,
        )

        request = MagicMock()
        request._mcp_effective_tier = "read"
        request._mcp_capabilities = caps
        request._mcp_max_tier = None
        with patch("frisian_mcp.registry.tool_registry", reg):
            entry = reg.get_entry("items")
            assert entry is not None
            return set(
                _visible_actions(
                    entry.dispatcher_meta,
                    None,
                    action_filter=_build_perm_action_filter_from_request(request, "items"),
                )
            )

    def test_indeterminate_dispatcher_agrees_across_consumers(self) -> None:
        """
        THE INVERSION: tools/list denied everything, help published everything.

        Both must now deny.  ``tools/list`` expresses that by dropping the
        dispatcher (an enum of nothing is an empty navigation shell); help
        expresses it as an empty action set. Same verdict, two shapes.
        """
        reg, _ = self._dispatcher()
        caps: frozenset[str] = frozenset()

        assert self._tools_list_actions(reg, caps) is None
        assert self._help_actions(reg, caps) == set()

    def test_declared_capability_agrees_across_consumers(self) -> None:
        """
        W015's own remedy must not produce divergence.

        The old helper checked only ``perm_app_label``/``perm_model``, so a
        dispatcher that followed W015 and declared ``capability`` got a
        filtered tools/list and an unfiltered help on the same request.
        """
        reg, _ = self._dispatcher(capability="catalog.item")
        caps = frozenset({"catalog.view_item"})

        listed = self._tools_list_actions(reg, caps)
        assert listed == self._help_actions(reg, caps)
        assert listed is not None
        assert {"list", "retrieve"} <= listed
        assert not listed & {"create", "update", "destroy"}

    def test_universal_dispatcher_agrees_across_consumers(self) -> None:
        """An explicit universal declaration publishes everything, everywhere."""
        reg, _ = self._dispatcher(universal_discovery=True)
        caps: frozenset[str] = frozenset()

        listed = self._tools_list_actions(reg, caps)
        assert listed == self._help_actions(reg, caps)
        assert listed is not None
        assert {"list", "retrieve", "create", "update", "destroy"} <= listed

    def test_derived_metadata_agrees_across_consumers(self) -> None:
        """The path that already worked keeps working, and still agrees."""
        reg, _ = self._dispatcher(perm_app_label="catalog", perm_model="item")
        caps = frozenset({"catalog.view_item"})

        listed = self._tools_list_actions(reg, caps)
        assert listed == self._help_actions(reg, caps)
        assert listed is not None
        assert {"list", "retrieve"} <= listed
        assert not listed & {"create", "update", "destroy"}

    def test_unknown_action_hint_is_capability_filtered_on_an_uncapped_mount(self) -> None:
        """
        The did-you-mean candidates apply the lens even with no tier cap.

        The suggestion existed to stop an error naming a hidden action back to
        the caller, but the capability half was gated on ``_mcp_max_tier``, so
        an uncapped perm-aware host still suggested from the full action map.
        """
        from frisian_mcp.backends.dispatcher import _build_perm_action_filter_from_request

        reg, meta = self._dispatcher(capability="catalog.item")
        request = MagicMock()
        # Stamp the tier too: the visibility pass computes _caller_rank from it,
        # so a fabricated Mock here would rank as denied and hide every member.
        request._mcp_effective_tier = "read"
        request._mcp_capabilities = frozenset({"catalog.view_item"})
        request._mcp_max_tier = None  # uncapped

        with patch("frisian_mcp.registry.tool_registry", reg):
            action_filter = _build_perm_action_filter_from_request(request, "items")
            assert action_filter is not None
            # 'destroy' is a real action the caller cannot see, so it must not
            # be a suggestion candidate for a near-miss like 'destro'.
            assert action_filter("retrieve", meta.actions["retrieve"]) is True
            assert action_filter("destroy", meta.actions["destroy"]) is False

    def test_group_404_hint_does_not_name_a_hidden_resource(self) -> None:
        """
        The fourth consumer: discovery hid the resource, the error named it back.

        Measured before the fix, with a caller holding nothing:
        tools/list was empty and the near-miss still answered
        "Did you mean resource='secret'?" — with the correct spelling.
        """
        from frisian_mcp.backends.group_dispatcher import make_group_invoke
        from frisian_mcp.views import _make_perm_entry_filter

        reg = ToolRegistry()
        reg.register(
            name="secret_list",
            fn=_noop,
            description="hidden by H3",
            input_schema={"type": "object"},
            hidden=True,
        )
        reg.register(
            name="svc",
            fn=_noop,
            description="group",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({"secret_list"}),
        )

        caps: frozenset[str] = frozenset()
        assert not {t["name"] for t in reg.list_tools(entry_filter=_make_perm_entry_filter(caps))}

        invoke = make_group_invoke(
            "svc", frozenset({"secret_list"}), reg, resource_prefixes=frozenset({"secret"})
        )
        request = MagicMock()
        request._mcp_effective_tier = "read"
        request._mcp_capabilities = caps
        request._mcp_max_tier = None

        with pytest.raises(LookupError) as exc:
            invoke({"resource": "secre", "action": "list", "params": {}}, request)
        assert "secret" not in str(exc.value)

    def test_group_404_hint_still_helps_a_caller_who_can_see_the_resource(self) -> None:
        """Filtering the candidates must not make the hint useless to a legitimate caller."""
        from frisian_mcp.backends.group_dispatcher import make_group_invoke

        reg = ToolRegistry()
        reg.register(
            name="secret_list",
            fn=_noop,
            description="visible to a holder",
            input_schema={"type": "object"},
            capability="catalog.view_item",
            hidden=True,
        )
        invoke = make_group_invoke(
            "svc", frozenset({"secret_list"}), reg, resource_prefixes=frozenset({"secret"})
        )
        request = MagicMock()
        request._mcp_effective_tier = "read"
        request._mcp_capabilities = frozenset({"catalog.view_item"})
        request._mcp_max_tier = None

        with pytest.raises(LookupError) as exc:
            invoke({"resource": "secre", "action": "list", "params": {}}, request)
        assert "Did you mean resource='secret'?" in str(exc.value)


class TestH15GroupVisibleToNonSuperuser:
    """
    H15 REGRESSION: a non-superuser with ``view_*`` caps must still see the group.

    Nothing asserted this, which is why a full green suite shipped a change that
    emptied ``tools/list`` for every non-superuser client on a per-route mount.
    Superusers were unaffected — ``is_unrestricted`` short-circuits to
    ``entry_filter=None`` — so every superuser-tied check stayed green too.

    Asserted on **both** listing paths deliberately.  The defect was that they
    were two implementations and H3's group fix reached only one; a test that
    exercised either alone would have missed it exactly as the suite did.
    """

    _CAPS = frozenset({"catalog.view_item"})

    def _registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        for action, _verb in (("list", "view"), ("create", "add")):
            reg.register(
                name=f"item_{action}",
                fn=_noop,
                description=f"item {action}",
                input_schema={"type": "object"},
                perm_app_label="catalog",
                perm_model="item",
                perm_drf_action=action,
                permission_tier="read_write" if action == "create" else "read",
                hidden=True,
            )
        reg.register(
            name="catalog",
            fn=_noop,
            description="group dispatcher",
            input_schema={"type": "object"},
            is_dispatcher=True,
            group_tool_names=frozenset({"item_list", "item_create"}),
        )
        return reg

    def _filters(self, caps: frozenset[str]) -> tuple[Any, Any]:
        from frisian_mcp.views import _make_perm_action_filter_factory, _make_perm_entry_filter

        return _make_perm_entry_filter(caps), _make_perm_action_filter_factory(caps)

    def test_registry_path_shows_the_group(self) -> None:
        """``ToolRegistry.list_tools`` — the path that already had H3's fix."""
        reg = self._registry()
        entry_filter, action_factory = self._filters(self._CAPS)
        names = {
            t["name"]
            for t in reg.list_tools(entry_filter=entry_filter, action_filter_factory=action_factory)
        }
        assert "catalog" in names

    def test_route_path_shows_the_group(self) -> None:
        """
        ``route_views._list_entries`` — the path a real deployment serves from.

        This is the path that did NOT apply the fail-closed lens.

        A group dispatcher carries no ``perm_app_label``, no ``perm_model``, no
        ``capability`` and no ``dispatcher_meta``, so the fail-closed entry
        filter rejects it outright.  Its capability lives in its children, and
        only the group branch knows to look there.
        """
        from frisian_mcp.route_views import _list_entries

        reg = self._registry()
        entry_filter, action_factory = self._filters(self._CAPS)
        entries = {n: reg.get_entry(n) for n in ("item_list", "item_create", "catalog")}
        names = {
            t["name"]
            for t in _list_entries(
                entries,
                max_tier="read",
                entry_filter=entry_filter,
                action_filter_factory=action_factory,
            )
        }
        assert "catalog" in names

    def test_both_paths_agree(self) -> None:
        """
        Parity, so the two can never diverge again without a test going red.

        H15 existed because one listing path was fixed and its documented
        mirror was not.  Asserting agreement fails whichever side drifts.
        """
        from frisian_mcp.route_views import _list_entries

        reg = self._registry()
        entry_filter, action_factory = self._filters(self._CAPS)
        entries = {n: reg.get_entry(n) for n in ("item_list", "item_create", "catalog")}

        via_registry = {
            t["name"]
            for t in reg.list_tools(
                max_tier="read", entry_filter=entry_filter, action_filter_factory=action_factory
            )
        }
        via_route = {
            t["name"]
            for t in _list_entries(
                entries,
                max_tier="read",
                entry_filter=entry_filter,
                action_filter_factory=action_factory,
            )
        }
        assert via_registry == via_route

    @pytest.mark.parametrize("listing_path", ["registry", "route"])
    @pytest.mark.parametrize(
        ("max_tier", "caps", "expected_resources"),
        [
            (
                "read",
                frozenset({"catalog.view_item", "catalog.add_item"}),
                {"item": ["list"]},
            ),
            ("read_write", frozenset({"catalog.view_item"}), {"item": ["list"]}),
            (
                "read_write",
                frozenset({"catalog.view_item", "catalog.add_item"}),
                {"item": ["create", "list"]},
            ),
        ],
    )
    def test_filtered_group_description_matches_help(
        self,
        listing_path: str,
        max_tier: str,
        caps: frozenset[str],
        expected_resources: dict[str, list[str]],
    ) -> None:
        """Descriptions and help agree across route and principal combinations."""
        from frisian_mcp.backends.group_dispatcher import build_group_help
        from frisian_mcp.route_views import _list_entries

        reg = self._registry()
        entry_filter, action_factory = self._filters(caps)
        if listing_path == "registry":
            listing = reg.list_tools(
                max_tier=max_tier,
                entry_filter=entry_filter,
                action_filter_factory=action_factory,
            )
        else:
            entries = {n: reg.get_entry(n) for n in ("item_list", "item_create", "catalog")}
            listing = _list_entries(
                entries,
                max_tier=max_tier,
                entry_filter=entry_filter,
                action_filter_factory=action_factory,
            )

        action_count = sum(len(actions) for actions in expected_resources.values())
        expected = (
            f"Group dispatcher for {action_count} tools across "
            f"{len(expected_resources)} resources. Use action='help' to discover."
        )
        group = next(tool for tool in listing if tool["name"] == "catalog")
        assert group["description"] == expected

        help_payload = build_group_help(
            "catalog",
            ["item_list", "item_create"],
            reg,
            max_tier=max_tier,
            resource_prefixes=frozenset({"item"}),
            entry_filter=entry_filter,
        )
        assert help_payload["resources"] == expected_resources

    @override_settings(FRISIAN_MCP_DISPATCH_GROUPS={"catalog": ["item", "missing"]})
    def test_group_description_ignores_unresolved_resource_prefixes(self) -> None:
        """A configured resource with no registered child is not advertised."""
        reg = self._registry()
        listed = reg.list_tools(max_tier="read")
        group = next(tool for tool in listed if tool["name"] == "catalog")
        assert group["description"] == (
            "Group dispatcher for 1 tools across 1 resources. Use action='help' to discover."
        )

    def test_group_still_hidden_when_no_child_is_visible(self) -> None:
        """The fix must not become fail-open: no usable child, no group."""
        from frisian_mcp.route_views import _list_entries

        reg = self._registry()
        entry_filter, action_factory = self._filters(frozenset({"other.view_thing"}))
        entries = {n: reg.get_entry(n) for n in ("item_list", "item_create", "catalog")}
        names = {
            t["name"]
            for t in _list_entries(
                entries,
                max_tier="read",
                entry_filter=entry_filter,
                action_filter_factory=action_factory,
            )
        }
        assert "catalog" not in names
