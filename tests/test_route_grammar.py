"""Tests for :mod:`frisian_mcp.route_grammar`.

Coverage:
- ``parse_entry`` FATAL conditions and happy-path parse kinds.
- ``parse_lists`` top-level entrypoint (None handling, bare-string rejection,
  route_name propagation, order preservation).
- ``RouteMatcher.select`` on group and flat surfaces (wildcard, group,
  qualified, bare, deny carve-out, partial glob).
- Group-shadows-tool resolution order.
- ``RouteMatcher.audit`` SOFT findings (partial glob, unknown group,
  empty match, ordering, route_name override, never raises).
- ``RouteMatcher.audit`` LOUD grading of an inert ``deny_list`` entry
  (``CODE_INERT_DENY``): config drift leaks and is LOUD, an absent optional
  component and a never-allowed target stay SOFT, and a cross-group name
  collision is LOUD but attributes the survivor to the unrelated group.
- ``ToolSurface.build`` normalization and immutability.
- Custom tool separator.
"""

from __future__ import annotations

import pytest

from frisian_mcp.route_grammar import (
    CODE_DENY_WILDCARD,
    CODE_DOUBLE_UNDERSCORE,
    CODE_EMPTY_ENTRY,
    CODE_EMPTY_MATCH,
    CODE_INERT_DENY,
    CODE_NON_STRING_ENTRY,
    CODE_PARTIAL_GLOB,
    CODE_TOO_MANY_SEGMENTS,
    CODE_UNKNOWN_GROUP,
    KIND_BARE,
    KIND_LITERAL_UNMATCHABLE,
    KIND_QUALIFIED,
    KIND_WILDCARD,
    SEVERITY_LOUD,
    SEVERITY_SOFT,
    GrammarError,
    ToolSurface,
    parse_entry,
    parse_lists,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def group_surface() -> ToolSurface:
    """Return a surface with two dispatch groups and one un-grouped flat tool."""
    return ToolSurface.build(
        tool_names=[
            "catalog",  # group dispatcher
            "product_list",
            "product_retrieve",
            "product_create",
            "category_list",
            "category_retrieve",
            "orders",  # group dispatcher
            "order_list",
            "order_create",
            "line_item_list",
            "healthcheck",  # flat, un-grouped
        ],
        group_members={
            "catalog": {
                "product_list",
                "product_retrieve",
                "product_create",
                "category_list",
                "category_retrieve",
            },
            "orders": {"order_list", "order_create", "line_item_list"},
        },
        tool_separator="_",
    )


@pytest.fixture()
def drift_surfaces() -> dict[str, ToolSurface]:
    """Four surfaces that differ only in where the ``product_*`` tools live.

    A single ``deny_list`` of ``["catalog:product"]`` is evaluated against each,
    isolating the variable that decides whether an inert deny leaked:

    ``grouped``
        Healthy. ``catalog`` is registered and bundles the ``product_*`` tools;
        the carve-out binds.
    ``drifted``
        ``catalog`` was renamed or removed. Its former members are still
        registered, now flat. The carve-out silently removes nothing.
    ``absent``
        An optional host component is not installed: the group *and* its tools
        are gone. The carve-out removes nothing and leaks nothing.
    ``collision``
        ``catalog`` is gone, but an unrelated registered group reuses the
        ``product`` resource name. Indistinguishable from ``drifted`` except
        through the finding's message.
    """
    grouped_members = {
        "catalog": {"product_list", "product_retrieve", "product_create", "category_list"},
        "orders": {"order_list", "order_create"},
    }
    orders_only = {"orders": {"order_list", "order_create"}}
    ungrouped = [
        "product_list",
        "product_retrieve",
        "product_create",
        "category_list",
        "orders",
        "order_list",
        "order_create",
        "healthcheck",
    ]
    return {
        "grouped": ToolSurface.build(
            tool_names=["catalog", *ungrouped],
            group_members=grouped_members,
        ),
        "drifted": ToolSurface.build(tool_names=ungrouped, group_members=orders_only),
        "absent": ToolSurface.build(
            tool_names=["orders", "order_list", "order_create", "healthcheck"],
            group_members=orders_only,
        ),
        "collision": ToolSurface.build(
            tool_names=["archive", "product_list", "healthcheck"],
            group_members={"archive": {"product_list"}},
        ),
    }


@pytest.fixture()
def flat_surface() -> ToolSurface:
    """Return a greenfield flat surface with no dispatch groups."""
    return ToolSurface.build(
        tool_names=[
            "device_list",
            "device_retrieve",
            "device_create",
            "healthcheck",
            "ping",
        ],
        group_members={},
        tool_separator="_",
    )


# ---------------------------------------------------------------------------
# parse_entry — FATAL conditions
# ---------------------------------------------------------------------------


class TestParseEntryFatal:
    """Each FATAL grammar condition raises :class:`GrammarError` with a stable code."""

    def test_non_string_int(self) -> None:
        """An integer entry raises ``CODE_NON_STRING_ENTRY``."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry(42, list_name="allow_list")
        assert excinfo.value.code == CODE_NON_STRING_ENTRY

    def test_non_string_none(self) -> None:
        """A ``None`` entry raises ``CODE_NON_STRING_ENTRY``."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry(None, list_name="allow_list")
        assert excinfo.value.code == CODE_NON_STRING_ENTRY

    def test_empty_string(self) -> None:
        """An empty string raises ``CODE_EMPTY_ENTRY``."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("", list_name="allow_list")
        assert excinfo.value.code == CODE_EMPTY_ENTRY

    def test_whitespace_string(self) -> None:
        """Whitespace-only entries collapse to empty and raise ``CODE_EMPTY_ENTRY``."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("   ", list_name="allow_list")
        assert excinfo.value.code == CODE_EMPTY_ENTRY

    def test_double_underscore_bare(self) -> None:
        """Bare entries containing ``__`` raise ``CODE_DOUBLE_UNDERSCORE``."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("foo__bar", list_name="allow_list")
        assert excinfo.value.code == CODE_DOUBLE_UNDERSCORE

    def test_double_underscore_qualified(self) -> None:
        """Qualified entries with ``__`` in the resource segment also raise."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("catalog:a__b", list_name="allow_list")
        assert excinfo.value.code == CODE_DOUBLE_UNDERSCORE

    def test_deny_wildcard(self) -> None:
        """``'*'`` in ``deny_list`` raises ``CODE_DENY_WILDCARD``."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("*", list_name="deny_list")
        assert excinfo.value.code == CODE_DENY_WILDCARD

    def test_too_many_segments(self) -> None:
        """Three-segment entries raise ``CODE_TOO_MANY_SEGMENTS``.

        Per-action route filtering is out of scope for v1.1.
        """
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("group:resource:action", list_name="allow_list")
        assert excinfo.value.code == CODE_TOO_MANY_SEGMENTS

    def test_empty_group_segment(self) -> None:
        """An entry like ``':foo'`` has an empty group segment and raises."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry(":foo", list_name="allow_list")
        assert excinfo.value.code == CODE_EMPTY_ENTRY

    def test_empty_resource_segment(self) -> None:
        """An entry like ``'foo:'`` has an empty resource segment and raises."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("foo:", list_name="allow_list")
        assert excinfo.value.code == CODE_EMPTY_ENTRY

    def test_error_carries_context(self) -> None:
        """Raised errors expose ``entry``, ``list_name``, and ``route_name`` fields."""
        with pytest.raises(GrammarError) as excinfo:
            parse_entry("bad__entry", list_name="allow_list", route_name="admin")
        exc = excinfo.value
        assert exc.entry == "bad__entry"
        assert exc.list_name == "allow_list"
        assert exc.route_name == "admin"
        assert "admin" in str(exc)


# ---------------------------------------------------------------------------
# parse_entry — happy path kinds
# ---------------------------------------------------------------------------


class TestParseEntryKinds:
    """String shape decides ``ParsedEntry.kind`` at parse time."""

    def test_wildcard_in_allow(self) -> None:
        """``'*'`` in ``allow_list`` parses as a wildcard entry with empty label."""
        entry = parse_entry("*", list_name="allow_list")
        assert entry.kind == KIND_WILDCARD
        assert entry.label == ""

    def test_bare(self) -> None:
        """A plain label parses as bare with no resource."""
        entry = parse_entry("catalog", list_name="allow_list")
        assert entry.kind == KIND_BARE
        assert entry.label == "catalog"
        assert entry.resource is None

    def test_bare_strips_whitespace(self) -> None:
        """Surrounding whitespace is stripped before parsing."""
        entry = parse_entry("  catalog  ", list_name="allow_list")
        assert entry.kind == KIND_BARE
        assert entry.label == "catalog"

    def test_qualified(self) -> None:
        """``group:resource`` parses as qualified with populated ``resource``."""
        entry = parse_entry("catalog:product", list_name="allow_list")
        assert entry.kind == KIND_QUALIFIED
        assert entry.label == "catalog"
        assert entry.resource == "product"

    def test_group_star_normalizes_to_bare(self) -> None:
        """``group:*`` silently normalizes to bare ``group`` (no warning)."""
        entry = parse_entry("catalog:*", list_name="allow_list")
        assert entry.kind == KIND_BARE
        assert entry.label == "catalog"
        assert entry.resource is None

    @pytest.mark.parametrize("raw", ["catalog:a*", "catalog_*", "a*", "*_foo", "f*o", "*:foo"])
    def test_partial_globs_are_literal_unmatchable(self, raw: str) -> None:
        """Any glob beyond bare ``*`` and ``group:*`` is a literal-unmatchable entry."""
        entry = parse_entry(raw, list_name="allow_list")
        assert entry.kind == KIND_LITERAL_UNMATCHABLE
        assert entry.raw == raw


# ---------------------------------------------------------------------------
# parse_lists — top-level entrypoint
# ---------------------------------------------------------------------------


class TestParseLists:
    """Top-level list parser: coercion, ordering, and error propagation."""

    def test_none_lists_default_to_empty(self) -> None:
        """``None`` lists are treated as empty — no entries, no wildcard."""
        matcher = parse_lists(None, None)
        assert matcher.allow == ()
        assert matcher.deny == ()
        assert matcher.allow_wildcard is False

    def test_bare_string_is_fatal(self) -> None:
        """A bare string list value is FATAL — the parser refuses to iterate characters."""
        with pytest.raises(GrammarError) as excinfo:
            parse_lists("catalog", [])
        assert excinfo.value.code == CODE_NON_STRING_ENTRY

    def test_non_iterable_is_fatal(self) -> None:
        """A non-iterable list value is FATAL."""
        with pytest.raises(GrammarError) as excinfo:
            parse_lists(42, [])
        assert excinfo.value.code == CODE_NON_STRING_ENTRY

    def test_allow_wildcard_flag(self) -> None:
        """``allow_wildcard`` reflects presence of a wildcard entry in allow."""
        matcher = parse_lists(["*"], [])
        assert matcher.allow_wildcard is True

    def test_route_name_propagates_to_errors(self) -> None:
        """A route_name passed to ``parse_lists`` surfaces on raised errors."""
        with pytest.raises(GrammarError) as excinfo:
            parse_lists(["foo__bar"], [], route_name="admin")
        assert excinfo.value.route_name == "admin"

    def test_deny_wildcard_rejected_from_lists(self) -> None:
        """A ``'*'`` entry in deny_list raises through the top-level parser."""
        with pytest.raises(GrammarError) as excinfo:
            parse_lists(["*"], ["*"])
        assert excinfo.value.code == CODE_DENY_WILDCARD

    def test_preserves_order(self) -> None:
        """Parsed entries retain the operator's declared order."""
        matcher = parse_lists(["catalog", "orders", "healthcheck"], ["orders"])
        assert [e.label for e in matcher.allow] == ["catalog", "orders", "healthcheck"]
        assert [e.label for e in matcher.deny] == ["orders"]


# ---------------------------------------------------------------------------
# RouteMatcher.select — group surface
# ---------------------------------------------------------------------------


class TestSelectGroupSurface:
    """Selection semantics against a surface with configured dispatch groups."""

    def test_deny_all_baseline(self, group_surface: ToolSurface) -> None:
        """Empty allow_list matches nothing — deny-all baseline."""
        matcher = parse_lists([], [])
        assert matcher.select(group_surface) == frozenset()

    def test_wildcard_matches_everything(self, group_surface: ToolSurface) -> None:
        """``'*'`` in allow_list matches every tool on the surface."""
        matcher = parse_lists(["*"], [])
        assert matcher.select(group_surface) == group_surface.tool_names

    def test_bare_group_expands(self, group_surface: ToolSurface) -> None:
        """A bare group name expands to dispatcher tool + all group members."""
        matcher = parse_lists(["catalog"], [])
        selected = matcher.select(group_surface)
        assert selected == frozenset(
            {
                "catalog",  # dispatcher itself
                "product_list",
                "product_retrieve",
                "product_create",
                "category_list",
                "category_retrieve",
            }
        )

    def test_qualified_resource_matches_prefix(self, group_surface: ToolSurface) -> None:
        """``group:resource`` matches every group member with ``resource{sep}...`` names."""
        matcher = parse_lists(["catalog:product"], [])
        selected = matcher.select(group_surface)
        assert selected == frozenset({"product_list", "product_retrieve", "product_create"})

    def test_group_star_normalization_equals_bare(self, group_surface: ToolSurface) -> None:
        """``group:*`` and bare ``group`` produce identical selections."""
        star = parse_lists(["catalog:*"], []).select(group_surface)
        bare = parse_lists(["catalog"], []).select(group_surface)
        assert star == bare

    def test_deny_carve_out_from_allow(self, group_surface: ToolSurface) -> None:
        """Deny entries carve out from the allow union (deny evaluated after allow)."""
        matcher = parse_lists(["catalog"], ["catalog:product"])
        selected = matcher.select(group_surface)
        # catalog minus product-slice.
        assert selected == frozenset({"catalog", "category_list", "category_retrieve"})

    def test_deny_after_wildcard(self, group_surface: ToolSurface) -> None:
        """Deny works against a wildcard allow: everything minus the denied group."""
        matcher = parse_lists(["*"], ["orders"])
        selected = matcher.select(group_surface)
        # Everything except the orders group (dispatcher + members).
        expected = group_surface.tool_names - {
            "orders",
            "order_list",
            "order_create",
            "line_item_list",
        }
        assert selected == expected

    def test_partial_glob_matches_nothing(self, group_surface: ToolSurface) -> None:
        """Literal-unmatchable entries never expand at select time."""
        matcher = parse_lists(["catalog:pr*"], [])
        assert matcher.select(group_surface) == frozenset()

    def test_multiple_allow_entries_union(self, group_surface: ToolSurface) -> None:
        """Multiple allow entries union to produce the visible set."""
        matcher = parse_lists(["catalog:product", "healthcheck"], [])
        selected = matcher.select(group_surface)
        assert selected == frozenset(
            {"product_list", "product_retrieve", "product_create", "healthcheck"}
        )


# ---------------------------------------------------------------------------
# RouteMatcher.select — flat surface
# ---------------------------------------------------------------------------


class TestSelectFlatSurface:
    """Selection on a greenfield flat surface uses the same grammar."""

    def test_wildcard_on_flat_surface(self, flat_surface: ToolSurface) -> None:
        """Wildcard behaves identically on flat surfaces."""
        matcher = parse_lists(["*"], [])
        assert matcher.select(flat_surface) == flat_surface.tool_names

    def test_bare_becomes_prefix_expansion(self, flat_surface: ToolSurface) -> None:
        """Bare labels fall through to resource-prefix expansion when no group matches."""
        matcher = parse_lists(["device"], [])
        selected = matcher.select(flat_surface)
        assert selected == frozenset({"device_list", "device_retrieve", "device_create"})

    def test_exact_tool_name(self, flat_surface: ToolSurface) -> None:
        """Bare labels also match tools whose name is exactly the label."""
        matcher = parse_lists(["healthcheck"], [])
        selected = matcher.select(flat_surface)
        assert selected == frozenset({"healthcheck"})

    def test_qualified_on_flat_surface_matches_nothing(self, flat_surface: ToolSurface) -> None:
        """Qualified entries never match on a group-less surface."""
        matcher = parse_lists(["device:list"], [])
        assert matcher.select(flat_surface) == frozenset()


# ---------------------------------------------------------------------------
# Group-shadows-tool rule
# ---------------------------------------------------------------------------


class TestGroupShadowsTool:
    """Bare-entry resolution binds to group first, shadowing any same-named flat tool."""

    def test_bare_binds_to_group_first(self) -> None:
        """A flat tool sharing a name with a group is unreachable by its bare name."""
        # A flat tool literally named 'catalog' AND a group named 'catalog'.
        surface = ToolSurface.build(
            tool_names={"catalog", "product_list", "product_retrieve"},
            group_members={"catalog": {"product_list", "product_retrieve"}},
        )
        matcher = parse_lists(["catalog"], [])
        selected = matcher.select(surface)
        # The bare label binds to the group; the dispatcher tool 'catalog' is
        # included because it exists as a tool; group members are pulled in.
        assert selected == frozenset({"catalog", "product_list", "product_retrieve"})


# ---------------------------------------------------------------------------
# RouteMatcher.audit — SOFT findings
# ---------------------------------------------------------------------------


class TestAudit:
    """Audit-time SOFT findings for entries that resolve to no tools."""

    def test_clean_config_no_findings(self, group_surface: ToolSurface) -> None:
        """A config where every entry resolves cleanly produces zero findings."""
        matcher = parse_lists(["catalog", "orders:order"], ["catalog:product"])
        assert matcher.audit(group_surface) == ()

    def test_partial_glob_soft_finding(self, group_surface: ToolSurface) -> None:
        """Partial globs surface as ``CODE_PARTIAL_GLOB`` findings."""
        matcher = parse_lists(["catalog:pr*"], [], route_name="admin")
        findings = matcher.audit(group_surface)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == SEVERITY_SOFT
        assert f.code == CODE_PARTIAL_GLOB
        assert f.entry == "catalog:pr*"
        assert f.list_name == "allow_list"
        assert f.route_name == "admin"

    def test_unknown_group_soft_finding(self, group_surface: ToolSurface) -> None:
        """A qualified entry with an unknown group emits ``CODE_UNKNOWN_GROUP``."""
        matcher = parse_lists(["ghost:foo"], [])
        findings = matcher.audit(group_surface)
        assert len(findings) == 1
        assert findings[0].code == CODE_UNKNOWN_GROUP

    def test_empty_match_soft_finding(self, group_surface: ToolSurface) -> None:
        """A bare label that matches no tool or group prefix emits ``CODE_EMPTY_MATCH``."""
        matcher = parse_lists(["nonexistent"], [])
        findings = matcher.audit(group_surface)
        assert len(findings) == 1
        assert findings[0].code == CODE_EMPTY_MATCH
        assert findings[0].entry == "nonexistent"

    def test_wildcard_produces_no_finding(self, group_surface: ToolSurface) -> None:
        """A wildcard entry is exempt from audit findings."""
        matcher = parse_lists(["*"], [])
        assert matcher.audit(group_surface) == ()

    def test_findings_are_ordered_allow_then_deny(self, group_surface: ToolSurface) -> None:
        """Findings are ordered ``allow_list`` first, then ``deny_list``."""
        matcher = parse_lists(["nope_a"], ["nope_d"])
        findings = matcher.audit(group_surface)
        assert [f.list_name for f in findings] == ["allow_list", "deny_list"]

    def test_audit_route_name_override(self, group_surface: ToolSurface) -> None:
        """``audit(route_name=...)`` overrides the matcher's stored ``route_name``."""
        matcher = parse_lists(["nope"], [], route_name="from_parse")
        findings = matcher.audit(group_surface, route_name="from_audit")
        assert findings[0].route_name == "from_audit"

    def test_audit_never_raises_on_matched_nothing(self, group_surface: ToolSurface) -> None:
        """Watch item: entries matching nothing are SOFT — never a boot failure."""
        matcher = parse_lists(["nonexistent", "another_ghost"], [])
        findings = matcher.audit(group_surface)
        assert len(findings) == 2
        assert all(f.severity == SEVERITY_SOFT for f in findings)


# ---------------------------------------------------------------------------
# RouteMatcher.audit — inert deny_list entries (LOUD)
# ---------------------------------------------------------------------------


class TestInertDenySeverity:
    """An inert ``deny_list`` entry is fail-open; an inert ``allow_list`` entry is not.

    The two directions were graded identically before this class existed.  Every
    test here pins the asymmetry: severity is a function of the list the entry
    came from *and* the route's net surface, never of the finding code alone.
    """

    def test_healthy_grouped_surface_deny_works_and_is_silent(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """Case A: the group is registered, the carve-out bites, the audit is clean."""
        matcher = parse_lists(["*"], ["catalog:product"])
        surface = drift_surfaces["grouped"]
        net = matcher.select(surface)
        assert not {n for n in net if n.startswith("product")}
        assert matcher.audit(surface) == ()

    def test_config_drift_leaks_and_is_loud(self, drift_surfaces: dict[str, ToolSurface]) -> None:
        """Case B: same deny_list, group renamed away — tools survive, so LOUD ``W113``."""
        matcher = parse_lists(["*"], ["catalog:product"], route_name="admin")
        surface = drift_surfaces["drifted"]
        net = matcher.select(surface)
        assert "product_list" in net  # the carve-out removed nothing

        findings = matcher.audit(surface)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == SEVERITY_LOUD
        assert f.code == CODE_INERT_DENY
        assert f.list_name == "deny_list"
        assert f.entry == "catalog:product"
        assert f.route_name == "admin"

    def test_fail_closed_control_stays_soft(self, drift_surfaces: dict[str, ToolSurface]) -> None:
        """Case C: an ``allow_list`` typo exposes nothing — fail-closed, so SOFT."""
        matcher = parse_lists(["catlog"], [])
        findings = matcher.audit(drift_surfaces["grouped"])
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_SOFT
        assert findings[0].code == CODE_EMPTY_MATCH
        assert findings[0].list_name == "allow_list"

    def test_absent_optional_component_stays_soft(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """Group *and* tools absent: the deny removed nothing and leaked nothing."""
        matcher = parse_lists(["*"], ["catalog:product"])
        findings = matcher.audit(drift_surfaces["absent"])
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_SOFT
        assert findings[0].code == CODE_UNKNOWN_GROUP

    def test_never_allowed_target_stays_soft(self, drift_surfaces: dict[str, ToolSurface]) -> None:
        """The probe is against the **net** set: an unexposed target cannot leak.

        ``product_*`` exists on the surface but ``allow_list`` never granted it,
        so the inert deny is harmless even though the group is gone.
        """
        matcher = parse_lists(["orders"], ["catalog:product"])
        surface = drift_surfaces["drifted"]
        assert "product_list" in surface.tool_names  # present in the registry...
        assert "product_list" not in matcher.select(surface)  # ...but never exposed

        findings = matcher.audit(surface)
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_SOFT

    def test_cross_group_collision_is_loud_but_names_the_unrelated_group(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """The negative twin: a coincidental name reuse fires ``W113`` — recoverably.

        This is why ``W113`` is LOUD and never FATAL.  The named group is absent,
        so its membership is unknowable; an unrelated registered group reusing the
        resource name is indistinguishable *except* through the message, which
        must attribute the survivor to that group so the operator can dismiss it.
        """
        matcher = parse_lists(["*"], ["catalog:product"])
        findings = matcher.audit(drift_surfaces["collision"])
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_LOUD
        assert findings[0].code == CODE_INERT_DENY
        assert "in group 'archive'" in findings[0].message
        assert "'product_list'" in findings[0].message

    def test_message_names_survivors_and_locations(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """A LOUD an operator cannot act on is one they learn to filter."""
        matcher = parse_lists(["*"], ["catalog:product"])
        message = matcher.audit(drift_surfaces["drifted"])[0].message
        for survivor in ("product_create", "product_list", "product_retrieve"):
            assert f"{survivor!r}" in message
        assert "(flat)" in message
        assert "fail-open" in message

    def test_survivor_list_is_capped_and_the_cap_is_explicit(self) -> None:
        """Truncation is reported, never silent."""
        surface = ToolSurface.build(
            tool_names=[f"product_{n}" for n in ("a", "b", "c", "d", "e", "f", "g")]
        )
        matcher = parse_lists(["*"], ["catalog:product"])
        findings = matcher.audit(surface)
        assert findings[0].severity == SEVERITY_LOUD
        assert "7 tool(s)" in findings[0].message
        assert "+2 more" in findings[0].message

    def test_loud_supersedes_unknown_group_code(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """``W113`` replaces ``W112``: the absent-group branch used to return early."""
        matcher = parse_lists(["*"], ["catalog:product"])
        findings = matcher.audit(drift_surfaces["drifted"])
        assert findings[0].code == CODE_INERT_DENY
        assert findings[0].code != CODE_UNKNOWN_GROUP

    def test_loud_supersedes_empty_match_code(self) -> None:
        """``W113`` replaces ``W111``: group registered, resource moved out of it."""
        surface = ToolSurface.build(
            tool_names=["catalog", "category_list", "product_list"],
            group_members={"catalog": ["category_list"]},
        )
        matcher = parse_lists(["*"], ["catalog:product"])
        findings = matcher.audit(surface)
        assert findings[0].severity == SEVERITY_LOUD
        assert findings[0].code == CODE_INERT_DENY
        assert "'product_list' (flat)" in findings[0].message

    def test_partial_glob_in_deny_list_is_loud_when_prefix_survives(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """A partial glob in a ``deny_list`` is an inert deny by construction."""
        matcher = parse_lists(["*"], ["product*"])
        findings = matcher.audit(drift_surfaces["drifted"])
        assert findings[0].severity == SEVERITY_LOUD
        assert findings[0].code == CODE_INERT_DENY

    def test_unprobeable_glob_in_deny_list_stays_soft(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """No literal prefix, no probe — glob semantics stay out of scope."""
        matcher = parse_lists(["*"], ["*product"])
        findings = matcher.audit(drift_surfaces["drifted"])
        assert findings[0].severity == SEVERITY_SOFT
        assert findings[0].code == CODE_PARTIAL_GLOB

    def test_partial_glob_in_allow_list_stays_soft(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """The allow-side grade is unchanged: fail-closed stays SOFT."""
        matcher = parse_lists(["product*"], [])
        findings = matcher.audit(drift_surfaces["drifted"])
        assert findings[0].severity == SEVERITY_SOFT
        assert findings[0].code == CODE_PARTIAL_GLOB

    def test_inert_bare_deny_entry_cannot_be_loud(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """A bare deny resolves by the same predicate it is probed with.

        If it matched nothing on the surface it can match nothing on the net set,
        which is a subset.  A bare deny is therefore never a silent carve-out.
        """
        matcher = parse_lists(["*"], ["nonexistent"])
        findings = matcher.audit(drift_surfaces["drifted"])
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_SOFT
        assert findings[0].code == CODE_EMPTY_MATCH

    def test_severity_is_computed_from_list_name_not_the_code(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """The same entry against the same surface grades differently per list."""
        surface = drift_surfaces["drifted"]
        as_allow = parse_lists(["catalog:product"], []).audit(surface)
        as_deny = parse_lists(["*"], ["catalog:product"]).audit(surface)
        assert as_allow[0].severity == SEVERITY_SOFT
        assert as_deny[0].severity == SEVERITY_LOUD

    def test_select_is_unchanged_by_the_severity_fix(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """Resolution semantics are ruled and correct — this PR touches severity only."""
        surface = drift_surfaces["grouped"]
        matcher = parse_lists(["*"], ["catalog:product"])
        net = matcher.select(surface)
        # The carve-out removes exactly the `product` resource slice...
        assert not {n for n in net if n.startswith("product")}
        # ...and nothing else, including its sibling slice inside the same group.
        assert "category_list" in net
        assert {"catalog", "orders", "order_list", "healthcheck"} <= net

    def test_audit_signature_takes_no_net_argument(
        self, drift_surfaces: dict[str, ToolSurface]
    ) -> None:
        """PR-9a wraps ``audit`` verbatim: it derives the net set itself.

        Passing a selection in would let a caller supply one computed against a
        different surface and silently mis-grade the finding.
        """
        matcher = parse_lists(["*"], ["catalog:product"])
        findings = matcher.audit(drift_surfaces["drifted"], route_name="admin")
        assert findings[0].severity == SEVERITY_LOUD
        with pytest.raises(TypeError):
            matcher.audit(drift_surfaces["drifted"], net=frozenset())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ToolSurface
# ---------------------------------------------------------------------------


class TestToolSurface:
    """``ToolSurface.build`` normalization and immutability."""

    def test_build_freezes_names(self) -> None:
        """``build`` freezes ``tool_names`` and every group members iterable."""
        surface = ToolSurface.build(tool_names=["a", "b"], group_members={"g": ["a"]})
        assert surface.tool_names == frozenset({"a", "b"})
        assert surface.group_members["g"] == frozenset({"a"})

    def test_build_defaults_separator(self) -> None:
        """Default ``tool_separator`` is ``'_'``."""
        surface = ToolSurface.build(tool_names=[])
        assert surface.tool_separator == "_"

    def test_none_group_members_yields_flat_surface(self) -> None:
        """``group_members=None`` produces an empty groups mapping."""
        surface = ToolSurface.build(tool_names=["a"], group_members=None)
        assert dict(surface.group_members) == {}

    def test_group_members_is_immutable(self) -> None:
        """``group_members`` is a ``MappingProxyType`` — assignment raises."""
        surface = ToolSurface.build(tool_names=["a"], group_members={"g": ["a"]})
        with pytest.raises(TypeError):
            surface.group_members["h"] = frozenset({"a"})  # type: ignore[index]


# ---------------------------------------------------------------------------
# Custom tool separator
# ---------------------------------------------------------------------------


class TestCustomSeparator:
    """Prefix expansion and qualified matching honour the configured separator."""

    def test_dot_separator_bare_expansion(self) -> None:
        """Bare labels expand using the configured separator (``.`` here)."""
        surface = ToolSurface.build(
            tool_names=["users.list", "users.retrieve", "orders.list"],
            tool_separator=".",
        )
        matcher = parse_lists(["users"], [])
        assert matcher.select(surface) == frozenset({"users.list", "users.retrieve"})

    def test_dot_separator_qualified(self) -> None:
        """Qualified entries also honour the configured separator."""
        surface = ToolSurface.build(
            tool_names=["users.list", "users.retrieve", "orders.list"],
            group_members={"admin": {"users.list", "users.retrieve", "orders.list"}},
            tool_separator=".",
        )
        matcher = parse_lists(["admin:users"], [])
        assert matcher.select(surface) == frozenset({"users.list", "users.retrieve"})
