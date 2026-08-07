"""Tests for FRISIAN_MCP_DISPATCH_GROUPS group dispatcher."""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from frisian_mcp.apps import _install_dispatch_groups
from frisian_mcp.backends.group_dispatcher import (
    build_group_help,
    build_group_input_schema,
    make_group_invoke,
)
from frisian_mcp.registry import ToolInputError, ToolRegistry

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
    """Return a registry pre-populated with svc and network flat tools."""
    reg = ToolRegistry()
    flat = {
        "item_list": _stub_tool("item.list"),
        "item_retrieve": _stub_tool("item.retrieve"),
        "item_create": _stub_tool("item.create"),
        "container_list": _stub_tool("container.list"),
        "endpoint_list": _stub_tool("endpoint.list"),
        "ipaddress_list": _stub_tool("ipaddress.list"),
        "prefix_list": _stub_tool("prefix.list"),
        "user_list": _stub_tool("user.list"),  # ungrouped
    }
    schema = {"type": "object", "properties": {}}
    for name, fn in flat.items():
        # Write actions need read_write tier so we can exercise tier filtering
        # in the help response.
        tier = "read_write" if name.endswith(("_create", "_update", "_destroy")) else "read"
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
    """
    The dispatcher schema stays small — no resource/action enumeration.

    "Small" means it does not grow with the number of bundled tools.  The fixed
    response-negotiation fields are exempt: they are a constant five regardless
    of group size, and without them a continuation token cannot be redeemed.
    """

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

    def test_schema_discloses_negotiation_protocol(self) -> None:
        """
        All five negotiation fields are published at the top level.

        ADR-005 line 61 defines redemption as re-invoking the same tool with a
        ``continuation_token`` and a ``mode``.  For a grouped tool that same
        tool is the dispatcher, so omitting these leaves a caller holding a
        token with no legal slot to send it back in — minted, never redeemable.
        """
        props = build_group_input_schema()["properties"]
        for key in ("continuation_token", "mode", "page", "page_size", "filter_keys"):
            assert key in props, f"{key!r} missing from group dispatcher schema"

    def test_schema_mode_enum_matches_served_modes(self) -> None:
        """The advertised modes are exactly the ones _serve_heavy_mode implements."""
        mode = build_group_input_schema()["properties"]["mode"]
        assert mode["enum"] == ["summary", "paginated", "filtered", "full"]

    def test_negotiation_placement_text_is_shape_neutral(self) -> None:
        """
        The token's placement text must not enumerate sibling argument names.

        The same description is merged into three argument shapes — flat
        ``@mcp_heavy`` (no ``action``, no ``params``), class dispatcher
        (``action`` + ``params``) and group dispatcher (``resource`` +
        ``action`` + ``params``) — so naming siblings is wrong for at least one.
        """
        desc = build_group_input_schema()["properties"]["continuation_token"]["description"]
        assert "sibling" not in desc.lower()
        assert "params" in desc


# ---------------------------------------------------------------------------
# build_group_help
# ---------------------------------------------------------------------------


