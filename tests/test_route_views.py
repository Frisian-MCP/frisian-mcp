"""
Tests for :mod:`frisian_mcp.route_views` (PR-6).

Covers the core construction contract the downstream PRs consume:

* the absence property — a denied tool is absent from ``entries``, from
  discovery, and from invocation, byte-identical to a never-registered tool;
* the four dispatcher prune sites (member set, resource prefixes, advertised
  count, ``group_tool_names``), and that a fully-denied group is dropped whole;
* dispatch parity — the route delegates flat tools to its backing registry and
  invokes a rebuilt dispatcher through its own pruned closure;
* rebuild atomicity — a view is swapped as one assignment, and an in-flight
  reader keeps its snapshot;
* the permission resolver, the anonymous-reachability predicates, and the
  ``AllowAny``-subclass bucket predicate.

All fixtures use neutral names (group ``catalog``, resources ``item`` /
``order``, flat tool ``ping``) per the package-neutrality ruling.  Member tool
names are resource-leading (``item_list``), never group-leading — that is the
shape ``apps._install_dispatch_groups`` actually produces.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.permissions import (
    AllowAny,
    IsAdminUser,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
)

from frisian_mcp.registry import ToolNotFoundError, ToolRegistry
from frisian_mcp.route_config import RouteConfig
from frisian_mcp.route_views import (
    BUCKET_ANONYMOUS_GRANTING,
    BUCKET_AUTH_REQUIRING,
    BUCKET_OPAQUE,
    BUCKET_PARTIAL_ANONYMOUS,
    LEGACY_ROUTE_NAME,
    RouteView,
    RouteViewRegistry,
    _bucket,
    _is_anonymous_granting,
    route_effective_permission_classes,
    route_is_anonymous_reachable,
    route_is_anonymous_sse_reachable,
)

SEP = "_"


# ---------------------------------------------------------------------------
# Fixtures — a registry mirroring the post-_install_dispatch_groups shape
# ---------------------------------------------------------------------------


def _flat_fn(name: str) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    def fn(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
        return {"tool": name, "arguments": arguments}

    return fn


def _make_registry() -> ToolRegistry:
    """Return a registry with a ``catalog`` group + flat ``ping``, post-install shape."""
    from frisian_mcp.backends.group_dispatcher import (
        build_group_input_schema,
        make_group_invoke,
    )

    reg = ToolRegistry()
    members = ["item_list", "item_create", "order_list"]
    for m in members:
        reg.register(
            name=m,
            fn=_flat_fn(m),
            description=f"flat {m}",
            input_schema={"type": "object", "properties": {}},
            permission_tier="read_write" if m.endswith("create") else "read",
            is_write=m.endswith("create"),
            perm_app_label="catalog",
            perm_model=m.split(SEP)[0],
        )
    reg.register(
        name="ping",
        fn=_flat_fn("ping"),
        description="flat ping",
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
    for m in members:
        reg.set_hidden(m, True)
    return reg


def _cfg(
    name: str = "default",
    *,
    allow: tuple[str, ...] = ("*",),
    deny: tuple[str, ...] = (),
    highest_tier: str | None = None,
) -> RouteConfig:
    """Return a minimal RouteConfig for the named route."""
    return RouteConfig(
        name=name,
        path="mcp",
        highest_tier=highest_tier,
        allow_list=tuple(allow),
        deny_list=tuple(deny),
    )


def _req() -> Any:
    """Return a minimal POST request stand-in for dispatch tests."""
    from django.test import RequestFactory

    return RequestFactory().post("/mcp")


@pytest.fixture
def registry() -> ToolRegistry:
    """Return a freshly-built neutral registry."""
    return _make_registry()


# ---------------------------------------------------------------------------
# Absence property
# ---------------------------------------------------------------------------


class TestAbsence:
    """A denied tool is absent from the view exactly as if never registered."""

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_wildcard_exposes_group_and_flat(self, registry: ToolRegistry) -> None:
        """``allow_list: ["*"]`` exposes both the group dispatcher and flat tools."""
        view = RouteView.build(registry, _cfg(allow=("*",)))
        assert "catalog" in view.entries
        assert "ping" in view.entries

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_denied_resource_absent_from_group_members(self, registry: ToolRegistry) -> None:
        """A denied resource's members are gone from the rebuilt group entry."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        entry = view.entries["catalog"]
        assert entry.group_tool_names == frozenset({"order_list"})
        assert "item_list" not in entry.group_tool_names
        assert "item_create" not in entry.group_tool_names

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_fully_denied_group_is_dropped_whole(self, registry: ToolRegistry) -> None:
        """Denying every resource drops the dispatcher entirely, not to empty."""
        view = RouteView.build(
            registry, _cfg(allow=("catalog",), deny=("catalog:item", "catalog:order"))
        )
        assert "catalog" not in view.entries
        assert "catalog" not in view.advertised_counts

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_unlisted_flat_tool_absent(self, registry: ToolRegistry) -> None:
        """A flat tool outside the allow list never enters the view."""
        view = RouteView.build(registry, _cfg(allow=("catalog",)))
        assert "ping" not in view.entries

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_hint_key_allow_tracks_visible_names(self, registry: ToolRegistry) -> None:
        """``hint_key_allow`` holds only the visible member/flat names (WI-1)."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        assert "order_list" in view.hint_key_allow
        assert "item_list" not in view.hint_key_allow
        assert "item_create" not in view.hint_key_allow


# ---------------------------------------------------------------------------
# The four prune sites
# ---------------------------------------------------------------------------


class TestPruneSites:
    """Rebuilding a carved group prunes all four registration-frozen surfaces."""

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_site4_group_tool_names_pruned(self, registry: ToolRegistry) -> None:
        """Site 4: ``group_tool_names`` holds only survivors."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        assert view.entries["catalog"].group_tool_names == frozenset({"order_list"})

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_site3_advertised_count_matches_survivors(self, registry: ToolRegistry) -> None:
        """Site 3: the description count matches the surviving members/resources."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        entry = view.entries["catalog"]
        assert "1 tools across 1 resources" in entry.description
        assert view.advertised_counts["catalog"] == 1

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_site1_and_2_denied_resource_not_suggested(self, registry: ToolRegistry) -> None:
        """Sites 1+2: a near-miss for a denied resource is not echoed back."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        with pytest.raises(LookupError) as excinfo:
            view.dispatch(_req(), "catalog", {"resource": "iten", "action": "list"})
        assert "item" not in str(excinfo.value)

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_site1_denied_resource_unroutable(self, registry: ToolRegistry) -> None:
        """Site 1: a denied resource cannot be invoked through the group."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        with pytest.raises(LookupError):
            view.dispatch(_req(), "catalog", {"resource": "item", "action": "list"})

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_surviving_resource_still_routes(self, registry: ToolRegistry) -> None:
        """A surviving resource routes to its flat member through the pruned closure."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        result = view.dispatch(_req(), "catalog", {"resource": "order", "action": "list"})
        assert result["tool"] == "order_list"

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_intact_group_shared_by_reference(self, registry: ToolRegistry) -> None:
        """An un-carved group and flat tools are the registry's own objects."""
        view = RouteView.build(registry, _cfg(allow=("*",)))
        assert view.entries["catalog"] is registry.get_entry("catalog")
        assert view.entries["ping"] is registry.get_entry("ping")

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_carved_group_is_a_new_object(self, registry: ToolRegistry) -> None:
        """A carved group is a route-local object; the global entry is untouched."""
        view = RouteView.build(registry, _cfg(allow=("*",), deny=("catalog:item",)))
        assert view.entries["catalog"] is not registry.get_entry("catalog")
        assert registry.get_entry("catalog").group_tool_names == frozenset(
            {"item_list", "item_create", "order_list"}
        )


