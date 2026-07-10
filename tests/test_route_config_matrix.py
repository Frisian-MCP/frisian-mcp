"""Config-validation matrix — every FATAL / LOUD / SOFT trigger (PR-13).

Three severities, three different observable behaviors:

======  =====================================================================
FATAL   The process **refuses to boot**.  ``manage.py check --deploy`` exits
        non-zero and ``AppConfig.ready()`` raises ``ImproperlyConfigured``.
        Asserting that an ``Error``-level message was *emitted* is not enough:
        a ``checks.Error`` that nothing raises on is a banner, and WI-5 exists
        precisely to reject banners.
LOUD    Prominent log output; boot **succeeds**.
SOFT    Log output; boot succeeds; no operational impact.
======  =====================================================================

Assertions target **codes** (``E1xx`` / ``E2xx`` / ``W1xx``), never message
prose.  Message text is not a contract; the code is.  PR-9 keys its Django
check IDs off ``.code`` verbatim, which is why the grammar (``E1xx``) and path
(``E2xx``) code blocks are disjoint.

Sections that assert against PR-4 (grammar), PR-3 (tier schema), PR-5 (paths)
and PR-6 (the ``route_views`` resolver + bucket predicate) are **live** — those
modules have landed.  Sections that need PR-7's live endpoint or PR-9b's
config-time audit grading are staged behind an explicit skip and unskipped as
those land.

Permission-class classification (BLOCKER-1 ruling)
-------------------------------------------------
``route_effective_permission_classes`` resolves:

1. non-empty global setting            -> honored verbatim on every route
2. empty global + ``default``          -> ``[]``               (open demo preserved)
3. empty global + ``elevated``/``admin`` -> ``[IsAuthenticated]``

Rule 3 is **load-bearing security**, not a convenience default: if it fails, an
admin route silently serves anonymous traffic and nothing else catches it.  The
resolver is proven live in :class:`TestRule3Resolver`; the wire enforcement it
depends on is staged in :class:`TestRule3EnforcedOnTheWire` (PR-7).  A resolver
that returns the right list while the view forgets to call it is the failure
mode, which is why both exist.

The empty-classes-on-admin FATAL is **structurally unreachable** under rule 3.
It is retained as an explicit skip rather than deleted, so the deferral-defense
pass can see the criterion was retired deliberately rather than forgotten.

Anonymous reachability is a property of ``(route, HTTP method)``
---------------------------------------------------------------
``IsAuthenticatedOrReadOnly`` does **not** expose the tool surface to anonymous
callers.  ``tools/list`` and ``tools/call`` are JSON-RPC methods dispatched
inside ``McpView.post()``, and ``POST`` is not in DRF's ``SAFE_METHODS`` — so an
anonymous caller is denied at the permission layer, before any tier logic runs.
``FRISIAN_MCP_UNAUTHENTICATED_TIER`` is never consulted.

What anonymous *does* reach is ``McpView.get()`` — the SSE keepalive stream.
That is resource exhaustion, not disclosure.  Hence two predicates, proven live
at the resolver level in :class:`TestAnonymousReachabilityPredicates`; the wire
proof (``TestPartialAnonymousWireBehavior``, PR-7) asserts the **denial** on POST
and the **stream** on GET.  Severity is never conditioned on ``highest_tier``:
lowering a route's ceiling does not mitigate an anonymous-SSE hole, and a check
that implies otherwise is worse than no check.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from rest_framework.permissions import (
    AllowAny,
    IsAdminUser,
    IsAuthenticated,
    IsAuthenticatedOrReadOnly,
)

from frisian_mcp.route_config import RouteConfig, canonical_permission_tier, parse_route_config
from frisian_mcp.route_grammar import (
    CODE_DENY_WILDCARD,
    CODE_DOUBLE_UNDERSCORE,
    CODE_EMPTY_ENTRY,
    CODE_EMPTY_MATCH,
    CODE_NON_STRING_ENTRY,
    CODE_PARTIAL_GLOB,
    CODE_TOO_MANY_SEGMENTS,
    CODE_UNKNOWN_GROUP,
    SEVERITY_SOFT,
    GrammarError,
    ToolSurface,
    parse_lists,
)
from frisian_mcp.route_paths import (
    CODE_DUPLICATE_PATH,
    CODE_EMPTY_PATH,
    CODE_INVALID_PATH_TYPE,
    CODE_PATH_TEMPLATE,
    CODE_RESERVED_PATH,
    RoutePathError,
    normalize_route_path,
    reserved_route_paths,
    validate_route_paths,
)
from frisian_mcp.route_views import (
    BUCKET_ANONYMOUS_GRANTING,
    BUCKET_AUTH_REQUIRING,
    BUCKET_OPAQUE,
    BUCKET_PARTIAL_ANONYMOUS,
    _bucket,
    _is_anonymous_granting,
    route_effective_permission_classes,
    route_is_anonymous_reachable,
    route_is_anonymous_sse_reachable,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Cfg:
    """Minimal stand-in for RouteConfig — validate_route_paths wants ``.path``."""

    def __init__(self, path: Any) -> None:
        self.path = path


def _paths(**routes: Any) -> dict[str, _Cfg]:
    """Build the ``{name: config}`` mapping ``validate_route_paths`` expects."""
    return {name: _Cfg(path) for name, path in routes.items()}


def _surface() -> ToolSurface:
    """A dispatch-group surface: group ``catalog``, two resources, plus a flat tool.

    Member tool names are **resource-leading**, not group-leading — ``item_list``,
    never ``catalog_item_list``.  ``apps.py`` builds ``member_tools`` by matching
    the configured ``prefix_set`` against the *first* segment of each registered
    tool name, so the group name is a config label that never appears in the tool
    names it bundles.  ``group_dispatcher`` depends on the same shape: it resolves
    ``target_name = f"{resource}{sep}{action}"``.

    Getting this backwards makes ``deny_list: ["catalog:item"]`` look like a silent
    no-op when the grammar is in fact correct.

    Names here are deliberately generic.  This is a Django MCP gateway package, not
    a package for any one host application, and a fixture borrowed from a specific
    vendor's schema implies the grammar knows something about that domain.  It does
    not: it knows ``group``, ``resource``, and ``action``.
    """
    return ToolSurface.build(
        tool_names=["catalog", "item_list", "item_create", "order_list", "ping"],
        group_members={"catalog": ["item_list", "item_create", "order_list"]},
        tool_separator="_",
    )


def _codes(findings: tuple[Any, ...]) -> set[str]:
    return {f.code for f in findings}


def _route(name: str, **raw: Any) -> RouteConfig:
    """Build a real ``RouteConfig`` via the landed parser.

    ``path`` defaults to the route name so callers only specify the fields the
    test is about (``highest_tier``, ``allow_list``, ``auto_register``, ...).
    """
    raw.setdefault("path", name)
    return parse_route_config(name, raw)


class _OpenPerm(AllowAny):
    """An unmodified ``AllowAny`` subclass — the fail-open the R1 predicate must catch."""


class _NarrowedPerm(AllowAny):
    """An ``AllowAny`` subclass that actually overrides the gate.

    Semantically no longer an anonymous grant, so it must fall through to the
    opaque bucket rather than being graded as ``AllowAny``.
    """

    def has_permission(self, request: Any, view: Any) -> bool:
        """Deny anonymous; this class is not a blanket grant."""
        return bool(getattr(request, "user", None) and request.user.is_authenticated)


class _CustomPerm:
    """An opaque custom permission class — not statically recognizable."""

    def has_permission(self, request: Any, view: Any) -> bool:
        """Arbitrary host logic the audit cannot classify."""
        return True


# ---------------------------------------------------------------------------
# Grammar — FATAL at parse (PR-4, landed)
# ---------------------------------------------------------------------------


class TestGrammarFatal:
    """FATAL grammar errors raise ``GrammarError`` at parse, never at audit."""

    def test_star_in_deny_list_rejected_at_parse(self) -> None:
        """`deny_list: ["*"]` -> E101. A deny-everything wildcard is a footgun."""
        with pytest.raises(GrammarError) as exc:
            parse_lists(["*"], ["*"], route_name="default")
        assert exc.value.code == CODE_DENY_WILDCARD

    def test_double_underscore_rejected_at_parse(self) -> None:
        """`catalog__item` -> E102."""
        with pytest.raises(GrammarError) as exc:
            parse_lists(["catalog__item"], [], route_name="default")
        assert exc.value.code == CODE_DOUBLE_UNDERSCORE

    def test_three_segment_entry_rejected(self) -> None:
        """`catalog:item:list` -> E105. Mechanically defends the no-`resource_action` deferral."""
        with pytest.raises(GrammarError) as exc:
            parse_lists(["catalog:item:list"], [], route_name="default")
        assert exc.value.code == CODE_TOO_MANY_SEGMENTS

    def test_empty_entry_rejected(self) -> None:
        """`""` -> E103."""
        with pytest.raises(GrammarError) as exc:
            parse_lists([""], [], route_name="default")
        assert exc.value.code == CODE_EMPTY_ENTRY

    def test_non_string_entry_rejected(self) -> None:
        """`123` -> E100."""
        with pytest.raises(GrammarError) as exc:
            parse_lists([123], [], route_name="default")
        assert exc.value.code == CODE_NON_STRING_ENTRY

    def test_grammar_error_carries_route_name(self) -> None:
        """PR-9 reads `.route_name` off the error rather than re-deriving it."""
        with pytest.raises(GrammarError) as exc:
            parse_lists(["a:b:c"], [], route_name="admin")
        assert exc.value.route_name == "admin"


# ---------------------------------------------------------------------------
# Grammar — accepted / normalized (PR-4, landed)
# ---------------------------------------------------------------------------


class TestGrammarAccepted:
    """Entries that must parse cleanly and produce no finding."""

    def test_group_star_normalizes_to_bare_group_silently(self) -> None:
        """`catalog:*` accepted, silent, and selects exactly what bare `catalog` selects."""
        surface = _surface()
        starred = parse_lists(["catalog:*"], [], route_name="default")
        bare = parse_lists(["catalog"], [], route_name="default")

        assert starred.select(surface) == bare.select(surface)
        assert starred.audit(surface, route_name="default") == ()

    def test_bare_wildcard_in_allow_list_is_legal(self) -> None:
        """`allow_list: ["*"]` is the legacy-default posture, not an error."""
        surface = _surface()
        matcher = parse_lists(["*"], [], route_name="default")
        assert matcher.select(surface) == frozenset(surface.tool_names)

    def test_deny_is_a_strict_carve_out(self) -> None:
        """select() == allow_union - deny_union; denied names are absent, not flagged.

        `catalog:item` must remove **every** action on the resource
        (`item_list` and `item_create`), not just an exact name match.
        """
        surface = _surface()
        matcher = parse_lists(["catalog"], ["catalog:item"], route_name="default")
        selected = matcher.select(surface)

        assert "item_list" not in selected
        assert "item_create" not in selected
        assert "order_list" in selected
        assert "catalog" in selected


# ---------------------------------------------------------------------------
# Grammar — SOFT findings at audit (PR-4, landed)
# ---------------------------------------------------------------------------


class TestGrammarSoftFindings:
    """SOFT findings surface at audit time, never at parse."""

    def test_partial_glob_matches_nothing_is_soft(self) -> None:
        """`catalog:it*` parses as literal-unmatchable -> W110 at audit."""
        matcher = parse_lists(["catalog:it*"], [], route_name="default")
        findings = matcher.audit(_surface(), route_name="default")
        assert CODE_PARTIAL_GLOB in _codes(findings)

    def test_entry_resolving_to_zero_tools_is_soft(self) -> None:
        """A bare entry matching no tool -> W111."""
        matcher = parse_lists(["ghost"], [], route_name="default")
        findings = matcher.audit(_surface(), route_name="default")
        assert CODE_EMPTY_MATCH in _codes(findings)

    def test_qualified_entry_with_unknown_group_is_soft(self) -> None:
        """`ghost:thing` -> W112, which is more specific than W111 and must win."""
        matcher = parse_lists(["ghost:thing"], [], route_name="default")
        findings = matcher.audit(_surface(), route_name="default")
        codes = _codes(findings)
        assert CODE_UNKNOWN_GROUP in codes
        assert CODE_EMPTY_MATCH not in codes

    def test_soft_findings_are_soft_severity(self) -> None:
        """Severity must be SOFT — PR-9 maps it to checks.Warning, not checks.Error."""
        matcher = parse_lists(["catalog:it*"], [], route_name="default")
        findings = matcher.audit(_surface(), route_name="default")
        assert findings
        assert all(f.severity == SEVERITY_SOFT for f in findings)

    def test_findings_carry_route_name_and_list_name(self) -> None:
        """PR-9 wraps findings; it must not re-derive their provenance."""
        matcher = parse_lists([], ["catalog:it*"], route_name="elevated")
        findings = matcher.audit(_surface(), route_name="elevated")
        assert findings
        assert all(f.route_name == "elevated" for f in findings)
        assert any(f.list_name == "deny_list" for f in findings)


# ---------------------------------------------------------------------------
# Tier schema — no synonyms, anywhere (PR-3, landed; deferral #7)
# ---------------------------------------------------------------------------


class TestTierSynonymsRejected:
    """Deferral #7 is enforced at parse time, not defended by review."""

    @pytest.mark.parametrize(
        "synonym",
        ["readonly", "read-only", "RO", "rw", "read_only", "write", "Admin", "READ"],
    )
    def test_synonym_rejected(self, synonym: str) -> None:
        """Every synonym raises; none is silently normalized."""
        with pytest.raises(ImproperlyConfigured):
            canonical_permission_tier(synonym)

    @pytest.mark.parametrize("tier", ["read", "read_write", "admin"])
    def test_canonical_tier_accepted(self, tier: str) -> None:
        """The three canonical tiers round-trip unchanged."""
        assert canonical_permission_tier(tier) == tier

    def test_bare_write_is_not_a_tier(self) -> None:
        """`write` is the README's documented-but-fatal value. Pinned deliberately.

        `_TIER_RANK` has no `write`; PR-3 turned this documentation error into a
        boot failure. The README fix rides PR-15.
        """
        with pytest.raises(ImproperlyConfigured):
            canonical_permission_tier("write")


