"""Golden tests for WI-1 — per-route absence invariants (PR-11).

A tool denied on a route must be **indistinguishable from a tool that was never
discovered**.  Absence is asserted across four surfaces, not one:

1. the ``tools/call`` error body (byte-identical),
2. the advertised count in the dispatcher description,
3. the ``action="help"`` resource tree,
4. the ``action="help"`` hint dict, which lives in a *separate* dict from the
   resource tree and is therefore the surface that gets missed.

Plus the three ``difflib`` suggesters, each of which enumerates a name set and
each of which is a disclosure oracle in its own right:

* ``views.py`` — tool names, from the global ``tool_registry``
* ``backends/group_dispatcher.py`` — resources, from the closed-over ``tool_names``
* ``backends/dispatcher.py`` — actions, from the unfiltered ``meta.actions``

On the byte-identity assertion
------------------------------
The naive reading of the acceptance criterion — "invoke denied resource ``N``,
invoke unknown resource ``M``, assert the bodies match" — is not satisfiable.
Both error paths interpolate the requested name into the message::

    registry.py         raise ToolNotFoundError(f"No tool registered with name {name!r}")
    group_dispatcher.py raise LookupError(f"Unknown tool {target_name!r} in group {group!r}.")

so the bodies necessarily differ by ``N`` vs ``M`` under *every* correct
implementation.  The invariant that is both meaningful and testable holds the
**name constant** and varies only *why* the name is absent:

    response(route=A,  name=N)   # N denied on A
    ==  byte-identical  ==
    response(route=A', name=N)   # A' never discovered N

Same name, same path, same token, same tier.  Only the reason for absence
differs — which is the only thing an attacker is probing for.  See
:func:`assert_absence_indistinguishable`.

Coverage spans **both** surface shapes required by the task:

* grouped / dispatch-group (``catalog:item``)
* flat / greenfield (no groups)

A flat-only suite passes while the grouped deny path is wide open — see the
group-dispatcher deny-bypass finding.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.skip(
    reason="Awaits PR-6 (RouteView construction) and PR-8 (absence invariants). "
    "Assertions are written; unskip when route_views.py lands."
)


# ---------------------------------------------------------------------------
# Route fixtures — the two configs differ ONLY in why `item` is absent.
# ---------------------------------------------------------------------------

#: Route A: `item` exists on the surface but is denied by the route.
ROUTE_DENIED: dict[str, Any] = {
    "default": {
        "path": "mcp",
        "highest_tier": "read_write",
        "allow_list": ["catalog"],
        "deny_list": ["catalog:item"],
    }
}

#: Route A': identical, but `item` was never discovered at all.  Produced by
#: restricting discovery rather than by denying — so the route config carries no
#: deny_list and the tool genuinely does not exist in the registry snapshot.
ROUTE_NEVER_DISCOVERED: dict[str, Any] = {
    "default": {
        "path": "mcp",
        "highest_tier": "read_write",
        "allow_list": ["catalog"],
        "deny_list": [],
    }
}


def assert_absence_indistinguishable(denied_response: Any, undiscovered_response: Any) -> None:
    """Assert two responses are byte-identical.

    *denied_response* asked for name ``N`` on a route where ``N`` is denied.
    *undiscovered_response* asked for the **same name ``N``** on a route where
    ``N`` was never discovered.  The raw bytes must match exactly — not the
    status code, not the error code, not the "shape".  A ``assertEqual(status,
    404)`` passes while the invariant is broken.
    """
    assert denied_response.content == undiscovered_response.content
    assert denied_response.status_code == undiscovered_response.status_code


# ---------------------------------------------------------------------------
# WI-1.1 — error parity
# ---------------------------------------------------------------------------


class TestErrorParityGroupedSurface:
    """Denied resource vs never-discovered resource on a dispatch-group surface."""

    def test_denied_resource_error_is_byte_identical_to_undiscovered(self) -> None:
        """`catalog` + resource=item: denied route == never-discovered route."""
        raise NotImplementedError

    def test_denied_whole_group_matches_unknown_group(self) -> None:
        """Denying `catalog` entirely is indistinguishable from `catalog` never existing."""
        raise NotImplementedError

    def test_suggester_never_names_a_denied_resource(self) -> None:
        """A near-miss (`itme`) must not surface `item` in "Did you mean".

        Guards ``group_dispatcher``'s suggester, whose `available_resources` is
        derived from the closed-over global `tool_names`.
        """
        raise NotImplementedError


class TestErrorParityFlatSurface:
    """Same invariants where no dispatch groups exist."""

    def test_denied_tool_error_is_byte_identical_to_undiscovered(self) -> None:
        """Flat tool name: denied route == never-discovered route."""
        raise NotImplementedError

    def test_tool_name_suggester_never_names_a_denied_tool(self) -> None:
        """Guards ``views.py``'s suggester, which reads the global tool_registry.

        The comment above that call says the full tool list is "intentionally
        omitted — listing all names in the error leaks the discovery surface to
        callers who have not made an explicit tools/list call."  The suggestion
        list leaks the same surface three names at a time.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WI-1.2 — advertised counts