# ---------------------------------------------------------------------------
# Dispatch parity / error parity (WI-1)
# ---------------------------------------------------------------------------


class TestDispatch:
    """Invocation honours absence and delegates correctly."""

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_denied_name_error_is_byte_identical(self, registry: ToolRegistry) -> None:
        """A denied name raises bytes identical to the same name never-registered."""
        # Hold the NAME constant; vary only the reason for absence.  Comparing two
        # DIFFERENT names would trivially differ and could pass while leaking — the
        # trap this constant-name form avoids.
        view = RouteView.build(registry, _cfg(allow=("catalog",)))  # ping denied here
        with pytest.raises(ToolNotFoundError) as denied:
            view.dispatch(_req(), "ping", {})

        bare = ToolRegistry()  # 'ping' never registered
        with pytest.raises(ToolNotFoundError) as never:
            bare.dispatch(_req(), "ping", {})

        assert str(denied.value) == str(never.value)
        assert str(denied.value) == "No tool registered with name 'ping'"

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_flat_tool_dispatch_delegates(
        self, registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flat tool delegates to the registry the view was built against.

        Records all three forwarded arguments, not just the name — otherwise a
        regression that dropped or swapped ``request``/``arguments`` would still
        pass while the delegation contract was broken.
        """
        view = RouteView.build(registry, _cfg(allow=("*",)))
        seen: dict[str, Any] = {}

        def fake_dispatch(request: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            seen["request"] = request
            seen["name"] = name
            seen["arguments"] = arguments
            return {"ok": True}

        monkeypatch.setattr(registry, "dispatch", fake_dispatch)
        request = _req()
        result = view.dispatch(request, "ping", {"x": 1})
        assert result == {"ok": True}
        assert seen["name"] == "ping"
        assert seen["request"] is request
        assert seen["arguments"] == {"x": 1}


# ---------------------------------------------------------------------------
# RouteViewRegistry — atomic swap
# ---------------------------------------------------------------------------


class TestRegistry:
    """The registry swaps views atomically and honours the legacy fallback."""

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_rebuild_swaps_atomically(self, registry: ToolRegistry) -> None:
        """A rebuild produces a new view; an in-flight reader keeps the old one."""
        rvr = RouteViewRegistry()
        rvr.rebuild(_cfg(allow=("*",)), registry)
        first = rvr.get("default")
        assert "catalog" in first.entries

        rvr.rebuild(_cfg(allow=("catalog",), deny=("catalog:item", "catalog:order")), registry)
        second = rvr.get("default")
        assert first is not second
        assert "catalog" in first.entries  # old snapshot unchanged
        assert "catalog" not in second.entries  # new snapshot reflects the deny

    @override_settings(FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_get_unmounted_route_is_none(self, registry: ToolRegistry) -> None:
        """Getting a route that was never mounted returns ``None``."""
        rvr = RouteViewRegistry()
        assert rvr.get("elevated") is None

    @override_settings(FRISIAN_MCP_ROUTES=None, FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP)
    def test_rebuild_all_mounts_legacy_when_routes_unset(self, registry: ToolRegistry) -> None:
        """No ``FRISIAN_MCP_ROUTES`` mounts one legacy view exposing everything."""
        rvr = RouteViewRegistry()
        rvr.rebuild_all(registry)
        assert rvr.names() == frozenset({LEGACY_ROUTE_NAME})
        legacy = rvr.get(LEGACY_ROUTE_NAME)
        assert legacy.ceiling is None
        assert "catalog" in legacy.entries

    @override_settings(
        FRISIAN_MCP_TOOL_NAME_SEPARATOR=SEP,
        FRISIAN_MCP_ROUTES={
            "default": {"path": "mcp", "allow_list": ["catalog"], "deny_list": ["catalog:item"]},
            "admin": {"path": "mcp/admin", "highest_tier": "admin", "allow_list": ["*"]},
        },
    )
    def test_rebuild_all_mounts_each_configured_route(self, registry: ToolRegistry) -> None:
        """Each configured route is mounted with its own carve-out and ceiling."""
        rvr = RouteViewRegistry()
        rvr.rebuild_all(registry)
        assert rvr.names() == frozenset({"default", "admin"})
        assert rvr.get("default").entries["catalog"].group_tool_names == frozenset({"order_list"})
        assert rvr.get("admin").ceiling == "admin"


# ---------------------------------------------------------------------------
# Permission resolver + bucket predicate
# ---------------------------------------------------------------------------


class _OpenPerm(AllowAny):
    """An AllowAny subclass that does NOT override the gate — still fully open."""


class _NarrowedPerm(AllowAny):
    """An AllowAny subclass that overrides the gate to deny."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Deny every request, overriding the AllowAny grant."""
        return False


class TestPermissionResolver:
    """``route_effective_permission_classes`` applies the BLOCKER-1 rules."""

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_empty_global_default_route_open(self) -> None:
        """No global classes leaves a ``default`` route open."""
        assert route_effective_permission_classes(_cfg("default")) == []

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_empty_global_admin_route_requires_auth(self) -> None:
        """No global classes gates an ``admin`` route with IsAuthenticated."""
        assert route_effective_permission_classes(_cfg("admin")) == [IsAuthenticated]

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_empty_global_elevated_route_requires_auth(self) -> None:
        """No global classes gates an ``elevated`` route with IsAuthenticated."""
        assert route_effective_permission_classes(_cfg("elevated")) == [IsAuthenticated]

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_legacy_route_stays_open_like_default(self) -> None:
        """The legacy view carves out to [] (open), matching resolve_route_ceiling.

        Regression: treating LEGACY_ROUTE_NAME as an unnamed privileged route
        would return [IsAuthenticated] and gate today's open-by-default endpoint.
        """
        assert route_effective_permission_classes(_cfg(LEGACY_ROUTE_NAME)) == []
        assert route_is_anonymous_reachable(_cfg(LEGACY_ROUTE_NAME)) is True

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=["rest_framework.permissions.IsAdminUser"])
    def test_nonempty_global_wins_verbatim_on_every_route(self) -> None:
        """A non-empty global list wins verbatim on every route name."""
        for name in ("default", "elevated", "admin"):
            assert route_effective_permission_classes(_cfg(name)) == [IsAdminUser]


