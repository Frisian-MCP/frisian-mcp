"""Advertised group counts under COMBINED filtering (#71 follow-on).

`#73` fixed the group-dispatcher count and pinned each filter on its own:
`test_tier_filtered_group_description_matches_help` covers the tier ceiling,
`test_filtered_group_description_matches_help` covers capability filtering.

These add the cases that fall between and around those two:

* both filters applied **at once** -- each is individually correct while their
  composition is not exercised;
* **monotonicity** -- a narrowing filter may only ever lower the advertised
  count, never raise it;
* the **elevated-route counterpart** -- the existing ceiling test only shows
  the count going *down*, which a hardcoded lower number would also satisfy.
  Showing the same registry advertise its full surface to a caller who can
  reach it is what proves the count tracks the caller.

That last one needs an elevated route *and* an elevated token: the `default`
route resolves a `read` ceiling from its own name, so a `read_write` caller
there is still capped. Route ceiling and principal tier are independent, which
is the distinction #71's first triage collapsed.
"""

from __future__ import annotations

# pylint: disable=abstract-method,unused-argument,import-outside-toplevel
# Test ViewSets/serializers deliberately omit create()/update(); the group_dispatcher
# import is deferred to match the existing fixtures it mirrors. Same module-level
# convention already used by tests/test_discovery.py.
import re
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
    _make_registry,
    _mount,
    _post_jsonrpc,
    _rpc_result,
    _StubUser,
    _tier_hook,
)


@pytest.fixture(name="registry")
def _registry_fixture() -> ToolRegistry:
    """The shared catalog fixture used by the route-level tests."""
    return _make_registry()


@pytest.fixture()
def clean_route_views() -> Generator[Any, None, None]:
    """Snapshot and restore the process-scoped RouteViewRegistry singleton."""
    with route_views._lock:  # noqa: SLF001  # pylint: disable=protected-access
        saved = dict(route_views._views)  # noqa: SLF001  # pylint: disable=protected-access
    yield route_views
    with route_views._lock:  # noqa: SLF001  # pylint: disable=protected-access
        route_views._views = saved  # noqa: SLF001  # pylint: disable=protected-access


MEMBERS: dict[str, tuple[str, str]] = {
    "item_list": ("read", "item"),
    "item_create": ("read_write", "item"),
    "order_list": ("read", "order"),
    "order_create": ("read_write", "order"),
}


def _fn(name: str) -> Any:
    def inner(arguments: dict[str, Any], request: Any) -> dict[str, Any]:
        return {"tool": name}

    return inner


def _registry() -> ToolRegistry:
    """Four tools over two resources, spanning two tiers."""
    from frisian_mcp.backends.group_dispatcher import build_group_input_schema, make_group_invoke

    reg = ToolRegistry()
    for name, (tier, model) in MEMBERS.items():
        reg.register(
            name=name,
            fn=_fn(name),
            description=f"flat {name}",
            input_schema={"type": "object", "properties": {}},
            permission_tier=tier,
            perm_app_label="cat",
            perm_model=model,
        )
    names = frozenset(MEMBERS)
    reg.register(
        name="catalog",
        fn=make_group_invoke("catalog", names, reg, frozenset()),
        description=(
            "Group dispatcher for 4 tools across 2 resources. Use action='help' to discover."
        ),
        input_schema=build_group_input_schema(),
        permission_classes=[],
        permission_tier="read",
        is_dispatcher=True,
        group_tool_names=names,
    )
    for name in MEMBERS:
        reg.set_hidden(name, True)
    return reg


def _counts(tools: list[dict[str, Any]]) -> tuple[int, int]:
    description = next(t for t in tools if t["name"] == "catalog")["description"]
    match = re.search(r"for (\d+) tools across (\d+) resources", description)
    assert match is not None, f"unparseable dispatcher description: {description!r}"
    return int(match.group(1)), int(match.group(2))


def _only(model: str) -> Any:
    return lambda entry: getattr(entry, "perm_model", None) in (None, model)


class TestFiltersCompose:
    """The ceiling and the principal's capabilities must apply together."""

    def test_unfiltered_advertises_the_whole_group(self) -> None:
        """Baseline: with neither filter, all four tools are advertised."""
        assert _counts(_registry().list_tools()) == (4, 2)

    def test_tier_and_permissions_compose(self) -> None:
        """Both at once: ``order`` capability at a ``read`` ceiling leaves one tool.

        Each filter alone is already pinned upstream. This is the case where a
        fix could satisfy both individual tests and still get the intersection
        wrong.
        """
        tools = _registry().list_tools(max_tier="read", entry_filter=_only("order"))
        assert _counts(tools) == (1, 1)

    def test_permissions_alone_narrow_the_resource_count(self) -> None:
        """Capability filtering with the ceiling wide open still narrows resources."""
        tools = _registry().list_tools(max_tier="read_write", entry_filter=_only("order"))
        assert _counts(tools) == (2, 1)

    @pytest.mark.parametrize("model", ["item", "order"])
    def test_a_narrowing_filter_can_only_lower_the_count(self, model: str) -> None:
        """Monotonicity: filtering must never advertise more than the full set."""
        full_tools, full_resources = _counts(_registry().list_tools())
        tools, resources = _counts(
            _registry().list_tools(max_tier="read", entry_filter=_only(model))
        )
        assert tools <= full_tools
        assert resources <= full_resources


@pytest.mark.usefixtures("clean_route_views")
class TestRouteCeilingIsIndependentOfPrincipalTier:
    """The count must track the caller, not merely be lower than it was."""

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_elevated_route_advertises_the_full_group(self, registry: ToolRegistry) -> None:
        """A door whose ceiling admits writes advertises the whole group.

        Counterpart to the existing ceiling test, which only shows the count
        going down -- a hardcoded lower number would satisfy that alone.
        """
        view = _mount(_cfg("elevated", GATEWAY_ELEVATED), registry)
        response = _post_jsonrpc(view, GATEWAY_ELEVATED, "tools/list", user=_StubUser())
        catalog = next(t for t in _rpc_result(response)["tools"] if t["name"] == "catalog")
        assert "3 tools across 2 resources" in catalog["description"]

    @override_settings(FRISIAN_MCP_RESOLVE_TIER=_tier_hook("read_write"))
    def test_an_elevated_token_is_still_capped_by_the_route(self, registry: ToolRegistry) -> None:
        """Same token, lower-ceilinged door: the route ceiling still binds.

        This is the pair that shows the two are independent rather than one
        standing in for the other.
        """
        view = _mount(_cfg("default", GATEWAY), registry)
        response = _post_jsonrpc(view, GATEWAY, "tools/list", user=_StubUser())
        catalog = next(t for t in _rpc_result(response)["tools"] if t["name"] == "catalog")
        assert "2 tools across 2 resources" in catalog["description"]
