"""
T7 conformance suite for the heavy response-negotiation protocol.

Scope note, because the framing of this defect changed twice:

The four advertised modes are **reachable**.  They always were.  What was
missing is **disclosure** — the agent was never told where the negotiation
fields go, so it put them where a reasonable reader would (inside ``params``,
or at top level without a token) and got a lean envelope or a filter error
back.  This suite therefore tests *discoverability and placement*, not
reachability alone, and it deliberately does **not** assert that the filterset
is lenient: rejecting a non-filter is correct behaviour.

Two kinds of test live here and they are labelled inline:

* **GATE** — asserts behaviour introduced by the disclosure fix.  Fails before
  it, passes after.
* **LOCK-IN** — asserts behaviour that was already correct and that a future
  change must not silently "improve".  Passes both before and after.

Tests marked ``xfail(strict=True)`` assert the behaviour the protocol
*should* have and record a defect that is still open.  Strict is deliberate:
when the defect is fixed the test XPASSes, which pytest reports as a failure,
forcing the marker to be removed rather than leaving a stale exemption behind.
"""

# pylint: disable=redefined-outer-name,protected-access

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings

from frisian_mcp.backends.group_dispatcher import build_group_input_schema
from frisian_mcp.decorators import _NEGOTIATION_PROPERTIES
from frisian_mcp.registry import ToolInputError, ToolRegistry
from frisian_mcp.views import _build_probe_envelope
from tests.negotiation_harness import (
    ROWS,
    CacheStub,
    call,
    class_dispatcher_registry,
    envelope,
    group_registry,
    install_group,
    payload,
    probe_then,
    result_bytes,
    tool_result,
)

#: The modes the probe envelope advertises to the agent.  Derived from the
#: envelope builder rather than hardcoded so that adding a mode to the
#: advertisement without adding coverage for it fails the meta-test below.
ADVERTISED_MODES: list[str] = _build_probe_envelope({"probe": "shape"}, "tok")["available_modes"]

#: The negotiation field names, taken from the single source of truth in
#: ``decorators``.  Tests derive from this rather than restating five literal
#: names — a hardcoded list would still have passed while the schema omitted
#: the fields, which is precisely how the original defect survived review.
NEGOTIATION_FIELDS: frozenset[str] = frozenset(_NEGOTIATION_PROPERTIES)


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


# ---------------------------------------------------------------------------
# Item 1 + 6 — every advertised mode is reachable, and every mode is covered
# ---------------------------------------------------------------------------


def _assert_summary(served: Any, full: Any) -> None:
    assert isinstance(served, list)
    assert len(served) == 5, "summary must cap a list result at five items"
    assert result_bytes(served) < result_bytes(full)


def _assert_paginated(served: Any, full: Any) -> None:
    assert isinstance(served, dict)
    assert served["page"] == 1
    assert served["total"] == ROWS
    assert served["has_more"] is True
    assert len(served["items"]) == served["page_size"]
    assert result_bytes(served) < result_bytes(full)


def _assert_filtered(served: Any, full: Any) -> None:
    assert isinstance(served, list)
    assert all(set(row) == {"id"} for row in served), "filtered must retain only filter_keys"
    assert result_bytes(served) < result_bytes(full)


def _assert_full(served: Any, full: Any) -> None:
    assert served == full
    assert result_bytes(served) == result_bytes(full)


#: Per-mode shape assertions.  The meta-test below pins this mapping against
#: ``available_modes`` so a newly advertised mode cannot ship uncovered.
MODE_ASSERTIONS = {
    "summary": _assert_summary,
    "paginated": _assert_paginated,
    "filtered": _assert_filtered,
    "full": _assert_full,
}

#: Extra top-level arguments each mode needs to be meaningful.
MODE_EXTRA_ARGS: dict[str, dict[str, Any]] = {
    "paginated": {"page": 1, "page_size": 10},
    "filtered": {"filter_keys": ["id"]},
}


