"""
Tests for PR-7 — the ``McpView.post()`` route-view + effective-tier wiring.

Covers the request-path half of the per-route permission model:

* per-route URL mounting from the canonical path mapping, and the rule that a
  tier absent from ``FRISIAN_MCP_ROUTES`` is not mounted at all (ADR-010 §4);
* the effective tier ``min(token_tier, route_ceiling, FRISIAN_MCP_MAX_TIER)``
  computed once in ``post()``, stamped on ``request._mcp_effective_tier``, and
  read (never recomputed) by every downstream consumer (ADR-010 §8);
* discovery reads the **capped** tier (WI-2) — a write-capable token on a
  ``read``-ceiling route is never shown a write tool it could not invoke;
* an omitted ``highest_tier`` on a configured route resolves to the tier key's
  secure default, never to uncapped; the legacy view stays uncapped;
* the ban-6 seam — an auth failure yields ``401`` with the ``WWW-Authenticate``
  challenge intact, while a denied tool yields the JSON-RPC absence error; the
  two must never collapse into one shape;
* the not-found suggester enumerates the route's deny-carved view, so a denied
  tool is never named back in a "did you mean" hint (WI-1);
* per-route ``tools/list`` cache keys — same tier, different route, different
  manifest.

PR-12 (`testing` lane) extends this file rather than duplicating it (PM ruled
one home per invariant): ``TestSynthesizedActionCap`` (a write action
synthesized on the dispatcher, ``bulk_create``, is hidden at a ``read`` ceiling
and absent on invoke) and ``TestAdminTokenOnReadCeiling`` (an admin token on a
``read``-ceiling route leaks no write or admin surface — the route-ceiling path,
distinct from the ``FRISIAN_MCP_MAX_TIER`` path above).  The retired skeleton
``test_route_tier_cap.py`` was deleted when those landed.

All fixtures use neutral names (group ``catalog``, resources ``item`` /
``order``, flat tool ``ping``) per the package-neutrality ruling.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from frisian_mcp.registry import ToolRegistry, _resolve_request_tier
from frisian_mcp.route_config import RouteConfig
from frisian_mcp.route_views import (
    LEGACY_ROUTE_NAME,
    _min_tier,
    resolve_route_ceiling,
    route_views,
)

SEP = "_"

GATEWAY = "gateway"
GATEWAY_ELEVATED = "gateway/elevated"
GATEWAY_ADMIN = "gateway/admin"


# ---------------------------------------------------------------------------
# Fixtures — a registry mirroring the post-_install_dispatch_groups shape
# ---------------------------------------------------------------------------


def _flat_fn(name: str) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    def fn(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
        # Report the tier every downstream reader resolves — proves the
        # request-scoped stamp is what dispatch-time consumers actually see.
        return {"tool": name, "resolved_tier": _resolve_request_tier(request)}

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
        is_write = m.endswith("create")
        reg.register(
            name=m,
            fn=_flat_fn(m),
            description=f"flat {m}",
            # The write tool carries a required field so ToolInputError paths
            # (and the lite escape hatch on them) are exercisable end-to-end.
            input_schema=(
                {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
                if is_write
                else {"type": "object", "properties": {}}
            ),
            permission_tier="read_write" if is_write else "read",
            is_write=is_write,
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
    # Production (apps.py:825) marks group members hidden after installing the
    # dispatcher: they collapse INTO `catalog` and never appear as flat tools in
    # tools/list.  Mirror that here so route tests validate the surface the host
    # actually exposes, not one where members leak alongside their dispatcher.
    # Hidden entries stay dispatchable by name (registry.py) and inside the
    # RouteView, so the tier cap is asserted via the dispatcher's action tree
    # (action='help'), which is how it manifests in production.
    for m in members:
        reg.set_hidden(m, True)
    return reg


def _cfg(
    name: str,
    path: str,
    *,
    allow: tuple[str, ...] = ("*",),
    deny: tuple[str, ...] = (),
    highest_tier: str | None = None,
) -> RouteConfig:
    """Return a RouteConfig for a per-route mount under test."""
    return RouteConfig(
        name=name,
        path=path,
        highest_tier=highest_tier,
        allow_list=tuple(allow),
        deny_list=tuple(deny),
    )


class _StubUser:
    """Minimal authenticated principal for force_authenticate."""

    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    pk = 1


@pytest.fixture()
def registry() -> ToolRegistry:
    """Return a freshly-built neutral registry."""
    return _make_registry()


@pytest.fixture()
def clean_route_views() -> Generator[Any, None, None]:
    """Snapshot and restore the process-scoped RouteViewRegistry singleton."""
    with route_views._lock:  # noqa: SLF001
        saved = dict(route_views._views)  # noqa: SLF001
    yield route_views
    with route_views._lock:  # noqa: SLF001
        route_views._views = saved  # noqa: SLF001


def _mount(cfg: RouteConfig, registry: ToolRegistry) -> Any:
    """Materialise *cfg* in the singleton and return its per-route view callable."""
    from frisian_mcp.apps import _make_route_mcp_view

    route_views.rebuild(cfg, registry)
    return _make_route_mcp_view(cfg).as_view()


def _post_jsonrpc(
    view: Any,
    path: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    user: Any = None,
) -> Any:
    """POST a JSON-RPC call to *view* and return the HTTP response."""
    factory = APIRequestFactory()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    request = factory.post(f"/{path}", payload, format="json")
    if user is not None:
        force_authenticate(request, user=user)
    return view(request)


def _rpc_result(response: Any) -> dict[str, Any]:
    body = json.loads(response.content)
    assert "error" not in body, body
    return body["result"]  # type: ignore[no-any-return]


def _rpc_error(response: Any) -> dict[str, Any]:
    body = json.loads(response.content)
    assert "error" in body, body
    return body["error"]  # type: ignore[no-any-return]


def _tool_names(response: Any) -> set[str]:
    return {t["name"] for t in _rpc_result(response)["tools"]}


def _call_result(response: Any) -> dict[str, Any]:
    result = _rpc_result(response)
    assert result.get("isError") is False, result
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


def _tier_hook(tier: str) -> Callable[[Any], str]:
    def hook(request: Any) -> str:
        return tier

    return hook


# ---------------------------------------------------------------------------
# _min_tier / resolve_route_ceiling — the PR-7 resolution helpers
# ---------------------------------------------------------------------------


class TestMinTier:
    """`min` is monotone — it narrows, never widens; None means no cap."""

    def test_min_of_mixed_tiers_is_most_restrictive(self) -> None:
        """The lowest-ranked tier wins."""
        assert _min_tier("admin", "read_write", "read") == "read"
        assert _min_tier("admin", "read_write") == "read_write"

    def test_none_values_do_not_cap(self) -> None:
        """None participants are skipped, not treated as read."""
        assert _min_tier(None, "admin", None) == "admin"

    def test_all_none_means_no_cap(self) -> None:
        """min() of no caps is no cap."""
        assert _min_tier(None, None) is None

    def test_unknown_tier_string_fails_closed(self) -> None:
        """An unrecognised tier ranks lowest and therefore caps."""
        assert _min_tier("admin", "not-a-tier") == "not-a-tier"


class TestResolveRouteCeiling:
    """Omitted highest_tier on a configured route NEVER means uncapped."""

    def test_omitted_resolves_to_secure_default_per_route(self) -> None:
        """default->read, elevated->read_write, admin->admin."""
        assert resolve_route_ceiling(_cfg("default", GATEWAY)) == "read"
        assert resolve_route_ceiling(_cfg("elevated", GATEWAY_ELEVATED)) == "read_write"
        assert resolve_route_ceiling(_cfg("admin", GATEWAY_ADMIN)) == "admin"

    def test_explicit_ceiling_wins(self) -> None:
        """A declared highest_tier is used verbatim."""
        cfg = _cfg("default", GATEWAY, highest_tier="read_write")
        assert resolve_route_ceiling(cfg) == "read_write"

    def test_legacy_view_stays_uncapped(self) -> None:
        """Only the implicit legacy view may resolve to no ceiling."""
        legacy = RouteConfig(
            name=LEGACY_ROUTE_NAME,
            path="mcp",
            highest_tier=None,
            allow_list=("*",),
            deny_list=(),
        )
        assert resolve_route_ceiling(legacy) is None


# ---------------------------------------------------------------------------
# WI-2 — discovery reads the capped tier
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestDiscoveryReadsCappedTier:
    """A write-capable token on a read-ceiling route never SEES write tools."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_write_token_on_default_route_sees_no_write_tools(self, registry: ToolRegistry) -> None:
        """Secure-default read ceiling hides write actions from discovery.

        Members are hidden in production, so the top-level surface is just the
        genuine flat tool `ping` and the `catalog` dispatcher; the read cap
        manifests in `catalog`'s action tree, which must expose `list` but not
        `create` on the `item` resource.
        """
        # `default` omits highest_tier -> secure default ceiling `read`.
        view = _mount(_cfg("default", GATEWAY), registry)
        names = _tool_names(_post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser()))
        assert names == {"ping", "catalog"}  # hidden members do not leak as flat tools
        item_actions = _help_actions(view, GATEWAY, "item")
        assert "list" in item_actions
        assert "create" not in item_actions

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_same_token_on_read_write_ceiling_sees_writes(self, registry: ToolRegistry) -> None:
        """A read_write ceiling admits the same token's write actions."""
        view = _mount(_cfg("elevated", GATEWAY_ELEVATED, highest_tier="read_write"), registry)
        assert "create" in _help_actions(view, GATEWAY_ELEVATED, "item")

    @override_settings(
        FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"),
        FRISIAN_MCP_MAX_TIER="read",
    )
    def test_global_max_tier_caps_below_route_ceiling(self, registry: ToolRegistry) -> None:
        """FRISIAN_MCP_MAX_TIER participates in the min()."""
        # admin token, admin-ceiling route, but MAX_TIER=read -> min() wins.
        view = _mount(_cfg("admin", GATEWAY_ADMIN, highest_tier="admin"), registry)
        assert "create" not in _help_actions(view, GATEWAY_ADMIN, "item")

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_min_never_widens_a_read_token(self, registry: ToolRegistry) -> None:
        """The ceiling is a cap, never a grant."""
        # read token on a read_write-ceiling route stays read: the ceiling is
        # a cap, never a grant.
        view = _mount(_cfg("elevated", GATEWAY_ELEVATED, highest_tier="read_write"), registry)
        assert "create" not in _help_actions(view, GATEWAY_ELEVATED, "item")