# ---------------------------------------------------------------------------


class TestAdvertisedCountParity:
    """The dispatcher description count must be computed from the filtered set."""

    def test_description_count_equals_help_entry_count(self) -> None:
        """Assert description-count == len(help entries).

        Deliberately NOT asserted against a literal: a route advertising 13
        while `help` lists 12 is the leak, and a literal would drift with the
        fixture surface.
        """
        raise NotImplementedError

    def test_denied_resource_not_counted_in_group_description(self) -> None:
        """Denied resources must not inflate the advertised count."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WI-1.3 — help resource tree + hint dict
# ---------------------------------------------------------------------------


class TestHelpTreeAndHintParity:
    """`action='help'` must omit denied resources from BOTH dicts."""

    def test_help_resource_tree_omits_denied_resource(self) -> None:
        """`resources_map` must not contain a denied resource."""
        raise NotImplementedError

    def test_help_hint_dict_omits_denied_resource(self) -> None:
        """The hint dict is filtered by tier and capability, never by route.

        ``_filter_hints`` resolves each hint key through ``registry.get_entry``
        — the global registry.  A denied tool's operator hint survives whenever
        it clears tier + capability.  This is the surface that gets missed
        because it lives in a separate dict from the resource tree.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# WI-1.4 — structural absence
# ---------------------------------------------------------------------------


class TestStructuralAbsence:
    """Denied entries must be physically absent, not filtered at call time.

    Live as of the BLOCKER-2 ruling: **option (a), per-route dispatcher entries**.
    A rebuilt dispatcher entry prunes four inputs, and each unpruned input is its
    own leak:

    ==  ============================  ======================================
    #   Input                         Leak if left global
    ==  ============================  ======================================
    1   ``tool_names`` frozenset      invocation bypass — deny unenforced
    2   ``resource_prefixes``         denied resource named back in an error
    3   frozen ``description``        advertised count (route 13, help 12)
    4   ``group_tool_names``          downstream readers see the global set
    ==  ============================  ======================================

    Hints need no separate prune: ``group_hints`` filters against the closure's
    ``tool_names``, so pruning #1 fixes them for free.  That is (a)'s payoff over
    a call-time gate, in one line.

    #2 is the one that survives a naive fix.  ``apps.py`` always passes
    ``prefix_set``, never ``None``, so the ``{t.split(sep, 1)[0] for t in
    tool_names}`` fallback in the suggester is dead code — pruning ``tool_names``
    alone leaves the oracle fully intact.
    """

    def test_denied_group_physically_absent_from_built_dispatcher(self) -> None:
        """Introspect the built dispatcher; a wholly-denied group must not be present."""
        raise NotImplementedError

    def test_deny_reaches_inside_group_dispatcher(self) -> None:
        """`deny_list: ["catalog:item"]` must block `catalog` + resource=item.

        The shared closure's membership gate checks the global member set and
        then dispatches against the global registry.  Filtering
        `RouteView.entries` at the top level does not reach it: `catalog` itself is
        allowed.  This test fails if deny is bypassable through the dispatcher.
        """
        raise NotImplementedError

    def test_rebuilt_entry_prunes_resource_prefixes(self) -> None:
        """Prune site #2 — the one a `tool_names`-only fix leaves wide open."""
        raise NotImplementedError

    def test_rebuilt_entry_prunes_frozen_description_count(self) -> None:
        """Prune site #3 — the description string is frozen at registration."""
        raise NotImplementedError

    def test_rebuilt_entry_prunes_group_tool_names(self) -> None:
        """Prune site #4 — entry membership as seen by downstream readers."""
        raise NotImplementedError

    def test_partial_carve_out_rebuilds_rather_than_drops_the_group(self) -> None:
        """`allow: [catalog]`, `deny: [catalog:item]` keeps `catalog` with `item` absent."""
        raise NotImplementedError
