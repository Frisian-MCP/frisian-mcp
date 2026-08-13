"""
H22 guard, mutation-checked against all three historical fabrication cases.

The task's bar: *"A guard that cannot reproduce H7, H17 and today's failure is
not a guard."* Each case below drives a **real security read site** from the
package with a bare ``MagicMock`` request — the exact fixture that shipped the
false result — and asserts the guard turns it into a loud failure. Then the
legitimate counterpart (explicit construction) is asserted to pass untouched, so
the guard discriminates rather than merely forbidding mocks.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

import pathlib
import re
from unittest.mock import MagicMock

import pytest

from frisian_mcp.registry import _resolve_request_tier
from frisian_mcp.views import _redemption_action_authorized
from tests._mcp_mock_guard import (
    GUARDED_ATTRS,
    McpMockFabricationError,
    mcp_request,
    mock_fabrication_guard,
)

#: The three historical fabrications, by the attribute each one steered.
_HISTORICAL = {
    "_mcp_effective_tier": "H7 — a Mock ranked as a tier",
    "_mcp_perm_entry_filter": "H17 — a fabricated filter refused/permitted from nothing",
    "_mcp_capabilities": "today — every member read invisible",
}

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "frisian_mcp"


# ---------------------------------------------------------------------------
# The guarded set is a contract with the source, not a hand-kept list
# ---------------------------------------------------------------------------


def test_guarded_set_matches_security_reads_in_source() -> None:
    """
    Guard the guarded set itself against the source it claims to cover.

    Every ``_mcp_*`` attribute read via ``getattr(request, ..., None)`` in the
    security-decision modules must be in ``GUARDED_ATTRS``.  Without this, a
    fourth attribute could be added to a gate and fabricated
    freely — the guard would pass while a new instance of the exact defect
    shipped. Discovered from source so the set cannot silently fall behind.
    """
    pattern = re.compile(r"getattr\(request,\s*[\"'](_mcp_[a-z_]+)[\"']")
    found: set[str] = set()
    for module in (
        "registry.py",
        "views.py",
        "backends/dispatcher.py",
        "backends/group_dispatcher.py",
    ):
        text = (_SRC / module).read_text()
        found.update(pattern.findall(text))
    # _mcp_agent_connection is bookkeeping (last-seen timestamp), not a gate;
    # it is the one read that does not steer an authz/visibility branch.
    gating = found - {"_mcp_agent_connection"}
    missing = gating - GUARDED_ATTRS
    assert not missing, (
        f"security-relevant request attributes read in source but not guarded: {missing}. "
        f"Add them to GUARDED_ATTRS or the H22 defect can recur on a new attribute."
    )


# ---------------------------------------------------------------------------
# Mutation check: each historical case, reproduced at a real read site
# ---------------------------------------------------------------------------


class TestReproducesHistoricalFabrications:
    """
    Drive real gates with the fixtures that shipped the false results.

    Each test drives a REAL security gate with the bare-``MagicMock`` fixture
    that shipped a false result, and asserts the guard turns it into a hard
    failure.  These assert the gate *cannot run on a fabricating request*, which is the
    property that matters — not which guarded attribute happens to be read
    first. Per-attribute naming precision is covered separately below.
    """

    def test_h7_gate_cannot_run_on_a_fabricating_request(self) -> None:
        """
        H7: the tier gate cannot run on a fabricating request.

        ``_resolve_request_tier`` reads ``_mcp_effective_tier`` first, so the
        guard fires there.
        """
        with mock_fabrication_guard(), pytest.raises(McpMockFabricationError) as exc:
            _resolve_request_tier(MagicMock())
        assert "_mcp_effective_tier" in str(exc.value)

    def test_today_redemption_gate_cannot_run_on_a_fabricating_request(self) -> None:
        """
        Today / H17-family: the redemption re-authorization gate cannot run either.

        ``_redemption_action_authorized`` is the ADR-011 §4 gate that reads both
        ``_mcp_route_view`` and ``_mcp_capabilities``.  A bare mock fabricates
        the route view first, so the gate is stopped before it can build a lens
        from a Mock.  Reproduced end to end against the real function, not a
        hand-rolled read.
        """
        with mock_fabrication_guard(), pytest.raises(McpMockFabricationError) as exc:
            _redemption_action_authorized(MagicMock(), "svc", "list")
        assert "_mcp_" in str(exc.value)

    @pytest.mark.parametrize("attr", sorted(_HISTORICAL))
    def test_each_historical_attribute_is_named_when_fabricated(self, attr: str) -> None:
        """
        Every historical attribute is individually caught and named.

        This is the by-name half of the mutation check: H7, H17 and today each
        map to a guarded attribute the guard reports at its read site.
        """
        request = MagicMock()
        with mock_fabrication_guard(), pytest.raises(McpMockFabricationError) as exc:
            getattr(request, attr)  # the exact fabrication each case hit
        assert attr in str(exc.value), _HISTORICAL[attr]


# ---------------------------------------------------------------------------
# Discrimination: legitimate explicit construction is NOT caught
# ---------------------------------------------------------------------------


class TestLegitimateConstructionSurvives:
    """The guard must fire on fabrication only, never on a deliberate stamp."""

    @pytest.mark.parametrize("attr", sorted(GUARDED_ATTRS))
    def test_explicit_kwarg_construction_is_allowed(self, attr: str) -> None:
        """``MagicMock(_mcp_x=value)`` sets the attribute; it never fabricates."""
        with mock_fabrication_guard():
            request = MagicMock(**{attr: None})
            assert getattr(request, attr) is None

    @pytest.mark.parametrize("attr", sorted(GUARDED_ATTRS))
    def test_explicit_assignment_is_allowed(self, attr: str) -> None:
        """``m._mcp_x = value`` after construction is equally legitimate."""
        with mock_fabrication_guard():
            request = MagicMock()
            setattr(request, attr, "read")
            assert getattr(request, attr) == "read"

    def test_unguarded_attribute_still_fabricates_freely(self) -> None:
        """The guard is scoped: a non-security attribute is untouched."""
        with mock_fabrication_guard():
            request = MagicMock()
            assert request._mcp_agent_connection is not None  # not a gate, not guarded

    def test_faithful_request_returns_production_defaults(self) -> None:
        """``mcp_request()`` yields ``None`` for every guarded attribute."""
        request = mcp_request()
        for attr in GUARDED_ATTRS:
            assert getattr(request, attr, None) is None

    def test_faithful_request_honours_explicit_stamps(self) -> None:
        """A deliberately stamped value is preserved (the legitimate path)."""
        request = mcp_request(_mcp_effective_tier="read")
        assert getattr(request, "_mcp_effective_tier", None) == "read"


# ---------------------------------------------------------------------------
# The guard is reversible and does not leak across tests
# ---------------------------------------------------------------------------


def test_guard_is_removed_on_exit() -> None:
    """Outside the context manager, ordinary mock behaviour is restored."""
    request = MagicMock()
    # No guard here: fabrication is the stdlib default again.
    assert request._mcp_capabilities is not None