# ---------------------------------------------------------------------------
# Path normalization + collision (PR-5, landed) — WI-7
# ---------------------------------------------------------------------------


class TestPathNormalization:
    """Normalize before every comparison; nesting is legal, collision is not.

    Grounded by probing PR-5's landed surface: ``validate_route_paths`` takes
    objects exposing ``.path`` (i.e. ``RouteConfig``), not raw dicts.
    """

    @pytest.mark.parametrize("raw", ["mcp", "/mcp", "mcp/", "/mcp/", "//mcp//", "  /mcp/  "])
    def test_slash_and_whitespace_variants_canonicalize(self, raw: str) -> None:
        """All spellings of the same path normalize identically."""
        assert normalize_route_path(raw, route_name="r") == "mcp"

    def test_trailing_slash_variants_collide(self) -> None:
        """`mcp` and `mcp/` are the same path -> E203."""
        with pytest.raises(RoutePathError) as exc:
            validate_route_paths(_paths(a="mcp", b="mcp/"))
        assert exc.value.code == CODE_DUPLICATE_PATH

    def test_shared_prefix_nesting_is_legal(self) -> None:
        """`mcp`, `mcp/elevated`, `mcp/admin` — no false positive.

        Longest-match wins; they resolve unambiguously. This is the watch item
        most likely to be implemented backwards.
        """
        canonical = validate_route_paths(_paths(d="mcp", e="mcp/elevated", f="mcp/admin"))
        assert canonical == {"d": "mcp", "e": "mcp/elevated", "f": "mcp/admin"}

    @pytest.mark.parametrize("reserved", sorted(reserved_route_paths()))
    def test_route_on_reserved_path_is_fatal(self, reserved: str) -> None:
        """Parametrized over `reserved_route_paths()` — the FUNCTION, not the constant.

        The function folds in `settings.FRISIAN_MCP_HEALTHCHECK_PATHS` at call
        time, so the reserved set is host-dependent. A test hardcoding
        `RESERVED_ROUTE_PATHS` silently stops covering a relocated healthcheck.
        """
        with pytest.raises(RoutePathError) as exc:
            validate_route_paths(_paths(a=reserved))
        assert exc.value.code == CODE_RESERVED_PATH

    def test_route_nested_under_reserved_path_is_fatal(self) -> None:
        """`oauth/token` nests under the reserved `oauth` -> E204."""
        with pytest.raises(RoutePathError) as exc:
            validate_route_paths(_paths(a="oauth/token"))
        assert exc.value.code == CODE_RESERVED_PATH

    def test_reserved_collision_respects_segment_boundaries(self) -> None:
        """`oauthx` must NOT collide with `oauth`. Prefix != segment."""
        assert validate_route_paths(_paths(a="oauthx")) == {"a": "oauthx"}

    def test_greedy_root_mount_is_fatal_as_empty_path(self) -> None:
        """A mount at `/` is FATAL — but as **E201 (empty)**, not E204 (reserved).

        `normalize_route_path` strips slashes, so `"/"` canonicalizes to `""`
        and is rejected as empty before the reserved-shadow check ever runs.
        The plan describes this as "a greedy mount at `/` that shadows them";
        the outcome is FATAL as required, but the code is E201. Asserting E204
        here would fail against correct behavior.
        """
        with pytest.raises(RoutePathError) as exc:
            validate_route_paths(_paths(root="/", b="mcp"))
        assert exc.value.code == CODE_EMPTY_PATH

    def test_path_template_rejected(self) -> None:
        """`mcp/{principal_id}` -> E202.

        Deferral #1 (no `{optional_principal_id}` path parsing) enforced at
        parse time rather than defended by review. A path segment is a routing
        label, never a credential.
        """
        with pytest.raises(RoutePathError) as exc:
            normalize_route_path("mcp/{principal_id}", route_name="default")
        assert exc.value.code == CODE_PATH_TEMPLATE

    def test_non_string_path_rejected(self) -> None:
        """`123` -> E200."""
        with pytest.raises(RoutePathError) as exc:
            normalize_route_path(123, route_name="default")
        assert exc.value.code == CODE_INVALID_PATH_TYPE

    def test_grammar_and_path_code_blocks_are_disjoint(self) -> None:
        """`E1xx` (grammar) vs `E2xx` (paths). PR-9 keys check IDs off `.code` verbatim.

        A collision here would silently merge two unrelated checks.
        """
        grammar = {
            CODE_NON_STRING_ENTRY,
            CODE_DENY_WILDCARD,
            CODE_DOUBLE_UNDERSCORE,
            CODE_EMPTY_ENTRY,
            CODE_TOO_MANY_SEGMENTS,
        }
        paths = {
            CODE_INVALID_PATH_TYPE,
            CODE_EMPTY_PATH,
            CODE_PATH_TEMPLATE,
            CODE_DUPLICATE_PATH,
            CODE_RESERVED_PATH,
        }
        assert not (grammar & paths)