class TestAnonymousReachable:
    """The two anonymous-reachability predicates split POST from GET correctly."""

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_default_route_post_reachable(self) -> None:
        """An open ``default`` route is anonymously POST-reachable."""
        assert route_is_anonymous_reachable(_cfg("default")) is True

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_admin_route_post_not_reachable(self) -> None:
        """An ``admin`` route (IsAuthenticated) is not anonymously POST-reachable."""
        assert route_is_anonymous_reachable(_cfg("admin")) is False

    @override_settings(
        FRISIAN_MCP_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticatedOrReadOnly"]
    )
    def test_partial_anonymous_post_not_reachable(self) -> None:
        """IsAuthenticatedOrReadOnly denies anonymous POST — the tool surface is gated."""
        assert route_is_anonymous_reachable(_cfg("admin")) is False

    @override_settings(
        FRISIAN_MCP_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticatedOrReadOnly"]
    )
    def test_partial_anonymous_sse_reachable(self) -> None:
        """IsAuthenticatedOrReadOnly permits anonymous GET — the SSE channel is open."""
        assert route_is_anonymous_sse_reachable(_cfg("admin")) is True

    @override_settings(FRISIAN_MCP_PERMISSION_CLASSES=None)
    def test_empty_global_sse_reachable(self) -> None:
        """No global classes leaves the SSE channel anonymously reachable."""
        assert route_is_anonymous_sse_reachable(_cfg("default")) is True

    @override_settings(
        FRISIAN_MCP_PERMISSION_CLASSES=["rest_framework.permissions.IsAuthenticated"]
    )
    def test_authenticated_sse_not_reachable(self) -> None:
        """IsAuthenticated denies anonymous GET too."""
        assert route_is_anonymous_sse_reachable(_cfg("admin")) is False


