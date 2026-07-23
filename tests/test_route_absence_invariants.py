"""
Tests for PR-8 — absence invariants at the wire (WI-1).

"The tool does not exist on this route" must hold across every observable
surface simultaneously, driven through the real ``McpView`` request path:

* **Error parity** — invoking a denied resource through a carved group
  dispatcher returns a response byte-identical to invoking the same resource
  on a route whose registry never discovered it (the constant-name form of
  the invariant: the name is held fixed, only the *reason* for absence
  varies).  Same for a denied whole group vs a never-registered tool.
* **Advertised counts** — the dispatcher description ("N tools across M
  resources") and the ``action='help'`` resource tree are computed from the
  route's filtered set; the two can never disagree.
* **Hints** — operator hints for denied members never appear in ``help``
  output, even though hints live in a separate settings dict from the
  resource tree.
* **Lite escape hatch** — a route-denied or tier-hidden tool invoked with
  ``lite: true`` never gets its ``inputSchema`` attached to its own absence
  error; a route-visible tool keeps the escape hatch working.

PR-11 (`testing` lane) extends this file rather than duplicating it — the PM
ruled one home per invariant.  Its additions: the constant-name parity
invariant at the *flat* tool level (``TestErrorParity``), and
``TestStructuralAbsence``, which introspects ``RouteView.entries`` to prove
absence is *structural* (denied entries never enter the view) rather than only
observable in responses.  The retired skeleton ``test_route_absence.py`` was
deleted when those landed.

All fixtures use neutral names (group ``catalog``, resources ``item`` /
``order``, flat tool ``ping``) per the package-neutrality ruling.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import pytest
from django.test import override_settings

from frisian_mcp.registry import ToolRegistry
from frisian_mcp.route_views import route_views
from tests.test_route_wiring import (
    GATEWAY,
    GATEWAY_ELEVATED,
    _cfg,
    _flat_fn,
    _make_registry,
    _mount,
    _post_jsonrpc,
    _rpc_error,
    _rpc_result,
    _StubUser,
    _tier_hook,
    _tool_names,
)


@pytest.fixture()
def registry() -> ToolRegistry:
    """Return a freshly-built neutral registry (catalog group + ping)."""
    return _make_registry()


@pytest.fixture()
def clean_route_views() -> Generator[Any, None, None]:
    """Snapshot and restore the process-scoped RouteViewRegistry singleton."""
    with route_views._lock:  # noqa: SLF001
        saved = dict(route_views._views)  # noqa: SLF001
    yield route_views
    with route_views._lock:  # noqa: SLF001
        route_views._views = saved  # noqa: SLF001


def _make_registry_without_item() -> ToolRegistry:
    """Return a registry whose ``catalog`` group never discovered ``item``."""
    from frisian_mcp.backends.group_dispatcher import (
        build_group_input_schema,
        make_group_invoke,
    )

    reg = ToolRegistry()
    members = ["order_list"]
    for m in members:
        reg.register(
            name=m,
            fn=_flat_fn(m),
            description=f"flat {m}",
            input_schema={"type": "object", "properties": {}},
            permission_tier="read",
        )
    prefix_set = frozenset({"order"})
    reg.register(
        name="catalog",
        fn=make_group_invoke("catalog", frozenset(members), reg, prefix_set),
        description=(
            f"Group dispatcher for {len(members)} tools across "
            f"{len(prefix_set)} resources. Use action='help' to discover."
        ),
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        is_dispatcher=True,
        group_tool_names=frozenset(members),
    )
    return reg


def _make_registry_without_ping() -> ToolRegistry:
    """Return a neutral registry that never registered the flat ``ping`` tool.

    The baseline for the flat-tool constant-name parity test: ``ping`` is absent
    because it was never discovered, not because a route denied it.
    """
    from frisian_mcp.backends.group_dispatcher import (
        build_group_input_schema,
        make_group_invoke,
    )

    reg = ToolRegistry()
    members = ["item_list", "order_list"]
    for m in members:
        reg.register(
            name=m,
            fn=_flat_fn(m),
            description=f"flat {m}",
            input_schema={"type": "object", "properties": {}},
            permission_tier="read",
        )
    prefix_set = frozenset({"item", "order"})
    reg.register(
        name="catalog",
        fn=make_group_invoke("catalog", frozenset(members), reg, prefix_set),
        description=(
            f"Group dispatcher for {len(members)} tools across "
            f"{len(prefix_set)} resources. Use action='help' to discover."
        ),
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        is_dispatcher=True,
        group_tool_names=frozenset(members),
    )
    return reg


def _call_error_content(response: Any) -> dict[str, Any]:
    """Return the parsed content dict of an isError=true tools/call response."""
    result = _rpc_result(response)
    assert result.get("isError") is True, result
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


def _invoke_catalog(view: Any, path: str, resource: str, action: str) -> Any:
    """Invoke the catalog dispatcher with a resource/action pair."""
    return _post_jsonrpc(
        view,
        path,
        "tools/call",
        {"name": "catalog", "arguments": {"resource": resource, "action": action}},
        user=_StubUser(),
    )


def _help_payload(view: Any, path: str) -> dict[str, Any]:
    """Return the parsed action='help' payload for the catalog dispatcher."""
    response = _post_jsonrpc(
        view,
        path,
        "tools/call",
        {"name": "catalog", "arguments": {"action": "help"}},
        user=_StubUser(),
    )
    result = _rpc_result(response)
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Leak 1 — error parity, constant-name form
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestErrorParity:
    """Denied-on-this-route ≡ never-discovered-here, byte for byte."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_denied_resource_matches_never_discovered_resource(
        self, registry: ToolRegistry
    ) -> None:
        """Deny vs never-discovered, name held constant.

        response(route A, resource='item') where item is DENIED on A is
        byte-identical to response(route A', resource='item') where A' never
        discovered it — only the reason for absence varies.
        """
        denied_view = _mount(
            _cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry
        )
        denied = _invoke_catalog(denied_view, GATEWAY, "item", "list")

        never_view = _mount(_cfg("default", GATEWAY), _make_registry_without_item())
        never = _invoke_catalog(never_view, GATEWAY, "item", "list")

        assert denied.content == never.content

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_denied_whole_group_matches_never_registered_tool(self, registry: ToolRegistry) -> None:
        """A fully-denied group is dropped whole, not rebuilt empty.

        Invoking it returns the constant never-registered message.
        """
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog",)), registry)
        response = _post_jsonrpc(
            view,
            GATEWAY,
            "tools/call",
            {"name": "catalog", "arguments": {"resource": "order", "action": "list"}},
            user=_StubUser(),
        )
        error = _rpc_error(response)
        assert error["data"].startswith("No tool registered with name 'catalog'")

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_denied_resource_never_suggested_on_near_miss(self, registry: ToolRegistry) -> None:
        """A near-miss of a denied resource is not corrected back to it."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        content = _call_error_content(_invoke_catalog(view, GATEWAY, "iten", "list"))
        assert "item" not in content["error"].replace("iten", "")

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_surviving_resource_still_routes(self, registry: ToolRegistry) -> None:
        """The carve-out removes item only; order keeps working."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        response = _invoke_catalog(view, GATEWAY, "order", "list")
        result = _rpc_result(response)
        assert result.get("isError") is False

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_denied_flat_tool_matches_never_registered_flat_tool(
        self, registry: ToolRegistry
    ) -> None:
        """PR-11 gap: the constant-name invariant at the FLAT tool level.

        The class above proves it for a grouped resource; this proves the same
        for a top-level flat name.  `tools/call` on a flat tool `ping` DENIED on
        route A must be byte-identical to `tools/call` on `ping` on a route
        whose registry never registered it — name held constant, only the reason
        for absence varies.  A grouped-only parity suite leaves the flat
        `tools/call` error path unpinned.
        """
        denied_view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("ping",)), registry)
        denied = _post_jsonrpc(
            denied_view,
            GATEWAY,
            "tools/call",
            {"name": "ping", "arguments": {}},
            user=_StubUser(),
        )

        never_view = _mount(_cfg("default", GATEWAY), _make_registry_without_ping())
        never = _post_jsonrpc(
            never_view,
            GATEWAY,
            "tools/call",
            {"name": "ping", "arguments": {}},
            user=_StubUser(),
        )

        assert denied.content == never.content