# ===========================================================================
# LIVE (PR-6) — resolver + bucket predicate. `route_views` has landed.
# ===========================================================================


class TestPermissionClassBucketPredicate:
    """R1: the anonymous-grant predicate must not be evadable by subclassing.

    This is the resolver-level classification (`_bucket` / `_is_anonymous_granting`).
    The FATAL/LOUD *audit consequence* of each bucket is PR-9b's grading, staged
    below — but the predicate that grading rests on is testable now, and it is
    the exact thing R1 was about.
    """

    def test_literal_allow_any_is_anonymous_granting(self) -> None:
        """Plain `AllowAny` classifies as the anonymous-granting bucket."""
        assert _is_anonymous_granting(AllowAny)
        assert _bucket(AllowAny) == BUCKET_ANONYMOUS_GRANTING

    def test_unmodified_allow_any_subclass_does_not_fail_open(self) -> None:
        """`class OpenPerm(AllowAny): pass` must NOT evade classification.

        `OpenPerm is AllowAny` is False — the plan's "literal" test fails open.
        The predicate is `issubclass(c, AllowAny) and
        c.has_permission is AllowAny.has_permission`, so an unmodified anonymous
        grant is still caught.
        """
        assert _OpenPerm is not AllowAny
        assert _is_anonymous_granting(_OpenPerm)
        assert _bucket(_OpenPerm) == BUCKET_ANONYMOUS_GRANTING

    def test_allow_any_subclass_that_overrides_gate_is_opaque(self) -> None:
        """Overriding `has_permission` makes it semantically not-AllowAny -> opaque.

        A subclass that actually narrows the gate is judged on its own merits,
        not graded as a blanket anonymous grant.
        """
        assert not _is_anonymous_granting(_NarrowedPerm)
        assert _bucket(_NarrowedPerm) == BUCKET_OPAQUE

    def test_is_authenticated_is_auth_requiring(self) -> None:
        """`IsAuthenticated` / `IsAdminUser` classify as auth-requiring."""
        assert _bucket(IsAuthenticated) == BUCKET_AUTH_REQUIRING
        assert _bucket(IsAdminUser) == BUCKET_AUTH_REQUIRING

    def test_is_authenticated_or_read_only_is_partial_anonymous(self) -> None:
        """The hole between FATAL and LOUD: partial-anonymous is its own bucket."""
        assert _bucket(IsAuthenticatedOrReadOnly) == BUCKET_PARTIAL_ANONYMOUS

    def test_opaque_custom_permission_class_is_opaque(self) -> None:
        """Not statically recognizable as authentication-requiring."""
        assert _bucket(_CustomPerm) == BUCKET_OPAQUE