class TestBuildGroupHelp:
    """Help payload structure and tier filtering."""

    def test_help_returns_resource_action_tree(self, populated_registry: ToolRegistry) -> None:
        """Help groups actions by resource and sorts them."""
        help_payload = build_group_help(
            "svc",
            ["item_list", "item_retrieve", "container_list"],
            populated_registry,
        )
        assert help_payload["help"] is True
        assert help_payload["group"] == "svc"
        assert help_payload["resources"]["item"] == ["list", "retrieve"]
        assert help_payload["resources"]["container"] == ["list"]

    def test_help_filters_actions_by_tier(self, populated_registry: ToolRegistry) -> None:
        """A 'read' caller does not see write actions like item_create."""
        help_payload = build_group_help(
            "svc",
            ["item_list", "item_retrieve", "item_create"],
            populated_registry,
            max_tier="read",
        )
        assert "create" not in help_payload["resources"].get("item", [])
        assert "list" in help_payload["resources"]["item"]

    def test_help_unfiltered_includes_write_actions(self, populated_registry: ToolRegistry) -> None:
        """Without a max_tier, every action is listed."""
        help_payload = build_group_help(
            "svc",
            ["item_list", "item_create"],
            populated_registry,
        )
        assert "create" in help_payload["resources"]["item"]

    def test_help_skips_unknown_tools(self, populated_registry: ToolRegistry) -> None:
        """Tool names not in the registry are silently dropped."""
        help_payload = build_group_help(
            "svc",
            ["item_list", "ghost_list"],
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
            "svc",
            frozenset({"item_list", "container_list"}),
            populated_registry,
        )
        result = invoke(
            {"resource": "item", "action": "list", "params": {}},
            _request(rf, permission="read"),
        )
        assert result["called"] == "item.list"

    def test_params_nested_continuation_token_is_rejected(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        A token nested in ``params`` is rejected loudly, not forwarded.

        Without the guard the token reaches the underlying action as a filter
        field.  A strict host rejects it as an unknown filter; a lenient host
        runs the query and mints a *second* token, so retrying the "failed"
        redemption loops.  Either way the caller is never told the key is real
        and merely misplaced.
        """
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
            populated_registry,
        )
        with pytest.raises(ToolInputError) as exc:
            invoke(
                {
                    "resource": "item",
                    "action": "list",
                    "params": {"continuation_token": "abc123"},
                },
                _request(rf, permission="read"),
            )
        assert "top level" in str(exc.value).lower()

    def test_flat_form_keeps_resource_out_of_params(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        The flat-form sweep excludes ``resource`` as well as ``action``.

        The class dispatcher's exclusion tuple has no ``resource`` because it
        has no such concept; copying it here would sweep the routing key into
        params and break every flat-form group call.
        """
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
            populated_registry,
        )
        result = invoke(
            {"resource": "item", "action": "list", "name": "widget"},
            _request(rf, permission="read"),
        )
        assert result["arguments"] == {"name": "widget"}

    def test_flat_form_keeps_continuation_token_out_of_params(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        A top-level token is never reclassified as an action filter (T6).

        ``views.py`` short-circuits a valid top-level token before dispatch, so
        this is belt-and-braces — it matters for an expired or foreign token,
        where the caller must get the expiry/owner error rather than a filter
        error naming a key they placed correctly.
        """
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
            populated_registry,
        )
        result = invoke(
            {
                "resource": "item",
                "action": "list",
                "continuation_token": "abc123",
                "name": "widget",
            },
            _request(rf, permission="read"),
        )
        assert result["arguments"] == {"name": "widget"}

    def test_flat_form_passes_through_colliding_negotiation_fields(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        ``mode``/``page``/``page_size``/``filter_keys`` reach the action untouched.

        Only ``continuation_token`` can never be a legitimate filter.  The other
        four collide with real host data — a model field named ``mode``, DRF's
        own ``page``/``page_size`` — so excluding them "for symmetry" would
        silently drop real query parameters.
        """
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
            populated_registry,
        )
        result = invoke(
            {
                "resource": "item",
                "action": "list",
                "mode": "access",
                "page": 2,
                "page_size": 50,
                "filter_keys": ["a"],
            },
            _request(rf, permission="read"),
        )
        assert result["arguments"] == {
            "mode": "access",
            "page": 2,
            "page_size": 50,
            "filter_keys": ["a"],
        }

    def test_help_response_when_action_missing(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Omitting action returns the help tree."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list", "container_list"}),
            populated_registry,
        )
        result = invoke({}, _request(rf, permission="read"))
        assert result["help"] is True
        assert result["group"] == "svc"

    def test_explicit_help_action(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """action='help' returns the help tree even when resource is set."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
            populated_registry,
        )
        result = invoke(
            {"action": "help", "resource": "item"},
            _request(rf, permission="read"),
        )
        assert result["help"] is True

    def test_unknown_resource_raises_lookup_error(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Unknown resource yields LookupError with a 'did you mean' hint."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list", "container_list"}),
            populated_registry,
        )
        with pytest.raises(LookupError) as exc:
            invoke(
                {"resource": "ite", "action": "list", "params": {}},
                _request(rf, permission="read"),
            )
        assert "Did you mean" in str(exc.value)

    def test_missing_resource_raises_value_error(
        self, populated_registry: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Non-help action without resource raises ValueError."""
        invoke = make_group_invoke(
            "svc",
            frozenset({"item_list"}),
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
            "svc",
            frozenset({"item_list"}),
            populated_registry,
        )
        result = invoke(
            {"resource": "item", "action": "list", "filter": "x"},
            _request(rf, permission="read"),
        )
        # The flat 'filter' kwarg is forwarded as part of params to the tool.
        assert result["arguments"] == {"filter": "x"}


# ---------------------------------------------------------------------------
# _install_dispatch_groups — apps.py integration
# ---------------------------------------------------------------------------


class TestInstallDispatchGroups:
    """End-to-end behaviour when FRISIAN_MCP_DISPATCH_GROUPS is set."""

    def test_no_setting_is_noop(self, populated_registry: ToolRegistry, settings: Any) -> None:
        """Without the setting the registry is left untouched."""
        if hasattr(settings, "FRISIAN_MCP_DISPATCH_GROUPS"):
            del settings.FRISIAN_MCP_DISPATCH_GROUPS
        with (
            patch("frisian_mcp.apps.tool_registry", populated_registry, create=True),
            patch("frisian_mcp.registry.tool_registry", populated_registry),
        ):
            group_count, bundled_count = _install_dispatch_groups()
        assert group_count == 0
        assert bundled_count == 0

    def test_registers_one_dispatcher_per_group(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """A two-group setting yields two new dispatcher tools."""
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {
            "svc": ["item", "container", "endpoint"],
            "ipam": ["ipaddress", "prefix"],
        }
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            group_count, bundled_count = _install_dispatch_groups()
        assert group_count == 2
        # bundled_count should equal the number of distinct flat tools matched
        # by either group's prefixes — we don't pin an exact integer here
        # because the populated_registry fixture may evolve, but it must be > 0
        # whenever any groups were registered.
        assert bundled_count > 0
        assert populated_registry.get_entry("svc") is not None
        assert populated_registry.get_entry("ipam") is not None

    def test_grouped_tools_hidden_from_list_tools(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """Tools bundled under a group disappear from list_tools()."""
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {
            "svc": ["item", "container", "endpoint"],
        }
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        names = {t["name"] for t in populated_registry.list_tools()}
        assert "item_list" not in names
        assert "container_list" not in names
        assert "svc" in names

    def test_ungrouped_tools_remain_visible(
        self, populated_registry: ToolRegistry, settings: Any
    ) -> None:
        """Resources not in any group keep appearing as flat tools."""
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {
            "svc": ["item", "container", "endpoint"],
        }
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        names = {t["name"] for t in populated_registry.list_tools()}
        assert "user_list" in names  # user is ungrouped
        # ipam tools also remain visible (no group claimed them)
        assert "ipaddress_list" in names

    def test_grouped_tools_still_dispatchable_by_name(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """Hidden tools remain reachable through registry.dispatch()."""
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {"svc": ["item"]}
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        result = populated_registry.dispatch(_request(rf, permission="read"), "item_list", {})
        assert result["called"] == "item.list"

    def test_empty_group_logs_warning_and_skips(
        self,
        populated_registry: ToolRegistry,
        settings: Any,
        caplog: Any,
    ) -> None:
        """A group whose prefixes match no tools is skipped with a warning."""
        import logging

        settings.FRISIAN_MCP_DISPATCH_GROUPS = {"empty": ["doesnotexist"]}
        with (
            patch("frisian_mcp.registry.tool_registry", populated_registry),
            caplog.at_level(logging.WARNING, logger="frisian_mcp.apps"),
        ):
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
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {"svc": ["item"]}
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        result = populated_registry.dispatch(
            _request(rf, permission="read"),
            "svc",
            {"resource": "item", "action": "list", "params": {}},
        )
        assert result["called"] == "item.list"


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
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {"svc": ["item"]}
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        with pytest.raises(PermissionError):
            populated_registry.dispatch(
                _request(rf, permission="read"),
                "svc",
                {"resource": "item", "action": "create", "params": {}},
            )

    def test_read_write_caller_can_invoke_write_tool(
        self,
        populated_registry: ToolRegistry,
        settings: Any,
        rf: RequestFactory,
    ) -> None:
        """A 'read_write' caller can route to a 'read_write' tool."""
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {"svc": ["item"]}
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()
        result = populated_registry.dispatch(
            _request(rf, permission="read_write"),
            "svc",
            {"resource": "item", "action": "create", "params": {}},
        )
        assert result["called"] == "item.create"


# ---------------------------------------------------------------------------
# V11-20 (F3) — invoke-side absence on a tier-capped route
# ---------------------------------------------------------------------------


class TestGroupInvokeCappedAbsence:
    """On a capped route an above-ceiling action is ABSENT at invoke (V11-20/F3).

    Discovery already hides above-ceiling actions; strict WI-2 requires the
    invoke path to agree — the tier cap defines what exists on the door, the
    permission-aware member filter only refines what exists.
    """

    @staticmethod
    def _capped_request(rf: RequestFactory) -> Any:
        """An anonymous (read-tier) request on a read-capped route."""
        req = _request(rf, permission=None)
        req._mcp_max_tier = "read"
        return req

    def _installed(self, populated_registry: ToolRegistry, settings: Any) -> None:
        settings.FRISIAN_MCP_DISPATCH_GROUPS = {"svc": ["item"]}
        with patch("frisian_mcp.registry.tool_registry", populated_registry):
            _install_dispatch_groups()

    def test_above_ceiling_action_is_absent_not_permission_error(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """A capped read caller invoking a write action gets the unknown-tool absence."""
        self._installed(populated_registry, settings)
        with pytest.raises(LookupError) as excinfo:
            populated_registry.dispatch(
                self._capped_request(rf),
                "svc",
                {"resource": "item", "action": "create", "params": {}},
            )
        assert not isinstance(excinfo.value, PermissionError)
        assert "Unknown tool 'item_create' in group 'svc'" in str(excinfo.value)
        assert "permission" not in str(excinfo.value)

    def test_absence_message_matches_never_existed_template(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """Above-ceiling and never-existed actions share one error template byte-for-byte."""
        self._installed(populated_registry, settings)
        with pytest.raises(LookupError) as above:
            populated_registry.dispatch(
                self._capped_request(rf),
                "svc",
                {"resource": "item", "action": "create", "params": {}},
            )
        with pytest.raises(LookupError) as missing:
            populated_registry.dispatch(
                self._capped_request(rf),
                "svc",
                {"resource": "item", "action": "zzznope", "params": {}},
            )
        assert str(above.value).replace("item_create", "TOOL") == str(missing.value).replace(
            "item_zzznope", "TOOL"
        )

    def test_permission_filter_is_not_consulted_for_above_ceiling_action(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """Absence precedes the permission-aware filter — it must not fire (or leak) first."""
        self._installed(populated_registry, settings)
        req = self._capped_request(rf)
        perm_filter = MagicMock(return_value=False)
        req._mcp_perm_entry_filter = perm_filter
        with pytest.raises(LookupError):
            populated_registry.dispatch(
                req, "svc", {"resource": "item", "action": "create", "params": {}}
            )
        perm_filter.assert_not_called()

    def test_within_ceiling_action_keeps_naming_permission_error(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """A Django-permission denial on a VISIBLE action stays a 403 that names it.

        That error is actionable (the action exists on this door; the caller's
        account lacks a grant), so converting it to absence would hide real
        remediation — the ruling only converts above-ceiling actions.
        """
        self._installed(populated_registry, settings)
        req = self._capped_request(rf)
        req._mcp_perm_entry_filter = MagicMock(return_value=False)
        with pytest.raises(PermissionError) as excinfo:
            populated_registry.dispatch(
                req, "svc", {"resource": "item", "action": "list", "params": {}}
            )
        assert "'item'/'list'" in str(excinfo.value)

    def test_uncapped_mount_keeps_tier_permission_error(
        self, populated_registry: ToolRegistry, settings: Any, rf: RequestFactory
    ) -> None:
        """Legacy uncapped mounts keep the explicit tier error (no absence contract)."""
        self._installed(populated_registry, settings)
        with pytest.raises(PermissionError):
            populated_registry.dispatch(
                _request(rf, permission="read"),
                "svc",
                {"resource": "item", "action": "create", "params": {}},
            )