class TestBucketPredicate:
    """``_bucket`` classifies permission classes for the startup audit."""

    def test_allow_any_is_anonymous_granting(self) -> None:
        """Bare ``AllowAny`` is anonymous-granting."""
        assert _bucket(AllowAny) == BUCKET_ANONYMOUS_GRANTING

    def test_unmodified_subclass_still_anonymous_granting(self) -> None:
        """An unmodified ``AllowAny`` subclass is still anonymous-granting."""
        assert _is_anonymous_granting(_OpenPerm) is True
        assert _bucket(_OpenPerm) == BUCKET_ANONYMOUS_GRANTING

    def test_overriding_subclass_falls_through_to_opaque(self) -> None:
        """A subclass that overrides the gate falls through to opaque."""
        assert _is_anonymous_granting(_NarrowedPerm) is False
        assert _bucket(_NarrowedPerm) == BUCKET_OPAQUE

    def test_is_authenticated_is_auth_requiring(self) -> None:
        """IsAuthenticated and IsAdminUser are auth-requiring."""
        assert _bucket(IsAuthenticated) == BUCKET_AUTH_REQUIRING
        assert _bucket(IsAdminUser) == BUCKET_AUTH_REQUIRING

    def test_read_only_is_partial_anonymous(self) -> None:
        """IsAuthenticatedOrReadOnly is partial-anonymous."""
        assert _bucket(IsAuthenticatedOrReadOnly) == BUCKET_PARTIAL_ANONYMOUS