class TestRule3Resolver:
    """Empty global + `admin` route must resolve to `[IsAuthenticated]` (BLOCKER-1 rule 3).

    Rule 3 is load-bearing security: without it, an admin route with no global
    classes silently serves anonymous traffic and nothing else catches it. The
    resolver is testable now; the wire-level 401 proof is staged behind PR-7.
    """

    def test_resolver_returns_is_authenticated_for_admin(self, settings: Any) -> None:
        """Empty global + `admin` -> `[IsAuthenticated]`."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert route_effective_permission_classes(_route("admin")) == [IsAuthenticated]

    def test_resolver_returns_is_authenticated_for_elevated(self, settings: Any) -> None:
        """Empty global + `elevated` -> `[IsAuthenticated]`."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert route_effective_permission_classes(_route("elevated")) == [IsAuthenticated]

    def test_resolver_returns_empty_for_default(self, settings: Any) -> None:
        """The open demo keeps working: empty global + `default` -> `[]`."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert route_effective_permission_classes(_route("default")) == []

    def test_non_empty_global_setting_wins_on_every_route(self, settings: Any) -> None:
        """An operator who wrote `[IsAdminUser]` gets it verbatim — no silent substitution."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = [IsAdminUser]
        assert route_effective_permission_classes(_route("admin")) == [IsAdminUser]
        assert route_effective_permission_classes(_route("default")) == [IsAdminUser]

    def test_unset_global_behaves_as_empty(self, settings: Any) -> None:
        """Unset and empty both trigger per-route secure defaults."""
        if hasattr(settings, "FRISIAN_MCP_PERMISSION_CLASSES"):
            del settings.FRISIAN_MCP_PERMISSION_CLASSES
        assert route_effective_permission_classes(_route("admin")) == [IsAuthenticated]