class TestEveryAdvertisedModeIsReachable:
    """
    LOCK-IN.  All four advertised modes work at the canonical top-level placement.

    These passed before the disclosure fix too — that was always the point.
    They are here so that a future change cannot quietly break a mode while the
    envelope keeps advertising it.  Each mode asserts its *declared shape and a
    size relationship*, never merely a 200: a mode that returned the full
    dataset while calling itself ``summary`` would satisfy a naive assertion.
    """

    @pytest.mark.parametrize("mode", ADVERTISED_MODES)
    def test_mode_is_reachable_and_returns_its_declared_shape(
        self, mode: str, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """Each advertised mode round-trips and returns the shape it promises."""
        probe, served = probe_then(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "list"},
            {"mode": mode, **MODE_EXTRA_ARGS.get(mode, {})},
        )
        assert probe["total_size"] == result_bytes(payload())
        MODE_ASSERTIONS[mode](served, payload())

    def test_bounded_modes_are_materially_smaller_than_full(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The bounded modes actually bound: each is a fraction of the full payload."""
        full_bytes = result_bytes(payload())
        for mode in ("summary", "paginated", "filtered"):
            _, served = probe_then(
                rf,
                group_reg,
                "catalog",
                {"resource": "widget", "action": "list"},
                {"mode": mode, **MODE_EXTRA_ARGS.get(mode, {})},
            )
            assert result_bytes(served) < full_bytes / 2, (
                f"mode {mode!r} returned {result_bytes(served)} of {full_bytes} bytes —"
                " it is advertised as a bounded mode but is not bounding anything"
            )


class TestMinimumHonestyOfTheAdvertisement:
    """
    GATE (meta).  The envelope may not advertise a mode this suite does not exercise.

    The original defect was an advertisement the code did not honour.  This is
    the structural guard against the same class of thing recurring: extend
    ``available_modes`` without extending coverage and the suite fails.
    """

    def test_every_advertised_mode_has_a_shape_assertion(self) -> None:
        """Each string in ``available_modes`` has a corresponding shape assertion."""
        assert set(ADVERTISED_MODES) == set(MODE_ASSERTIONS), (
            "available_modes and the tested modes have diverged: "
            f"advertised-but-untested={sorted(set(ADVERTISED_MODES) - set(MODE_ASSERTIONS))}, "
            f"tested-but-unadvertised={sorted(set(MODE_ASSERTIONS) - set(ADVERTISED_MODES))}"
        )

    def test_advertisement_is_not_empty(self) -> None:
        """A silently emptied advertisement must not read as trivially honest."""
        assert ADVERTISED_MODES, "the probe envelope advertises no modes at all"


# ---------------------------------------------------------------------------
# Item 3 — inputSchema round-trip, derived rather than hardcoded
# ---------------------------------------------------------------------------


def _published_schema(reg: ToolRegistry, tool: str) -> dict[str, Any]:
    """Return the inputSchema for *tool* exactly as ``tools/list`` publishes it."""
    with patch("frisian_mcp.registry.tool_registry", reg):
        for tool_def in reg.list_tools():
            if tool_def["name"] == tool:
                return tool_def["inputSchema"]  # type: ignore[no-any-return]
    raise AssertionError(f"tool {tool!r} not present in tools/list")


class TestInputSchemaRoundTrip:
    """
    GATE.  Every argument the protocol accepts is published, with a description.

    Derived from ``_NEGOTIATION_PROPERTIES`` rather than a literal list of
    names: a hardcoded assertion would have gone on passing while the schema
    omitted the fields entirely, which is how the defect reached production.
    """

    def test_class_dispatcher_publishes_every_negotiation_field(
        self, class_reg: ToolRegistry
    ) -> None:
        """The ``@mcp_dispatcher`` schema discloses the whole negotiation protocol."""
        props = _published_schema(class_reg, "tasks")["properties"]
        missing = sorted(NEGOTIATION_FIELDS - set(props))
        assert not missing, f"undisclosed negotiation fields on the dispatcher path: {missing}"

    def test_class_dispatcher_descriptions_are_present_and_non_trivial(
        self, class_reg: ToolRegistry
    ) -> None:
        """Disclosure means a usable description, not merely a declared key."""
        props = _published_schema(class_reg, "tasks")["properties"]
        for field in sorted(NEGOTIATION_FIELDS):
            description = props[field].get("description", "")
            assert len(description) > 20, f"{field!r} is published without a usable description"

    def test_placement_is_stated_in_the_schema_not_only_theenvelope(
        self, class_reg: ToolRegistry
    ) -> None:
        """
        The schema itself must say where the fields go.

        An agent reading ``tools/list`` and nothing else is the exact reader
        that got this wrong; telling it only in the probe envelope is too late.
        """
        props = _published_schema(class_reg, "tasks")["properties"]
        assert "top level" in props["continuation_token"]["description"].lower()
        assert "params" in props["params"]["description"].lower()
        assert "top-level" in props["params"]["description"].lower()

    def test_mode_enum_matches_what_the_envelope_advertises(self, class_reg: ToolRegistry) -> None:
        """The schema's ``mode`` enum and the envelope's ``available_modes`` agree."""
        props = _published_schema(class_reg, "tasks")["properties"]
        assert set(props["mode"]["enum"]) == set(ADVERTISED_MODES)

    def test_group_dispatcher_publishes_every_negotiation_field(
        self, group_reg: ToolRegistry
    ) -> None:
        """A group dispatcher must disclose the protocol it can put the agent into."""
        props = _published_schema(group_reg, "catalog")["properties"]
        missing = sorted(NEGOTIATION_FIELDS - set(props))
        assert not missing, f"undisclosed negotiation fields on the group path: {missing}"

    def test_group_dispatcher_can_issue_a_continuation_token(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        Evidence for the assertion above: the group path really does enter the protocol.

        This is what makes the missing disclosure a defect rather than a
        harmless omission — the tool hands out a token either way.
        """
        probe, _ = probe_then(
            rf, group_reg, "catalog", {"resource": "widget", "action": "list"}, None
        )
        assert "continuation_token" in probe
        assert probe["available_modes"] == ADVERTISED_MODES

    def test_group_schema_builder_discloses_at_the_builder_layer(self) -> None:
        """
        Pin the fix at its root, independent of registration wiring.

        Deliberately separate from the end-to-end assertion so that a fix
        applied at the wrong layer (patching list_tools rather than the
        builder) is still visible for what it is.
        """
        props = build_group_input_schema()["properties"]
        missing = sorted(NEGOTIATION_FIELDS - set(props))
        assert not missing, f"builder still omits: {missing}"
        # Disclosure must be additive — the group contract is unchanged.
        assert {"resource", "action", "params", "lite"} <= set(props)


# ---------------------------------------------------------------------------
# Item 2 — placement adversarial.  Two distinct cases, deliberately not merged.
# ---------------------------------------------------------------------------


class TestPlacementAdversarialTokenInParams:
    """
    GATE.  A ``continuation_token`` nested in ``params`` names its own fix.

    Before the disclosure fix this was forwarded to the underlying tool, where
    the filterset rejected it as an unknown filter field — accurate, and
    useless, because it never revealed that the key was real and merely
    misplaced.  Asserted on error TEXT: a bare status code would pass against
    the old filter error too.
    """

    def test_error_names_the_correct_placement(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """The error tells the agent where the field actually belongs."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        with pytest.raises(ToolInputError) as excinfo:
            class_reg.dispatch(
                request,
                "tasks",
                {"action": "list", "params": {"continuation_token": "abc123"}},
            )
        message = str(excinfo.value)
        assert "TOP LEVEL" in message
        assert "continuation_token" in message

    def test_error_is_distinguishable_from_an_unknown_filter_rejection(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        The two failures must not read alike.

        An agent that cannot tell "this key is not a filter" from "this key is
        real but misplaced" retries the same shape, which is the loop the
        original report described.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        with pytest.raises(ToolInputError) as excinfo:
            class_reg.dispatch(
                request,
                "tasks",
                {"action": "list", "params": {"continuation_token": "abc123"}},
            )
        message = str(excinfo.value)
        assert "filter" in message.lower()
        assert "sibling" in message.lower() or "top level" in message.lower()

    def test_error_states_the_cost_of_omitting_mode(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """The corrective error also says what happens if ``mode`` is left off."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        with pytest.raises(ToolInputError) as excinfo:
            class_reg.dispatch(
                request,
                "tasks",
                {"action": "list", "params": {"continuation_token": "abc123"}},
            )
        assert "complete dataset" in str(excinfo.value).lower()


class TestPlacementAdversarialModeInParams:
    """
    T7-F2 — FIXED.  ``mode`` inside ``params`` on a continuation call is refused.

    Distinct from the token case above and kept separate on purpose: it takes a
    different path.  ``views`` reads ``mode`` from top-level arguments only, so
    before the fix a misplaced ``mode`` was neither honoured nor rejected — the
    caller asked for ``summary`` and was served the complete dataset with no
    error and no warning, measured at 41x the cost they asked for.

    Note this is *not* the ``mode``-as-real-filter collision that kept ``mode``
    unreserved: on a continuation call the request short-circuits before
    dispatch, so ``params`` is never forwarded to any filterset and cannot
    collide with host data.  Detection is safe specifically in this context.
    """

    def test_misplaced_mode_is_refused_with_a_message_naming_the_placement(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The refusal must teach the fix, not merely decline."""
        _, served = probe_then(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "list"},
            {"params": {"mode": "summary"}},
        )
        assert "error" in served, "T7-F2 regressed — a misplaced mode is silent again"
        error = served["error"]
        assert "TOP LEVEL" in error
        assert "mode" in error
        # The caller must be told what the silence would have cost them.
        assert "COMPLETE" in error

    def test_misplaced_mode_should_be_honoured_or_refused_not_ignored(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """A bounded mode asked for in the wrong place must not silently cost the full one."""
        _, served = probe_then(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "list"},
            {"params": {"mode": "summary"}},
        )
        assert result_bytes(served) < result_bytes(payload())


class TestPlacementAdversarialModeWithoutToken:
    """
    LOCK-IN of a ruled-on trade-off.  ``mode`` at top level with no token.

    This is the case that produced the original confusion, and it is now
    **intended**: ``mode`` is a genuine model field on at least one real host
    application, so the dispatcher must keep passing it through to the action
    rather than claiming it as protocol.  The mitigation is disclosure, not
    rejection — so this asserts the *quality of what comes back*, not an error.

    Do not "fix" this into an error without re-opening the collision analysis.
    """

    def test_mode_without_a_token_is_passed_through_to_the_action(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """A bare ``mode`` stays an action parameter — it is not claimed as protocol."""
        cache = CacheStub()
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
            patch("frisian_mcp.views.django_cache", cache),
            override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=10**9),
        ):
            served = tool_result(
                call(
                    rf,
                    "catalog",
                    {"resource": "widget", "action": "list", "mode": "summary"},
                )
            )
        assert served["received"] == {"mode": "summary"}, (
            "'mode' without a token must reach the action as an ordinary parameter;"
            " reserving it would break hosts where 'mode' is a real field"
        )

    def test_the_response_still_discloses_correct_placement(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        GATE.  Since we cannot reject it, the response must at least teach the fix.

        This is the whole mitigation for this case: the agent that guessed wrong
        gets an envelope that tells it where the field goes and what the
        unbounded response costs.
        """
        probe, _ = probe_then(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "list", "mode": "summary"},
            None,
        )
        usage = probe["usage"]
        assert "TOP LEVEL" in usage
        assert "params" in usage
        assert str(probe["total_size"]) in usage, "the cost of omitting 'mode' must be concrete"


# ---------------------------------------------------------------------------
# Item 4 — flat-form fallback.  Highest-risk regression in the disclosure fix.
# ---------------------------------------------------------------------------


class TestFlatFormFallbackPreserved:
    """
    LOCK-IN, and the one this suite treats as highest risk.

    The flat form exists for schema-driven agents that cannot nest arguments.
    The disclosure fix narrows the sweep, so the failure mode it risks is a
    silent client incompatibility — strictly worse than the documented
    annoyance it replaced.  Coverage here is deliberately broader than one
    happy path: multiple keys, non-string values, and both dispatcher kinds.
    """

    def test_class_dispatcher_sweeps_a_single_flat_param(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """``{action, key: val}`` still routes ``key`` into params."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = class_reg.dispatch(request, "tasks", {"action": "list", "status": "active"})
        assert result["received"] == {"status": "active"}

    def test_class_dispatcher_sweeps_multiple_mixed_type_params(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """Several flat params of different types all survive the sweep."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = class_reg.dispatch(
            request,
            "tasks",
            {"action": "list", "status": "active", "limit": 5, "archived": False},
        )
        assert result["received"] == {"status": "active", "limit": 5, "archived": False}

    def test_colliding_negotiation_names_still_reach_the_action(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        ``mode``/``page``/``page_size`` are host data, not reserved words.

        Reserving them would have broken a real host's interface filter and
        every host using the default page-number paginator.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = class_reg.dispatch(
            request,
            "tasks",
            {"action": "list", "params": {"mode": "access", "page": 2, "page_size": 50}},
        )
        assert result["received"] == {"mode": "access", "page": 2, "page_size": 50}

    def test_nested_collision_is_unaffected_by_schema_validation(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        The nested form escapes the regression below, which is why it was missed.

        ``dispatch`` validates the TOP-LEVEL arguments against the tool schema;
        ``params`` is an ``additionalProperties: true`` object, so anything
        inside it is unconstrained.  Only the flat form is exposed.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = class_reg.dispatch(
            request, "tasks", {"action": "list", "params": {"mode": "access"}}
        )
        assert result["received"] == {"mode": "access"}

    def test_colliding_names_survive_the_flat_form_too(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """The collision carve-out must hold on the flat path, not just the nested one."""
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        result = class_reg.dispatch(request, "tasks", {"action": "list", "mode": "access"})
        assert result["received"] == {"mode": "access"}

    def test_only_the_unambiguous_protocol_key_is_withheld(
        self, class_reg: ToolRegistry, rf: RequestFactory
    ) -> None:
        """
        Exactly one field is excluded from the flat sweep — no more.

        Derived from the negotiation field set so that reserving a further
        field in future fails here rather than silently on a host.
        """
        request = rf.post("/mcp/", content_type="application/json")
        request.auth = None
        flat: dict[str, Any] = {"action": "list"}
        flat.update({field: f"value-of-{field}" for field in sorted(NEGOTIATION_FIELDS)})
        flat.pop("continuation_token")
        result = class_reg.dispatch(request, "tasks", flat)
        assert set(result["received"]) == NEGOTIATION_FIELDS - {"continuation_token"}

    def test_group_path_is_currently_immune_to_that_regression(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        CANARY.  The group path still accepts a host-meaningful flat ``mode``.

        It is immune only because its schema omits the negotiation fields —
        i.e. the disclosure gap (T7-F1) is what is shielding it.  Closing F1 by
        merging the same properties into the group schema would spread the F5
        regression to every grouped host.  If this test starts failing, F1 was
        fixed without fixing F5 first.
        """
        cache = CacheStub()
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
            patch("frisian_mcp.views.django_cache", cache),
            override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=10**9),
        ):
            served = tool_result(
                call(
                    rf,
                    "catalog",
                    {"resource": "widget", "action": "list", "mode": "access"},
                )
            )
        assert served["received"] == {"mode": "access"}

    def test_group_dispatcher_flat_form_still_routes(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The group path accepts the flat form too, and must keep doing so."""
        cache = CacheStub()
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
            patch("frisian_mcp.views.django_cache", cache),
            override_settings(FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=10**9),
        ):
            served = tool_result(
                call(
                    rf,
                    "catalog",
                    {"resource": "widget", "action": "list", "status": "active"},
                )
            )
        assert served["received"] == {"status": "active"}


# ---------------------------------------------------------------------------
# Item 5 — the ruled default.  A lock-in, not a gate.
# ---------------------------------------------------------------------------


class TestBareTokenDefaultsToFull:
    """
    LOCK-IN of an explicit human ruling: a token with no ``mode`` returns ``full``.

    This is a deliberate compatibility choice, not an oversight, and it is the
    expensive branch — so it is exactly the behaviour a future agent is most
    likely to "fix" on sight.  These tests exist to make that a conversation
    rather than a silent change.
    """

    def test_bare_token_returns_the_complete_dataset(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """No ``mode`` means the whole thing, byte for byte."""
        _, served = probe_then(
            rf, group_reg, "catalog", {"resource": "widget", "action": "list"}, {}
        )
        assert served == payload()

    def test_unknown_mode_also_falls_back_to_full(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """An unrecognised mode degrades to ``full`` rather than erroring."""
        _, served = probe_then(
            rf,
            group_reg,
            "catalog",
            {"resource": "widget", "action": "list"},
            {"mode": "nonsense-mode"},
        )
        assert served == payload()

    def test_the_cost_of_that_default_is_disclosed_up_front(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        GATE.  If the default is the expensive one, the probe must price it.

        The ruling is defensible only because the agent is told what it costs
        before it chooses; this asserts the disclosure that makes it so.
        """
        probe, _ = probe_then(
            rf, group_reg, "catalog", {"resource": "widget", "action": "list"}, None
        )
        assert "COMPLETE dataset" in probe["usage"]
        assert str(probe["total_size"]) in probe["usage"]


# ---------------------------------------------------------------------------
# Item 7 — regressions
# ---------------------------------------------------------------------------


class TestNotFoundShapes:
    """
    LOCK-IN.  Absence answers keep their shape.

    An unknown *tool* and an unknown *resource inside a group* are answered at
    different layers and therefore in different envelopes.  That is by design,
    but it is worth pinning: an agent parses these, and a silent move between
    the two shapes changes how a client detects "not found".
    """

    def test_unknown_tool_is_a_jsonrpc_method_not_found(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """A nonexistent tool answers with the JSON-RPC error object."""
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
        ):
            body = envelope(call(rf, "no_such_tool", {}))
        assert body["error"]["code"] == -32601
        assert "no_such_tool" in body["error"]["data"]

    def test_unknown_resource_in_a_group_is_a_tool_level_error(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """An unknown resource answers inside the tool result, flagged isError."""
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
        ):
            response = call(rf, "catalog", {"resource": "gadget", "action": "create", "params": {}})
            body = envelope(response)
            payload = tool_result(response)
        assert body["result"]["isError"] is True
        assert payload["status_code"] == 404
        assert "gadget_create" in payload["error"]

    def test_both_absence_answers_carry_a_machine_readable_marker(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        Whichever layer answers, the client can detect absence without parsing prose.

        This is the invariant worth holding; identical envelopes across the two
        layers is not, since they are raised at genuinely different levels.
        """
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
        ):
            unknown_tool = envelope(call(rf, "no_such_tool", {}))
            unknown_resource = call(
                rf, "catalog", {"resource": "gadget", "action": "create", "params": {}}
            )
        assert unknown_tool["error"]["code"] == -32601
        assert tool_result(unknown_resource)["status_code"] == 404

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "OPEN DEFECT, tracked separately as T8: when the RESOURCE is correct and the"
            " ACTION is wrong, the did-you-mean suggestion names the resource the caller"
            " already got right and says nothing about the action that was wrong."
            " Recorded here because this suite exercises the path; the fix belongs to T8."
        ),
    )
    def test_unknown_action_suggestion_addresses_the_action_not_the_resource(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The suggestion should point at the axis that was actually wrong."""
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
        ):
            payload = tool_result(
                call(rf, "catalog", {"resource": "widget", "action": "frobnicate", "params": {}})
            )
        assert "resource='widget'" not in payload["error"]

    def test_unknown_action_currently_suggests_the_resource(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """Pin T8 as measured so the xfail above is evidenced, not asserted."""
        with (
            patch("frisian_mcp.views.tool_registry", group_reg),
            patch("frisian_mcp.registry.tool_registry", group_reg),
        ):
            payload = tool_result(
                call(rf, "catalog", {"resource": "widget", "action": "frobnicate", "params": {}})
            )
        assert "resource='widget'" in payload["error"]


class TestUsageIsHonestOnEveryPath:
    """
    LOCK-IN.  ``_usage`` reports what was actually returned, not what was computed.

    The negotiation protocol makes this easy to get wrong: the probe path
    computes a large result and returns a small envelope.  Reporting the
    former would make the very feature that saves context look like it costs
    it, and would misinform any agent budgeting on these numbers.
    """

    def _usage_for(
        self, rf: RequestFactory, reg: ToolRegistry, arguments: dict[str, Any], threshold: int
    ) -> tuple[dict[str, Any], Any]:
        cache = CacheStub()
        with (
            patch("frisian_mcp.views.tool_registry", reg),
            patch("frisian_mcp.registry.tool_registry", reg),
            patch("frisian_mcp.views.django_cache", cache),
            override_settings(
                FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD=threshold,
                FRISIAN_MCP_USAGE_REPORTING=True,
            ),
        ):
            response = call(rf, "catalog", arguments)
        body = envelope(response)
        return body["result"]["_usage"], tool_result(response)

    def test_probe_path_reports_the_envelope_not_the_cached_payload(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """The probe returns a small envelope, so it must report a small cost."""
        usage, probe = self._usage_for(rf, group_reg, {"resource": "widget", "action": "list"}, 500)
        full_tokens = result_bytes(payload()) / 4
        assert usage["result_tokens"] < full_tokens / 10, (
            "probe reported the cost of the payload it cached rather than the envelope"
            " it returned"
        )
        assert probe["total_size"] == result_bytes(payload())

    def test_usage_totals_are_internally_consistent(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """``total_tokens`` equals the parts it claims to sum."""
        usage, _ = self._usage_for(rf, group_reg, {"resource": "widget", "action": "list"}, 500)
        assert usage["total_tokens"] == (
            usage["schema_tokens"] + usage["request_tokens"] + usage["result_tokens"]
        )

    def test_full_passthrough_reports_more_than_the_probe(
        self, rf: RequestFactory, group_reg: ToolRegistry
    ) -> None:
        """
        With the backstop off, the same call returns everything — and says so.

        The comparison is the assertion: identical numbers across a probe and a
        full passthrough would mean the counter is not measuring the response.
        """
        probe_usage, _ = self._usage_for(
            rf, group_reg, {"resource": "widget", "action": "list"}, 500
        )
        full_usage, _ = self._usage_for(
            rf, group_reg, {"resource": "widget", "action": "list"}, 10**9
        )
        assert full_usage["result_tokens"] > probe_usage["result_tokens"] * 10