# ---------------------------------------------------------------------------
# PR-12 gaps — synthesized actions under the cap, and an admin token on a
# read-ceiling route.  The class above proves write tools are hidden; these add
# the two acceptance cases it does not: an action synthesized ON the dispatcher
# (`bulk_create`), and the admin-token path (distinct from the MAX_TIER path).
# ---------------------------------------------------------------------------


def _make_tiered_registry() -> ToolRegistry:
    """Return a ``catalog`` group whose ``item`` resource spans all three tiers.

    Members are resource-leading, so the dispatcher exposes ``item`` with actions
    ``list`` (read), ``bulk_create`` (read_write — a synthesized-style write
    action), and ``purge`` (admin).  Lets a single surface prove the cap hides
    the write and admin actions at ``read``.
    """
    from frisian_mcp.backends.group_dispatcher import (
        build_group_input_schema,
        make_group_invoke,
    )

    reg = ToolRegistry()
    tiers = {
        "item_list": "read",
        "item_bulk_create": "read_write",
        "item_purge": "admin",
    }
    for name, tier in tiers.items():
        reg.register(
            name=name,
            fn=_flat_fn(name),
            description=f"flat {name}",
            input_schema={"type": "object", "properties": {}},
            permission_tier=tier,
            is_write=tier != "read",
        )
    members = list(tiers)
    prefix_set = frozenset({"item"})
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
    # Members hidden to match the production post-group shape (see _make_registry).
    for m in members:
        reg.set_hidden(m, True)
    return reg