class TestAnonymousReachabilityPredicates:
    """Anonymous reachability is a property of `(route, HTTP method)`, not of a route.

    Two predicates, tested at the resolver level (PR-6). The wire behavior that
    proves them (anonymous POST -> 401, anonymous GET -> SSE) is staged behind
    PR-7's live endpoint.
    """

    def test_empty_classes_route_is_post_reachable(self, settings: Any) -> None:
        """Empty global + `default` (open demo) -> POST-reachable."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert route_is_anonymous_reachable(_route("default"))

    def test_admin_route_is_not_post_reachable(self, settings: Any) -> None:
        """Rule 3 supplies `[IsAuthenticated]`, so admin is not POST-reachable."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert not route_is_anonymous_reachable(_route("admin"))

    def test_partial_anonymous_is_not_post_reachable(self, settings: Any) -> None:
        """`IsAuthenticatedOrReadOnly` denies anonymous POST -> not POST-reachable.

        Adding a `_PARTIAL_ANONYMOUS` arm to this predicate would ship a false
        positive on L3 (`auto_register`), the exact flag this project exists to
        make trustworthy.
        """
        settings.FRISIAN_MCP_PERMISSION_CLASSES = [IsAuthenticatedOrReadOnly]
        assert not route_is_anonymous_reachable(_route("admin"))

    def test_partial_anonymous_is_sse_reachable(self, settings: Any) -> None:
        """`IsAuthenticatedOrReadOnly` permits anonymous GET -> SSE-reachable."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = [IsAuthenticatedOrReadOnly]
        assert route_is_anonymous_sse_reachable(_route("admin"))

    def test_admin_route_is_not_sse_reachable(self, settings: Any) -> None:
        """`[IsAuthenticated]` denies anonymous GET too."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert not route_is_anonymous_sse_reachable(_route("admin"))

    def test_open_default_is_both_reachable(self, settings: Any) -> None:
        """Empty classes -> reachable on both methods (the open demo)."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        assert route_is_anonymous_reachable(_route("default"))
        assert route_is_anonymous_sse_reachable(_route("default"))


# ===========================================================================
# STAGED — unskip as PR-7 (live endpoint) and PR-9b (config-time audit) land.
# ===========================================================================

_AWAITS_PR9B = pytest.mark.skip(
    reason="Awaits PR-9b (config-time FATAL/LOUD/SOFT audit + Django-checks wiring)."
)
_AWAITS_PR7 = pytest.mark.skip(
    reason="Awaits PR-7 (McpView.post() route-view + effective-tier wiring; live endpoint)."
)


@_AWAITS_PR9B
class TestFatalTriggersStopTheProcess:
    """WI-5: FATAL must genuinely refuse to boot, not merely print a banner."""

    def test_open_world_default_above_read_is_fatal(self) -> None:
        """Anonymous-reachable `default` with `highest_tier` above `read`.

        The trigger that actually protects the flagship config. Independent of
        the permission-class resolver.
        """
        raise NotImplementedError

    def test_fatal_fails_check_deploy_with_nonzero_exit(self) -> None:
        """`manage.py check --deploy` exits non-zero. Not "a message was emitted"."""
        raise NotImplementedError

    def test_fatal_raises_improperly_configured_in_app_ready(self) -> None:
        """Gunicorn never runs `check`. `AppConfig.ready()` must stop the process."""
        raise NotImplementedError

    def test_grammar_fatal_propagates_to_boot(self) -> None:
        """A `GrammarError` becomes checks.Error + ImproperlyConfigured, keyed on `.code`."""
        raise NotImplementedError

    def test_path_fatal_propagates_to_boot(self) -> None:
        """A `RoutePathError` becomes checks.Error + ImproperlyConfigured, keyed on `.code`."""
        raise NotImplementedError


@_AWAITS_PR9B
class TestPermissionClassAuditSeverity:
    """The FATAL/LOUD grading each bucket earns in the config-time audit.

    The bucket *predicate* is proven live in
    :class:`TestPermissionClassBucketPredicate`; these assert the audit
    *consequence* of each bucket, which needs PR-9b's grading.
    """

    def test_literal_allow_any_on_admin_is_fatal(self) -> None:
        """Global `AllowAny` + an `admin` route: incoherent, refuse to boot.

        Not silently overridden to `IsAuthenticated` — a silent fix trains
        operators to trust a config that means the opposite of what it says.
        """
        raise NotImplementedError

    def test_unmodified_allow_any_subclass_on_admin_is_fatal(self) -> None:
        """`class OpenPerm(AllowAny): pass` grades FATAL, not silence."""
        raise NotImplementedError

    def test_allow_any_subclass_that_overrides_gate_is_loud_not_fatal(self) -> None:
        """The opaque bucket grades LOUD, not FATAL."""
        raise NotImplementedError

    def test_opaque_custom_permission_class_is_loud(self) -> None:
        """Not statically recognizable as authentication-requiring -> LOUD."""
        raise NotImplementedError

    def test_audit_imports_the_bucket_predicate_not_a_copy(self) -> None:
        """PR-9b imports `_bucket` from `route_views`; it never re-derives it.

        Two copies of "is this route anonymous" is how the audit and the runtime
        end up disagreeing.
        """
        raise NotImplementedError


@_AWAITS_PR7
class TestRule3EnforcedOnTheWire:
    """The resolver returning `[IsAuthenticated]` is worthless if the view ignores it.

    :class:`TestRule3Resolver` proves the resolver. This proves the endpoint
    actually applies it — the failure mode where the resolver is right and
    `post()` forgets to call it.
    """

    def test_anonymous_post_to_admin_route_gets_401(self) -> None:
        """401 with the `WWW-Authenticate` challenge intact — no 404 masking (deferral #6)."""
        raise NotImplementedError

    @pytest.mark.skip(
        reason="Structurally unreachable under the BLOCKER-1 ruling: rule 3 supplies "
        "[IsAuthenticated] whenever the global setting is empty, so an elevated/admin "
        "route can never resolve to empty permission classes. Retired deliberately, "
        "not forgotten. The hazard is eliminated by construction and covered by "
        "TestRule3Resolver; the AllowAny FATAL still earns its place."
    )
    def test_empty_permission_classes_on_admin_is_fatal(self) -> None:
        """Unreachable by construction. See skip reason."""
        raise NotImplementedError


@_AWAITS_PR7
class TestPartialAnonymousWireBehavior:
    """`IsAuthenticatedOrReadOnly` on `admin`: assert what actually happens on the wire.

    The filed finding claimed an anonymous caller reaches the admin *tool
    surface* at read tier.  They do not.  `tools/list` and `tools/call` are
    JSON-RPC inside `McpView.post()`, and `POST` is not a DRF `SAFE_METHOD`, so
    the permission layer denies anonymous before any tier logic runs.  The
    resolver-level proof is :meth:`TestAnonymousReachabilityPredicates`; these
    assert the wire, and they assert the **opposite** of what was filed.
    """

    def test_anonymous_post_tools_list_is_denied(self) -> None:
        """POST is not a SAFE_METHOD -> 401. No enumeration, no invocation."""
        raise NotImplementedError

    def test_anonymous_get_opens_sse_stream(self) -> None:
        """GET is safe -> the SSE keepalive opens. Resource exhaustion, not disclosure."""
        raise NotImplementedError

    def test_unauthenticated_tier_is_never_consulted_on_post(self) -> None:
        """The denial happens above the tier layer entirely."""
        raise NotImplementedError


@_AWAITS_PR9B
class TestLoudAndSoftTriggers:
    """LOUD warns; SOFT informs. Both boot successfully."""

    def test_partial_anonymous_is_loud_never_conditioned_on_tier(self) -> None:
        """`_PARTIAL_ANONYMOUS` -> LOUD on every route, NEVER gated on `highest_tier`.

        Conditioning severity on the ceiling would tell an operator that
        lowering `highest_tier` mitigates an anonymous-SSE hole. It does not.
        """
        raise NotImplementedError

    def test_auto_register_on_anonymous_reachable_route_is_loud(self) -> None:
        """`route_is_anonymous_reachable(route) and route.auto_register`."""
        raise NotImplementedError

    def test_sse_reachable_loud_is_suppressed_under_allow_unauthenticated(self) -> None:
        """The acknowledged open demo must not be double-warned (W001 already covers it)."""
        raise NotImplementedError

    def test_auto_discover_true_is_soft(self) -> None:
        """`auto_discover` alone carries no privilege."""
        raise NotImplementedError

    def test_global_max_tier_capping_a_route_below_its_ceiling_is_soft(self) -> None:
        """`min(token, route_ceiling, FRISIAN_MCP_MAX_TIER)` — `min` narrows only.

        Where a global `MAX_TIER` caps a route below its declared `highest_tier`,
        the operator's `admin` route is silently inert. SOFT tells them why.
        """
        raise NotImplementedError

    def test_loud_and_soft_boot_successfully(self) -> None:
        """`check --deploy` exits zero. Only FATAL refuses."""
        raise NotImplementedError