# ---------------------------------------------------------------------------
# Leak 2 — advertised counts derive from the filtered set
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestAdvertisedCounts:
    """The description count and the help tree can never disagree."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_description_counts_match_the_carved_surface(self, registry: ToolRegistry) -> None:
        """A route that denies item advertises only the surviving counts."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        response = _post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser())
        catalog = next(t for t in _rpc_result(response)["tools"] if t["name"] == "catalog")
        assert "1 tools across 1 resources" in catalog["description"]

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_help_tree_agrees_with_the_advertised_count(self, registry: ToolRegistry) -> None:
        """Help lists exactly the resources the description counted."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        payload = _help_payload(view, GATEWAY)
        assert set(payload["resources"]) == {"order"}

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_intact_group_keeps_global_counts(self, registry: ToolRegistry) -> None:
        """No carve-out: the shared-by-reference entry advertises the full set."""
        view = _mount(_cfg("default", GATEWAY), registry)
        response = _post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser())
        catalog = next(t for t in _rpc_result(response)["tools"] if t["name"] == "catalog")
        assert "3 tools across 2 resources" in catalog["description"]


# ---------------------------------------------------------------------------
# Leak 3 — hints filtered per route
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestHintFiltering:
    """Hints live in a separate settings dict — that separation is the hazard."""

    @override_settings(
        FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"),
        FRISIAN_MCP_TOOL_HINTS={
            "item_list": "List items with pagination.",
            "order_list": "List orders with pagination.",
        },
    )
    def test_denied_resource_hints_absent_from_help(self, registry: ToolRegistry) -> None:
        """A denied member's hint never appears in help output."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        payload = _help_payload(view, GATEWAY)
        hints = payload.get("hints", {})
        assert "item_list" not in hints
        assert "order_list" in hints

    @override_settings(
        FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"),
        FRISIAN_MCP_TOOL_HINTS={
            "item_list": "List items with pagination.",
            "order_list": "List orders with pagination.",
        },
    )
    def test_surviving_hints_still_served_on_intact_group(self, registry: ToolRegistry) -> None:
        """No carve-out: both hints surface as configured."""
        view = _mount(_cfg("default", GATEWAY), registry)
        hints = _help_payload(view, GATEWAY).get("hints", {})
        assert {"item_list", "order_list"} <= set(hints)