def _help_actions(view: Any, path: str, resource: str) -> list[str]:
    """Return the actions ``action='help'`` lists for *resource*."""
    response = _post_jsonrpc(
        view,
        path,
        "tools/call",
        {"name": "catalog", "arguments": {"action": "help"}},
        user=_StubUser(),
    )
    payload = json.loads(_rpc_result(response)["content"][0]["text"])
    return list(payload["resources"].get(resource, []))


@pytest.fixture()
def tiered_registry() -> ToolRegistry:
    """A registry whose ``item`` resource spans read / read_write / admin."""
    return _make_tiered_registry()


@pytest.mark.usefixtures("clean_route_views")
class TestSynthesizedActionCap:
    """A write action synthesized on the dispatcher obeys the effective-tier cap."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_bulk_create_hidden_on_read_ceiling_route(self, tiered_registry: ToolRegistry) -> None:
        """`read_write` token, secure-default `read` ceiling: no `bulk_create` in help."""
        view = _mount(_cfg("default", GATEWAY), tiered_registry)
        actions = _help_actions(view, GATEWAY, "item")
        assert "bulk_create" not in actions
        assert "list" in actions

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_bulk_create_visible_on_read_write_ceiling_route(
        self, tiered_registry: ToolRegistry
    ) -> None:
        """The same token on a `read_write` ceiling sees the synthesized write action."""
        view = _mount(
            _cfg("elevated", GATEWAY_ELEVATED, highest_tier="read_write"), tiered_registry
        )
        actions = _help_actions(view, GATEWAY_ELEVATED, "item")
        assert "bulk_create" in actions

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_capped_synthesized_action_is_absent_on_invoke(
        self, tiered_registry: ToolRegistry
    ) -> None:
        """Invocation matches discovery: a hidden `bulk_create` also fails to invoke.

        No action visible on discovery that fails at invoke, and none invocable
        that discovery hid — the two directions must agree.  The correct twin
        for an above-ceiling action probed THROUGH THE GROUP is a never-existed
        action probed the same way (V11-20/F3): both must return the group's
        own unknown-tool absence, byte-for-byte on the shared template.  (The
        previous expectation here — the inner dispatch's "No tool registered"
        JSON-RPC error — was itself distinguishable from the never-existed
        shape, i.e. the F3 oracle.)
        """
        view = _mount(_cfg("default", GATEWAY), tiered_registry)

        def _probe(action: str) -> str:
            response = _post_jsonrpc(
                view,
                GATEWAY,
                "tools/call",
                {"name": "catalog", "arguments": {"resource": "item", "action": action}},
                user=_StubUser(),
            )
            result = _rpc_result(response)
            assert result.get("isError") is True, result
            return str(json.loads(result["content"][0]["text"])["error"])

        above = _probe("bulk_create")
        missing = _probe("zzznope")
        assert "Unknown tool 'item_bulk_create' in group 'catalog'" in above
        assert above.replace("item_bulk_create", "TOOL") == missing.replace("item_zzznope", "TOOL")


@pytest.mark.usefixtures("clean_route_views")
class TestAdminTokenOnReadCeiling:
    """An admin token on a `read`-ceiling route leaks no write or admin surface."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"))
    def test_admin_token_capped_to_read_sees_only_read_actions(
        self, tiered_registry: ToolRegistry
    ) -> None:
        """`read` ceiling caps an admin token: only read actions survive discovery."""
        view = _mount(_cfg("default", GATEWAY), tiered_registry)
        actions = _help_actions(view, GATEWAY, "item")
        assert actions == ["list"]

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"))
    def test_tools_list_exposes_only_the_dispatcher_under_read_ceiling(
        self, tiered_registry: ToolRegistry
    ) -> None:
        """`tools/list` shows only the `catalog` dispatcher; no member leaks, no admin/write.

        Members are hidden in production, so the top-level surface is just the
        dispatcher.  The admin/write actions (`purge`, `bulk_create`) are absent
        from `catalog`'s read-capped action tree — the cap does the work, not
        member visibility.
        """
        view = _mount(_cfg("default", GATEWAY), tiered_registry)
        names = _tool_names(_post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser()))
        assert names == {"catalog"}
        actions = _help_actions(view, GATEWAY, "item")
        assert "purge" not in actions
        assert "bulk_create" not in actions
        assert "list" in actions

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"))
    def test_admin_token_dispatch_time_tier_is_capped_to_read(
        self, tiered_registry: ToolRegistry
    ) -> None:
        """The dispatch-time reader sees `read`, not the raw admin token tier."""
        view = _mount(_cfg("default", GATEWAY), tiered_registry)
        response = _post_jsonrpc(
            view,
            GATEWAY,
            "tools/call",
            {"name": "item_list", "arguments": {}},
            user=_StubUser(),
        )
        assert _call_result(response)["resolved_tier"] == "read"


