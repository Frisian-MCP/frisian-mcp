"""
Conformance coverage for ``verify`` placement on both dispatcher kinds.

Split out from ``test_heavy_negotiation_conformance`` because these findings
move independently: the negotiation-protocol tests track a fix that has
landed, while everything here records placement behaviour that is still
awaiting a ruling on whether the documentation or the code is the thing that
is wrong.

Origin: a documentation review flagged that the write-path documentation shows
``verify`` as a top-level sibling of ``params`` on a group-dispatcher call, and
that this placement appears to do nothing.  That is reproduced here through the
view rather than by code reading, and extended to the ``@mcp_dispatcher`` path,
which had been flagged as unproven.

These tests **pin measured behaviour**; they do not change it.  Where current
behaviour is wrong, the desired behaviour is asserted in a separate
``xfail(strict=True)`` test so that a fix turns into an XPASS failure and
forces the marker out, rather than leaving a stale exemption behind.
"""

# pylint: disable=redefined-outer-name

from __future__ import annotations

from typing import Any

import pytest
from django.test import RequestFactory

from frisian_mcp.registry import ToolRegistry
from tests.negotiation_harness import (
    class_dispatcher_registry,
    group_registry,
    install_group,
    write_call,
)


@pytest.fixture()
def group_reg(settings: Any) -> ToolRegistry:
    """Return an isolated registry with the ``catalog`` group dispatcher installed."""
    reg = group_registry()
    install_group(reg, settings)
    return reg


@pytest.fixture()
def class_reg() -> ToolRegistry:
    """Return an isolated registry with the ``tasks`` class dispatcher installed."""
    return class_dispatcher_registry()


def _is_full_object(result: Any) -> bool:
    """A verified write returns the object itself; a lean one returns an envelope."""
    return isinstance(result, dict) and "received" in result


