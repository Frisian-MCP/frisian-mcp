"""
The package's contribution to an ordinary published schema is ZERO (CR-4).

WHY THIS FILE EXISTS.  ``tests/test_token_usage_contract.py`` already proves
*parity* -- that the reported ``schema_tokens`` matches the ``inputSchema`` the
caller was handed.  Parity is a real guarantee and it is not this one: it passes
happily when **both** sides grow five times larger, which is exactly what
happened when H2 made every schema disclose the continuation protocol.  Parity
did not catch that regression and by construction never will.

TWO ASSERTIONS, DELIBERATELY NOT ONE.  They fail for different reasons and
neither subsumes the other:

* **Zero-delta** (:class:`TestPackageContributionIsZero`) -- the published
  schema is byte-identical to the one handed to registration.  Exact equality,
  no threshold, no headroom judgement.  This is what "back to pre-H2" actually
  means.  It catches a re-merged negotiation branch precisely -- but only on the
  shapes fixtured here.
* **Absolute ceiling** (:class:`TestSchemaTokenCeiling`) -- a tight
  ``schema_tokens`` cap on those same fixtures.  It catches cost arriving by
  some *other* route -- a new always-on property, an inflated description --
  that zero-delta would pass.

SCOPE, AND WHY THERE IS NO GLOBAL CAP.  Do not "improve" this into a cap across
all registered tools.  An ``@mcp_tool`` ``input_schema`` is **the host's own
schema**: the package neither writes it nor controls its size, and hosts
legitimately register large ones.  A global cap fires on content we do not own,
so it would be switched off within a release and take the real assertion with
it.  What the package actually promises is narrower and stronger -- that its own
*contribution* is zero -- and that is what is asserted here, on fixtures the
package fully controls.

Baselines measured in CR-1 against the published schemas, not estimated.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from frisian_mcp.decorators import mcp_action, mcp_dispatcher, mcp_tool
from frisian_mcp.negotiation import (
    _NEGOTIATION_PROPERTIES,
    merge_continuation_branch,
    schema_discloses_continuation,
)
from frisian_mcp.registry import ToolRegistry
from frisian_mcp.usage import count_value, encoding_name

# ---------------------------------------------------------------------------
# Fixtures the PACKAGE controls -- deliberately not a host's schema
# ---------------------------------------------------------------------------

#: The degenerate shape: a tool that declares nothing of its own.  Any token
#: cost measured here is the package's, because there is nothing else in it.
_EMPTY_TOOL_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

#: A realistic ordinary tool.  The empty schema alone cannot detect "an
#: inflated description on a shape we do control" -- there is no description in
#: it to inflate -- so the ceiling needs a fixture with real content too.
_ORDINARY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "identifier": {"type": "string", "description": "Record identifier."},
        "limit": {"type": "integer", "description": "Maximum records to return."},
        "expand": {"type": "boolean", "description": "Include related records."},
    },
    "required": ["identifier"],
}

#: Ceilings in ``schema_tokens``, as the package's own counter reports them.
#:
#: Sized just above the CR-1 pre-H2 baseline -- tight, not comfortable.  Each is
#: the **maximum across both encodings**, because the two disagree and NOT in a
#: consistent direction: the empty fixture is 11 tokens under ``cl100k_base``
#: and 9 under ``approx-char4``, while the auto-discovered action is 58 and 65
#: respectively.  This repo's default venv has no tiktoken and counts
#: ``approx-char4``; CI installs the ``usage`` extra and counts ``cl100k_base``.
#: A ceiling written against one encoding passes locally and fails in CI, or the
#: reverse -- so every ceiling here must hold under both.
#:
#: For scale: with the H2 negotiation branch merged in, these same fixtures
#: measured 391, 451 and 440 tokens.  The ceilings sit roughly five times below
#: that, so a re-merged branch overshoots by 300+ and cannot creep past.
_CEILING_EMPTY_TOOL = 16
_CEILING_ORDINARY_TOOL = 85
_CEILING_DISCOVERED_ACTION = 80

#: What the same fixtures cost once a negotiation branch is merged in (CR-1).
#: Used to assert the ceilings stay meaningfully below the regression rather
#: than being quietly widened until they admit it.
_MERGED_COST_FLOOR = 300


def _register_plain(schema: dict[str, Any]) -> tuple[ToolRegistry, dict[str, Any]]:
    """Register *schema* through ``@mcp_tool`` and return (registry, published schema)."""
    reg = ToolRegistry()
    with patch("frisian_mcp.decorators.tool_registry", reg):

        @mcp_tool(
            name="fixture_read",
            description="Package-controlled fixture tool.",
            # Deep-copied so the assertion compares against an untouched
            # reference even if a future merge were to mutate in place.
            input_schema=json.loads(json.dumps(schema)),
        )
        def _fn(_arguments: dict[str, Any], _request: Any) -> Any:
            return []

        _ = _fn

    return reg, reg.get_entry("fixture_read").input_schema


def _discovered_action_schema() -> dict[str, Any]:
    """Return the published ``inputSchema`` of a package-controlled ViewSet action."""
    from frisian_mcp.backends.discovery import (  # pylint: disable=import-outside-toplevel
        DRFSyncDiscovery,
    )

    with override_settings(ROOT_URLCONF="tests.urls"):
        discovered = {t.name: t for t in DRFSyncDiscovery().discover_tools()}
    return discovered["users_list"].input_schema


def _why(measured: int, expected: int, what: str) -> str:
    """Return a failure message that names the numbers and the likely cause."""
    return (
        f"{what}: measured {measured} schema_tokens, ceiling {expected} "
        f"(encoding={encoding_name()}).\n"
        "Likely cause: a negotiation branch or a new always-on property reached "
        "the default registration path, or a field description grew.\n"
        "This ceiling encodes Jeremy's 'back to pre-H2' target. Investigate the "
        "growth before considering raising it -- the last time this cost arrived "
        "it was a ~5x regression on every ordinary tool."
    )


# ---------------------------------------------------------------------------
# (a) Zero-delta -- the primary assertion
# ---------------------------------------------------------------------------


class TestPackageContributionIsZero:
    """The published schema is byte-identical to the one handed to registration."""

    @pytest.mark.parametrize(
        "schema", [_EMPTY_TOOL_SCHEMA, _ORDINARY_TOOL_SCHEMA], ids=["empty", "ordinary"]
    )
    def test_published_schema_is_byte_identical(self, schema: dict[str, Any]) -> None:
        """
        ``@mcp_tool`` publishes the caller's schema unchanged -- exactly.

        Compared as serialized JSON rather than by ``==`` so that key REORDERING
        fails too: two dicts with the same items compare equal, but the bytes a
        client is handed are ordered, and ``schema_tokens`` counts bytes.
        """
        _reg, published = _register_plain(schema)

        assert json.dumps(published) == json.dumps(schema), (
            "@mcp_tool altered the host's schema. The package's contribution to "
            "an ordinary published schema must be zero -- not small.\n"
            f"  handed to us: {json.dumps(schema)}\n"
            f"  published:    {json.dumps(published)}"
        )

    @pytest.mark.parametrize(
        "schema", [_EMPTY_TOOL_SCHEMA, _ORDINARY_TOOL_SCHEMA], ids=["empty", "ordinary"]
    )
    def test_published_schema_discloses_nothing(self, schema: dict[str, Any]) -> None:
        """
        No negotiation surface, asserted through the mint gate's own predicate.

        ``schema_discloses_continuation`` is what ``views.py`` consults before
        minting, so asserting through it means this test and the gate cannot
        disagree about what disclosure means.
        """
        _reg, published = _register_plain(schema)

        assert schema_discloses_continuation(published) is False
        assert "allOf" not in published
        assert not set(published.get("properties", {})) & set(_NEGOTIATION_PROPERTIES)

    def test_discovered_action_discloses_nothing(self) -> None:
        """
        An auto-discovered ViewSet action publishes no negotiation surface.

        NOT asserted as byte-identity against the ``ToolDefinition``, and that is
        deliberate.  Pre-CR-2 the merge ran *inside* ``discover_tools`` before the
        definition was constructed, so a definition-vs-registered comparison was
        already byte-identical while the schema was fully merged -- it would have
        passed on the regression, which is a false green.  The honest assertions
        on this path are that nothing is disclosed and that the schema is
        genuinely un-merged (see the open-fixture guard below).
        """
        schema = _discovered_action_schema()

        assert schema_discloses_continuation(schema) is False
        assert "allOf" not in schema
        assert not set(schema.get("properties", {})) & set(_NEGOTIATION_PROPERTIES)

    @pytest.mark.parametrize(
        "schema",
        [_EMPTY_TOOL_SCHEMA, _ORDINARY_TOOL_SCHEMA, None],
        ids=["empty", "ordinary", "discovered"],
    )
    def test_fixtures_are_open_so_the_assertions_cost_something(
        self, schema: dict[str, Any] | None
    ) -> None:
        """
        GUARD: none of the above passes for a trivial reason.

        ``merge_continuation_branch`` returns a schema declaring
        ``"additionalProperties": false`` unchanged (H18).  A closed fixture
        would therefore satisfy every zero-delta and no-disclosure assertion in
        this file **on the pre-CR-2 tree as well** -- a green that proves nothing
        and would stay green if the removal were reverted.

        Asserting that merging WOULD change these schemas pins them open, so the
        contract keeps costing something to satisfy.
        """
        published = _discovered_action_schema() if schema is None else _register_plain(schema)[1]

        assert published.get("additionalProperties") is not False, (
            "fixture is a CLOSED schema; the negotiation merge skips those, so "
            "every assertion in this file would pass even on the regression"
        )
        assert merge_continuation_branch(json.loads(json.dumps(published))) != published


# ---------------------------------------------------------------------------
# (b) Absolute ceiling -- the backstop for (a)
# ---------------------------------------------------------------------------


class TestSchemaTokenCeiling:
    """
    A tight cap on PACKAGE-CONTROLLED fixtures only.

    See the module docstring for why this is not, and must not become, a global
    cap across all registered tools.
    """

    def test_empty_tool_schema_stays_under_ceiling(self) -> None:
        """A tool declaring nothing costs almost nothing to publish."""
        _reg, published = _register_plain(_EMPTY_TOOL_SCHEMA)
        measured = count_value(published)

        assert measured <= _CEILING_EMPTY_TOOL, _why(
            measured, _CEILING_EMPTY_TOOL, "empty package fixture tool"
        )

    def test_ordinary_tool_schema_stays_under_ceiling(self) -> None:
        """A realistic three-field tool stays at its own size, plus nothing."""
        _reg, published = _register_plain(_ORDINARY_TOOL_SCHEMA)
        measured = count_value(published)

        assert measured <= _CEILING_ORDINARY_TOOL, _why(
            measured, _CEILING_ORDINARY_TOOL, "ordinary package fixture tool"
        )

    def test_discovered_action_schema_stays_under_ceiling(self) -> None:
        """An auto-discovered action -- the most numerous population on a host."""
        measured = count_value(_discovered_action_schema())

        assert measured <= _CEILING_DISCOVERED_ACTION, _why(
            measured, _CEILING_DISCOVERED_ACTION, "auto-discovered fixture action"
        )

    def test_ceilings_stay_below_the_regression_they_exist_to_catch(self) -> None:
        """
        META: the ceilings cannot be widened until they admit the regression.

        A ceiling raised past the merged cost is not a ceiling.  Pinning them
        below that cost means "just bump the number" stops being an option long
        before it stops being an alarm.
        """
        for name, ceiling in (
            ("_CEILING_EMPTY_TOOL", _CEILING_EMPTY_TOOL),
            ("_CEILING_ORDINARY_TOOL", _CEILING_ORDINARY_TOOL),
            ("_CEILING_DISCOVERED_ACTION", _CEILING_DISCOVERED_ACTION),
        ):
            assert ceiling < _MERGED_COST_FLOOR, (
                f"{name} is {ceiling}, at or above the ~{_MERGED_COST_FLOOR}+ tokens a "
                "merged negotiation branch costs. A ceiling that admits the "
                "regression is not a ceiling -- investigate the growth instead."
            )


# ---------------------------------------------------------------------------
# The retained heavy paths must NOT be zero-delta
# ---------------------------------------------------------------------------


class TestRetainedPathsStillDisclose:
    """
    The dispatcher keeps the flat merge, so its delta is the merge -- not zero.

    The point of PR #62 is the ``@mcp_heavy`` / dispatcher continuation fix.  A
    zero-delta check that leaked onto those paths would quietly undo it, so the
    expectation is pinned in the opposite direction here: the retained paths are
    asserted to still cost the merge.
    """

    def test_class_dispatcher_schema_still_discloses(self) -> None:
        """A class dispatcher publishes the negotiation protocol, by design."""
        reg = ToolRegistry()
        with patch("frisian_mcp.decorators.tool_registry", reg):

            @mcp_dispatcher("fixture_ops", description="Package-controlled dispatcher.")
            class _Ops:
                """Synthetic dispatcher."""

                @mcp_action("list", description="List records.", params={})
                def list(self, request: Any, params: dict[str, Any]) -> Any:
                    """Return nothing."""
                    # pylint: disable=unused-argument
                    return []

            _ = _Ops

        published = reg.get_entry("fixture_ops").input_schema

        assert schema_discloses_continuation(published) is True
        assert set(_NEGOTIATION_PROPERTIES) <= set(published["properties"]), (
            "the class dispatcher stopped publishing the negotiation fields; that "
            "is the fix PR #62 exists to deliver, not a token saving"
        )
        assert count_value(published) > _CEILING_ORDINARY_TOOL, (
            "a dispatcher schema is expected to carry the flat merge and therefore "
            "to sit well above the ordinary-tool ceiling; measuring below it "
            "suggests the merge was removed from the retained path"
        )
