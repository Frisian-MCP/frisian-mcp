"""
ADR-011 §4/§5 — redemption re-authorizes against the *current* route surface.

These tests exist because the obvious implementation of §4 — comparing the
redeeming route's tier ceiling against the minting route's — passes a plausible
test suite and permits exactly the cross-route service the control was built to
refuse.  :class:`TestCeilingComparisonWouldNotCatchThis` is written so that a
ceiling comparison **fails it**; if that test ever passes against an
implementation that only compares tiers, it has stopped doing its job.

The surface is exercised through real :class:`RouteView` objects built from a
real registry, not stubs, because the fact under test *is* the view: which
members survive a route's allow/deny carve-out.
"""

from __future__ import annotations

from typing import Any

from django.test import RequestFactory

from frisian_mcp.route_views import RouteView
from frisian_mcp.views import _build_heavy_cache_entry, _redemption_target_authorized

from tests.test_route_views import _cfg, _make_registry


def _request(view: RouteView | None, tier: str = "read") -> Any:
    """A POST stand-in carrying the two stamps the re-authorization reads."""
    req = RequestFactory().post("/mcp/")
    req._mcp_route_view = view  # type: ignore[attr-defined]
    # ADR-010 §8: the one-shot min(token, ceiling, MAX_TIER) stamp.
    req._mcp_effective_tier = tier  # type: ignore[attr-defined]
    return req


class TestGroupedMembershipIsRouteScoped:
    """§4: the child is checked against *this* route's pruned membership."""

    def test_member_of_an_intact_group_is_authorized(self) -> None:
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(allow=("*",)))
        assert _redemption_target_authorized(_request(view), "catalog", "order_list") is True

    def test_member_denied_on_this_route_is_refused(self) -> None:
        """The token was minted where ``order_list`` was mounted; here it is not."""
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(deny=("catalog:order",)))
        req = _request(view)
        assert _redemption_target_authorized(req, "catalog", "order_list") is False
        # The surviving sibling is unaffected — this is a carve-out, not a
        # blanket refusal of everything grouped.
        assert _redemption_target_authorized(req, "catalog", "item_list") is True

    def test_outer_dispatcher_absent_from_this_route_is_refused(self) -> None:
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(allow=("ping",)))
        assert _redemption_target_authorized(_request(view), "catalog", "item_list") is False

    def test_a_name_that_is_not_a_member_is_refused(self) -> None:
        """``ping`` is mounted, but it is not reachable *through* ``catalog``."""
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(allow=("*",)))
        assert _redemption_target_authorized(_request(view), "catalog", "ping") is False


class TestCeilingComparisonWouldNotCatchThis:
    """
    §4's stated trap, made executable.

    Both routes declare the **same** ceiling and differ only in their allow/deny
    carve-out.  An implementation that compared ceilings would authorize both
    and look entirely correct.
    """

    def test_same_ceiling_different_surface(self) -> None:
        reg = _make_registry()
        minted_on = RouteView.build(reg, _cfg("wide", allow=("*",), highest_tier="read_write"))
        redeemed_on = RouteView.build(
            reg, _cfg("narrow", allow=("*",), deny=("catalog:order",), highest_tier="read_write")
        )

        assert minted_on.ceiling == redeemed_on.ceiling  # the trap: ceilings agree

        assert _redemption_target_authorized(_request(minted_on), "catalog", "order_list") is True
        assert (
            _redemption_target_authorized(_request(redeemed_on), "catalog", "order_list") is False
        )


class TestTierIsTheChildsNotTheDispatchers:
    """
    §4: dispatchers register as ``read`` so they stay visible as navigation
    entry-points.  Reading the tier off the *outer* entry would therefore
    authorize a write-tier child for a read-tier caller.
    """

    def test_read_caller_cannot_redeem_a_write_tier_child(self) -> None:
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(allow=("*",)))
        assert reg.get_entry("catalog").permission_tier == "read"  # the outer says read
        assert reg.get_entry("item_create").permission_tier == "read_write"

        assert (
            _redemption_target_authorized(_request(view, tier="read"), "catalog", "item_create")
            is False
        )

    def test_write_caller_can(self) -> None:
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(allow=("*",)))
        assert (
            _redemption_target_authorized(
                _request(view, tier="read_write"), "catalog", "item_create"
            )
            is True
        )


class TestCapabilityFilterIsApplied:
    """§4: the same per-user entry filter ``tools/list`` uses under
    ``PERMISSION_AWARE_DISCOVERY``."""

    def test_entry_filter_can_refuse(self) -> None:
        reg = _make_registry()
        view = RouteView.build(reg, _cfg(allow=("*",)))
        req = _request(view)
        req._mcp_perm_entry_filter = lambda entry: entry.name != "order_list"  # type: ignore

        assert _redemption_target_authorized(req, "catalog", "order_list") is False
        assert _redemption_target_authorized(req, "catalog", "item_list") is True


class TestSection5EntryShape:
    """§5: the entry records the server-resolved child, not only the outer name."""

    def test_resolved_target_is_recorded_when_supplied(self) -> None:
        entry = _build_heavy_cache_entry({"d": 1}, _request(None), "catalog", "order_list")
        assert entry["resolved_target"] == "order_list"
        # The owner key still binds the OUTER name (G1) — §5 is a third fact,
        # deliberately not an ownership dimension (§6).
        assert entry["tool_name"] == "catalog"

    def test_flat_calls_default_to_the_tool_itself(self) -> None:
        entry = _build_heavy_cache_entry({"d": 1}, _request(None), "ping")
        assert entry["resolved_target"] == "ping"