class TestVerifyPlacementOnGroupDispatcher:
    """
    OPEN DEFECT (T7-F3).  Top-level ``verify`` on a group dispatcher is ignored.

    The write-path strip in ``views`` is gated on the *entry's* ``is_write``
    flag, and a group dispatcher is registered ``is_dispatcher=True`` with
    ``is_write`` defaulting to ``False``, so that branch never fires.  The
    dispatcher-routed branch further down reads ``verify`` from inside
    ``params`` only.  Net effect: the placement the documentation shows is the
    one placement that does nothing, and it fails silently — the caller gets a
    success with a lean envelope and pays for a second round-trip.

    Whether the repair is a documentation edit or a code change is not
    testing's call.  If it is ruled a documentation fix, delete the xfail below
    and keep the pinned tests.
    """

    def test_verify_inside_params_returns_the_full_object(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The undocumented placement is the one that works."""
        result = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "params": {"name": "w1", "verify": True}},
        )
        assert _is_full_object(result)

    def test_verify_is_stripped_before_the_underlying_tool_sees_it(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """``verify`` is protocol, and must never reach the host's serializer."""
        result = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "params": {"name": "w1", "verify": True}},
        )
        assert "verify" not in result["received"]

    def test_no_verify_returns_the_lean_envelope(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """Baseline: the lean write default is unchanged."""
        result = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "params": {"name": "w1"}},
        )
        assert not _is_full_object(result)
        assert "continuation_token" in result

    def test_top_level_verify_is_indistinguishable_from_no_verify(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        Pin the defect: the documented placement produces the lean envelope.

        Compared against the no-verify baseline rather than a literal shape, so
        this stays meaningful if the envelope's contents change.
        """
        documented = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "params": {"name": "w1"}, "verify": True},
        )
        baseline = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "params": {"name": "w1"}},
        )
        assert not _is_full_object(documented), "measured behaviour changed — re-check T7-F3"
        assert set(documented) == set(baseline)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "OPEN DEFECT (T7-F3): 'verify' at the top level of a group-dispatcher call"
            " — the placement the write-path documentation shows — is silently ignored,"
            " because the top-level strip is gated on entry.is_write and a group"
            " dispatcher registers is_dispatcher=True with is_write defaulting False."
            " The agent follows the documentation exactly, gets a success with a lean"
            " envelope, and pays for a second round-trip.  Ruling owed: repair the"
            " placement asymmetry, or fix the documentation and delete this test."
        ),
    )
    def test_top_level_verify_should_return_the_full_object(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The documented placement should behave like the working one."""
        result = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "params": {"name": "w1"}, "verify": True},
        )
        assert _is_full_object(result)

    def test_flat_form_verify_is_also_ignored(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        Third placement, pinned for completeness.

        With no ``params`` wrapper the flat sweep moves ``verify`` into params
        only *after* the view has already looked for it at the top level, so
        this placement is ignored too.  All three placements are now covered;
        previously only the nested one was.
        """
        result = write_call(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "create", "name": "w1", "verify": True},
        )
        assert not _is_full_object(result)


class TestVerifyPlacementOnClassDispatcher:
    """
    OPEN DEFECT (T7-F4).  On ``@mcp_dispatcher``, ``verify`` is inert in both positions.

    Flagged as unproven by the documentation review and measured here.  There is
    no ``resource`` on this path, so the dispatcher-routed write branch builds an
    empty target name, resolves no entry, and never runs.  Two consequences, and
    the second is the one that bites:

    1. ``verify`` does nothing in either placement — but writes already return
       the full object here, so nothing is truncated by it.
    2. ``verify`` placed inside ``params`` is **not stripped**, and is forwarded
       to the action as though it were a real parameter.  The group path strips
       protocol params before dispatch; this path has no equivalent, so a host
       serializer that rejects unknown fields would fail the write outright.
    """

    def test_write_returns_the_full_object_regardless_of_verify(
        self, rf: RequestFactory, class_reg: ToolRegistry
    ) -> None:
        """The lean write envelope never applies on the class-dispatcher path."""
        for arguments in (
            {"action": "create", "params": {"title": "t"}},
            {"action": "create", "params": {"title": "t"}, "verify": True},
        ):
            result = write_call(rf, class_reg, "tasks", arguments)
            assert result["id"] == 7
            assert "received" in result

    def test_top_level_verify_changes_nothing(
        self, rf: RequestFactory, class_reg: ToolRegistry
    ) -> None:
        """Pinned: top-level ``verify`` is a no-op here, not merely undocumented."""
        without = write_call(rf, class_reg, "tasks", {"action": "create", "params": {"title": "t"}})
        with_flag = write_call(
            rf, class_reg, "tasks", {"action": "create", "params": {"title": "t"}, "verify": True}
        )
        assert without == with_flag

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "OPEN DEFECT (T7-F4): on the @mcp_dispatcher path 'verify' placed inside"
            " 'params' is forwarded to the action instead of being stripped.  The group"
            " dispatcher strips protocol params before dispatch; this path has no"
            " equivalent, so a frisian-mcp protocol flag reaches the host's serializer"
            " as if it were a model field.  A host that rejects unknown fields fails"
            " the write outright."
        ),
    )
    def test_verify_in_params_should_not_reach_the_action(
        self, rf: RequestFactory, class_reg: ToolRegistry
    ) -> None:
        """A protocol flag must not be forwarded to host data-layer code."""
        result = write_call(
            rf, class_reg, "tasks", {"action": "create", "params": {"title": "t", "verify": True}}
        )
        assert "verify" not in result["received"]

    def test_verify_in_params_currently_leaks_to_the_action(
        self, rf: RequestFactory, class_reg: ToolRegistry
    ) -> None:
        """Pin the leak as measured, so the xfail above has its evidence beside it."""
        result = write_call(
            rf, class_reg, "tasks", {"action": "create", "params": {"title": "t", "verify": True}}
        )
        assert result["received"] == {"title": "t", "verify": True}