# ---------------------------------------------------------------------------
# Lite escape hatch — absence beats self-teaching
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestLiteEscapeHatchAbsence:
    """lite:true must never return the schema of a tool that is absent here."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_route_denied_tool_gets_no_schema_with_lite(self, registry: ToolRegistry) -> None:
        """The absence error must not carry the denied tool's input contract."""
        view = _mount(
            _cfg(
                "elevated",
                GATEWAY_ELEVATED,
                highest_tier="read_write",
                deny=("ping",),
            ),
            registry,
        )
        response = _post_jsonrpc(
            view,
            GATEWAY_ELEVATED,
            "tools/call",
            {"name": "ping", "arguments": {"lite": True}},
            user=_StubUser(),
        )
        error = _rpc_error(response)
        assert "No tool registered with name 'ping'" in str(error["data"])
        assert "inputSchema" not in json.dumps(error)

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_tier_hidden_tool_gets_no_schema_with_lite(self, registry: ToolRegistry) -> None:
        """A tool above the effective tier is absent; lite must agree."""
        # default route, secure-default ceiling `read`: item_create (read_write)
        # is reported nonexistent by dispatch — the lite hatch must not then
        # hand back its schema.
        view = _mount(_cfg("default", GATEWAY), registry)
        response = _post_jsonrpc(
            view,
            GATEWAY,
            "tools/call",
            {"name": "item_create", "arguments": {"lite": True}},
            user=_StubUser(),
        )
        error = _rpc_error(response)
        assert "No tool registered with name 'item_create'" in str(error["data"])
        assert "inputSchema" not in json.dumps(error)

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_visible_tool_keeps_the_escape_hatch(self, registry: ToolRegistry) -> None:
        """A route-visible tool failing validation still self-teaches."""
        view = _mount(_cfg("elevated", GATEWAY_ELEVATED, highest_tier="read_write"), registry)
        # item_create requires "name"; omit it to trigger ToolInputError.
        response = _post_jsonrpc(
            view,
            GATEWAY_ELEVATED,
            "tools/call",
            {"name": "item_create", "arguments": {"lite": True}},
            user=_StubUser(),
        )
        error = _rpc_error(response)
        assert isinstance(error["data"], dict)
        assert "inputSchema" in error["data"]

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_capped_route_help_respects_effective_tier(self, registry: ToolRegistry) -> None:
        """action='help' hides actions above the effective tier (WI-2 reach)."""
        view = _mount(_cfg("default", GATEWAY), registry)
        payload = _help_payload(view, GATEWAY)
        assert "create" not in payload["resources"].get("item", [])
        assert "list" in payload["resources"].get("item", [])