# ---------------------------------------------------------------------------
# ADR-010 §8 — computed once, stamped, read everywhere
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestEffectiveTierStampedOnce:
    """Every downstream reader sees the one post()-computed value."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_dispatch_time_reader_sees_the_capped_tier(self, registry: ToolRegistry) -> None:
        """A tool reading the tier at dispatch time sees the capped value."""
        # The tool itself calls _resolve_request_tier; on a read-ceiling route
        # it must see `read`, not the raw read_write token tier.
        view = _mount(_cfg("default", GATEWAY), registry)
        response = _post_jsonrpc(
            view, GATEWAY, "tools/call", {"name": "ping", "arguments": {}}, user=_StubUser()
        )
        assert _call_result(response)["resolved_tier"] == "read"

    def test_tier_hook_is_consulted_exactly_once_per_request(self, registry: ToolRegistry) -> None:
        """The stamp short-circuits every later tier resolution."""
        calls: list[Any] = []

        def counting_hook(request: Any) -> str:
            calls.append(request)
            return "read_write"

        view = _mount(_cfg("elevated", GATEWAY_ELEVATED, highest_tier="read_write"), registry)
        with override_settings(FRISIAN_MCP_RESOLVE_TIER=counting_hook):
            response = _post_jsonrpc(
                view,
                GATEWAY_ELEVATED,
                "tools/call",
                {"name": "ping", "arguments": {}},
                user=_StubUser(),
            )
        # post() resolves once; the tool's own _resolve_request_tier read and
        # the dispatch-time enforcement read both hit the stamp instead of the
        # hook.  More than one call means something recomputed the tier.
        assert _call_result(response)["resolved_tier"] == "read_write"
        assert len(calls) == 1

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"))
    def test_legacy_plain_view_stays_uncapped(self) -> None:
        """Plain McpView keeps today's uncapped, route-less behaviour."""
        # ROUTES unset + plain McpView: no route view, no ceiling — the admin
        # token keeps its full tier.  Byte-identical legacy behaviour.
        from frisian_mcp.views import McpView

        captured: dict[str, Any] = {}

        def probe(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
            captured["route_view"] = getattr(request, "_mcp_route_view", "MISSING")
            captured["tier"] = _resolve_request_tier(request)
            return {"ok": True}

        from frisian_mcp.registry import tool_registry

        tool_registry.register(
            name="wiring_probe",
            fn=probe,
            description="probe",
            input_schema={"type": "object", "properties": {}},
            permission_tier="read",
        )
        try:
            response = _post_jsonrpc(
                McpView.as_view(),
                "mcp",
                "tools/call",
                {"name": "wiring_probe", "arguments": {}},
                user=_StubUser(),
            )
            assert _call_result(response)["ok"] is True
            assert captured["route_view"] is None
            assert captured["tier"] == "admin"
        finally:
            tool_registry._tools.pop("wiring_probe", None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Invocation parity — the cap converts tier denial into absence
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestInvocationParity:
    """No undiscovered tool is invocable; the denial is absence, not a tier error."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_capped_out_tool_is_absent_on_invoke(self, registry: ToolRegistry) -> None:
        """A tier-capped tool invokes as absence, not as a tier error."""
        view = _mount(_cfg("default", GATEWAY), registry)
        response = _post_jsonrpc(
            view,
            GATEWAY,
            "tools/call",
            {"name": "item_create", "arguments": {}},
            user=_StubUser(),
        )
        error = _rpc_error(response)
        assert "No tool registered with name 'item_create'" in error["data"]
        # Absence, not a tier error: the message must not leak that the tool
        # exists above the caller's effective tier.
        assert "permission" not in error["data"].lower()

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_route_denied_tool_is_absent_on_invoke(self, registry: ToolRegistry) -> None:
        """A deny-listed tool raises the never-registered error."""
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("ping",)), registry)
        response = _post_jsonrpc(
            view, GATEWAY, "tools/call", {"name": "ping", "arguments": {}}, user=_StubUser()
        )
        assert "No tool registered with name 'ping'" in _rpc_error(response)["data"]

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"))
    def test_suggester_never_names_a_denied_tool(self, registry: ToolRegistry) -> None:
        """Close-match suggestions come from the deny-carved view only."""
        # `item` is denied on the route; a near-miss must not be corrected to
        # any denied member name (WI-1 — the absence error must not undo
        # itself in its own suggestion line).
        view = _mount(_cfg("default", GATEWAY, allow=("*",), deny=("catalog:item",)), registry)
        response = _post_jsonrpc(
            view,
            GATEWAY,
            "tools/call",
            {"name": "item_lst", "arguments": {}},
            user=_StubUser(),
        )
        data = _rpc_error(response)["data"]
        assert "item_list" not in data
        assert "item_create" not in data


# ---------------------------------------------------------------------------
# Ban 6 — auth failure and absence are different shapes and never collapse
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestBan6Seam:
    """401 + WWW-Authenticate for auth failure; JSON-RPC absence for a denied tool."""

    @override_settings(
        FRISIAN_MCP_AUTHENTICATION_CLASSES=["rest_framework.authentication.BasicAuthentication"]
    )
    def test_anonymous_post_to_admin_route_gets_401_with_challenge(
        self, registry: ToolRegistry
    ) -> None:
        """Rule 3 gates the admin route; the challenge header survives."""
        # Empty global FRISIAN_MCP_PERMISSION_CLASSES + admin route -> rule 3
        # supplies [IsAuthenticated] (BLOCKER-1); the challenge header stays.
        view = _mount(_cfg("admin", GATEWAY_ADMIN, highest_tier="admin"), registry)
        response = _post_jsonrpc(view, GATEWAY_ADMIN, "tools/list")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate")

    def test_default_route_stays_open_without_global_classes(self, registry: ToolRegistry) -> None:
        """Rule 2: default stays open when no global classes are set."""
        view = _mount(_cfg("default", GATEWAY), registry)
        response = _post_jsonrpc(view, GATEWAY, "tools/list")
        assert response.status_code == 200

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("admin"))
    def test_denied_tool_for_authenticated_caller_is_absence_not_401(
        self, registry: ToolRegistry
    ) -> None:
        """Absence rides HTTP 200 JSON-RPC; auth failure rides 401."""
        view = _mount(_cfg("admin", GATEWAY_ADMIN, highest_tier="admin", deny=("ping",)), registry)
        response = _post_jsonrpc(
            view,
            GATEWAY_ADMIN,
            "tools/call",
            {"name": "ping", "arguments": {}},
            user=_StubUser(),
        )
        # The two failure shapes must not collapse: this is a JSON-RPC-level
        # absence error on an HTTP 200, not an HTTP auth status.
        assert response.status_code == 200
        assert "No tool registered with name 'ping'" in _rpc_error(response)["data"]


# ---------------------------------------------------------------------------
# URL mounting — routes ARE the surface when FRISIAN_MCP_ROUTES is set
# ---------------------------------------------------------------------------


class TestRouteUrlMounting:
    """_install_route_urls mounts the canonical paths, exactly, idempotently."""

    @pytest.fixture()
    def _cleanup_urls(self) -> Generator[None, None, None]:
        yield
        from django.urls import clear_url_caches, get_resolver

        from frisian_mcp.apps import _MCP_ROUTE_URL_ATTR

        # _install_route_urls mutates the urlconf module's urlpatterns list in
        # place; name the test urlconf explicitly because the ROOT_URLCONF
        # override has already been unwound by the time teardown runs.
        resolver = get_resolver("tests.urls")
        resolver.url_patterns[:] = [
            p for p in resolver.url_patterns if not getattr(p, _MCP_ROUTE_URL_ATTR, False)
        ]
        clear_url_caches()

    @pytest.mark.usefixtures("_cleanup_urls", "clean_route_views")
    @override_settings(
        ROOT_URLCONF="tests.urls",
        FRISIAN_MCP_ROUTES={
            "default": {"path": "gateway", "allow_list": ["*"]},
            "admin": {
                "path": "gateway/admin",
                "highest_tier": "admin",
                "allow_list": ["*"],
            },
        },
    )
    def test_configured_routes_mount_and_absent_tiers_do_not(
        self, client: Any, registry: ToolRegistry
    ) -> None:
        """Configured paths answer; unconfigured tiers 404 (ADR-010 S4)."""
        from frisian_mcp.apps import _install_route_urls

        assert _install_route_urls() == 2
        # Second call is a no-op (sentinel idempotency).
        assert _install_route_urls() == 0

        route_views.rebuild(_cfg("default", GATEWAY), registry)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        response = client.post("/gateway/", data=payload, content_type="application/json")
        assert response.status_code == 200

        # `elevated` is absent from the setting: not mounted, no handler —
        # the framework 404, not an MCP response (ADR-010 §4).
        response = client.post("/gateway/elevated/", data=payload, content_type="application/json")
        assert response.status_code == 404

    @override_settings(FRISIAN_MCP_ROUTES=None)
    def test_no_routes_setting_mounts_nothing(self) -> None:
        """ROUTES unset -> the installer is a no-op."""
        from frisian_mcp.apps import _install_route_urls

        assert _install_route_urls() == 0


@pytest.mark.usefixtures("clean_route_views")
class TestResolveRouteViewFailsClosed:
    """A per-route mount that cannot resolve its view fails loud, never open."""

    def test_missing_view_and_config_raises_rather_than_serving_global(self) -> None:
        """route_name set + no view + no config -> raise, not None (fail-open)."""
        from django.core.exceptions import ImproperlyConfigured

        from frisian_mcp.views import McpView

        class _Broken(McpView):
            _route_name = "unmounted_route_xyz"
            _route_config = None

        with pytest.raises(ImproperlyConfigured):
            _Broken()._resolve_route_view()

    def test_plain_mount_still_returns_none(self) -> None:
        """A genuine plain mount (no route_name) resolves to None as before."""
        from frisian_mcp.views import McpView

        assert McpView()._resolve_route_view() is None


# ---------------------------------------------------------------------------
# Per-route tools/list cache keys
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_route_views")
class TestPerRouteCacheKeys:
    """Same tier, different route -> different cached manifest."""

    @override_settings(
        FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"),
        FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=300,
    )
    def test_routes_do_not_share_cache_entries(self, registry: ToolRegistry) -> None:
        """Per-route cache keys keep carved manifests separate."""
        from frisian_mcp.views import invalidate_tools_list_cache

        default_view = _mount(_cfg("default", GATEWAY, deny=("ping",)), registry)
        admin_view = _mount(_cfg("admin", GATEWAY_ADMIN, highest_tier="admin"), registry)
        try:
            default_names = _tool_names(
                _post_jsonrpc(default_view, GATEWAY, "tools/list", user=_StubUser())
            )
            admin_names = _tool_names(
                _post_jsonrpc(admin_view, GATEWAY_ADMIN, "tools/list", user=_StubUser())
            )
            assert "ping" not in default_names
            assert "ping" in admin_names
        finally:
            invalidate_tools_list_cache()

    @override_settings(
        FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read"),
        FRISIAN_MCP_TOOLS_LIST_CACHE_TTL=300,
    )
    def test_invalidate_clears_per_route_keys(self, registry: ToolRegistry) -> None:
        """invalidate_tools_list_cache covers the per-route keys."""
        from django.core.cache import cache as django_cache

        from frisian_mcp.views import _TOOLS_LIST_CACHE_KEY, invalidate_tools_list_cache

        view = _mount(_cfg("default", GATEWAY), registry)
        _post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser())
        key = f"{_TOOLS_LIST_CACHE_KEY}:default:read"
        assert django_cache.get(key) is not None
        invalidate_tools_list_cache()
        assert django_cache.get(key) is None