# ---------------------------------------------------------------------------
# tools/list absence (route-level, live endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestDiscoveryAbsence:
    """A denied name never appears in discovery on the carved route."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_denied_flat_tool_absent_from_tools_list(self, registry: ToolRegistry) -> None:
        """Deny ping: absent from discovery, others unaffected."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("ping",)), registry)
        names = _tool_names(_post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser()))
        assert "ping" not in names
        assert "catalog" in names

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_fully_denied_group_absent_from_tools_list(self, registry: ToolRegistry) -> None:
        """Deny the whole group: the dispatcher itself is not listed."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog",)), registry)
        names = _tool_names(_post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser()))
        assert "catalog" not in names
        assert "ping" in names


# ---------------------------------------------------------------------------
# Structural absence — denied entries never enter RouteView.entries at all
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestStructuralAbsence:
    """PR-11 gap: absence is structural, not merely observable.

    The classes above prove the *observable* property — a denied name is absent
    from every response surface (error, counts, hints, discovery).  These
    introspect the built :class:`~frisian_mcp.route_views.RouteView` and assert
    the denied entry never enters ``RouteView.entries`` in the first place.  That
    is the mechanism behind the observable property, and BLOCKER-2 option (a)
    (per-route dispatcher entries) is what makes it assertable: a filter-at-call
    layer would leave the entry present.  Do not weaken these into output checks
    — that would only re-prove what the classes above already cover.
    """

    def test_fully_denied_group_absent_from_route_view_entries(
        self, registry: ToolRegistry
    ) -> None:
        """A wholly-denied group never enters ``entries`` — not present-and-filtered."""
        _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog",)), registry)
        view = route_views.get("default")
        assert view is not None
        assert "catalog" not in view.entries
        assert "ping" in view.entries

    def test_denied_flat_tool_absent_from_route_view_entries(self, registry: ToolRegistry) -> None:
        """A denied flat tool never enters ``entries``."""
        _mount(_cfg("default", GATEWAY, allow=("*",), deny=("ping",)), registry)
        view = route_views.get("default")
        assert view is not None
        assert "ping" not in view.entries
        assert "catalog" in view.entries

    def test_carved_group_entry_is_a_route_local_rebuild(self, registry: ToolRegistry) -> None:
        """A partially-denied group is rebuilt per route, not the shared global entry.

        The dispatcher survives (its resource `order` remains), but as a fresh
        entry whose advertised counts reflect the carve-out — so pruning is
        structural on the entry, not a call-time gate over a shared object.
        """
        _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        carved = route_views.get("default")
        assert carved is not None
        assert "catalog" in carved.entries
        # The rebuilt entry advertises only the surviving resource.
        assert carved.entries["catalog"] is not registry.get_entry("catalog")
