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
depends on (anonymous POST to an admin route -> 401) is proven by PR-7's
``test_route_wiring.py::TestBan6Seam``.  A resolver that returns the right list
while the view forgets to call it is the failure
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

from frisian_mcp.route_audit import (
    E004_OPEN_WORLD_DEFAULT_ABOVE_READ,
    E005_ROUTE_SCHEMA,
    E006_ANONYMOUS_GRANT_ON_PRIVILEGED,
    W005_AUTO_REGISTER_ANONYMOUS,
    W006_AUTO_DISCOVER_ENABLED,
    W007_MAX_TIER_CAPS_ROUTE,
    W010_ANONYMOUS_SSE_REACHABLE,
    W011_UNPROVABLE_PERMISSION_CLASS,
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


def _audit_ids(settings: Any, routes: dict[str, Any], **overrides: Any) -> set[str]:
    """Run the config-time audit over *routes* and return the set of check ids.

    Assert on ids (``frisian_mcp.E004`` ...), never on message prose. PR-9b's
    ``audit_route_configs`` reads ``settings.FRISIAN_MCP_ROUTES`` and the global
    permission classes, so the settings are stamped here.
    """
    from frisian_mcp.route_audit import audit_route_configs

    settings.FRISIAN_MCP_ROUTES = routes
    for key, value in overrides.items():
        setattr(settings, key, value)
    return {m.id for m in audit_route_configs()}


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
# Minimal self-contained wire harness — for the partial-anonymous POST/GET split.
#
# The 401 seam and route mounting are exercised broadly in test_route_wiring.py
# (PR-7).  This is a small, local harness for the ONE wire behavior that file
# does not cover: an ``IsAuthenticatedOrReadOnly`` route denying anonymous POST
# while opening the anonymous GET SSE stream.  Kept self-contained rather than
# importing that module's private fixtures.
# ---------------------------------------------------------------------------

_WIRE_PATH = "mcp/admin"


def _wire_post(view: Any, path: str, method: str) -> Any:
    """POST a JSON-RPC call anonymously and return the HTTP response."""
    from rest_framework.test import APIRequestFactory

    request = APIRequestFactory().post(
        f"/{path}",
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        format="json",
    )
    return view(request)


def _wire_get(view: Any, path: str) -> Any:
    """Open the SSE channel anonymously and return the HTTP response.

    ``FRISIAN_MCP_SSE_MAX_STREAM_SECONDS=0`` (set by the fixture) closes the
    stream after one keepalive, so this never blocks a worker.
    """
    from rest_framework.test import APIRequestFactory

    request = APIRequestFactory().get(f"/{path}", HTTP_ACCEPT="text/event-stream")
    return view(request)


@pytest.fixture()
def _wire_route(settings: Any) -> Any:
    """Mount an admin route gated by ``IsAuthenticatedOrReadOnly`` and yield ``(view, path)``.

    Restores the ``route_views`` singleton afterward so the mount does not leak
    into other tests.
    """
    from frisian_mcp.apps import _make_route_mcp_view
    from frisian_mcp.registry import ToolRegistry
    from frisian_mcp.route_views import route_views

    settings.FRISIAN_MCP_PERMISSION_CLASSES = [IsAuthenticatedOrReadOnly]
    settings.FRISIAN_MCP_AUTHENTICATION_CLASSES = [
        "rest_framework.authentication.BasicAuthentication"
    ]
    settings.FRISIAN_MCP_SSE_CHANNEL = True
    settings.FRISIAN_MCP_SSE_MAX_STREAM_SECONDS = 0

    cfg = parse_route_config("admin", {"path": _WIRE_PATH, "highest_tier": "admin"})
    registry = ToolRegistry()

    with route_views._lock:  # noqa: SLF001
        saved = dict(route_views._views)  # noqa: SLF001
    route_views.rebuild(cfg, registry)
    try:
        yield _make_route_mcp_view(cfg).as_view(), _WIRE_PATH
    finally:
        with route_views._lock:  # noqa: SLF001
            route_views._views = saved  # noqa: SLF001


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
# Audit severity + wire behavior — all deps landed (PR-7, PR-9b).
# ===========================================================================


class TestFatalTriggersStopTheProcess:
    """WI-5: FATAL must genuinely refuse to boot, not merely print a banner.

    Asserts against PR-9b's shipped `route_audit`: config-time `Error` messages
    keyed on `frisian_mcp.EXXX`, plus `raise_on_fatal_route_config()` — the
    function `AppConfig.ready()` calls to make the refusal real, because a
    `checks.Error` alone does not stop a WSGI server from serving traffic.
    """

    def test_open_world_default_above_read_is_fatal(self, settings: Any) -> None:
        """Anonymous-reachable `default` with `highest_tier` above `read` -> E004.

        The trigger that actually protects the flagship config.
        """
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        ids = _audit_ids(settings, {"default": {"path": "mcp", "highest_tier": "admin"}})
        assert E004_OPEN_WORLD_DEFAULT_ABOVE_READ in ids

    def test_anonymous_grant_on_admin_is_fatal(self, settings: Any) -> None:
        """Global `AllowAny` + an `admin` route -> E006."""
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[AllowAny],
        )
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED in ids

    def test_grammar_fatal_is_reported_keyed_on_its_code(self, settings: Any) -> None:
        """A `GrammarError` surfaces as a check keyed on `frisian_mcp.E1xx`."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        ids = _audit_ids(
            settings,
            {"default": {"path": "mcp", "deny_list": ["*"]}},
        )
        assert f"frisian_mcp.{CODE_DENY_WILDCARD}" in ids

    def test_path_fatal_is_reported_keyed_on_its_code(self, settings: Any) -> None:
        """A `RoutePathError` surfaces as a check keyed on `frisian_mcp.E2xx`."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        ids = _audit_ids(
            settings,
            {
                "default": {"path": "mcp"},
                "admin": {"path": "mcp", "highest_tier": "admin"},
            },
        )
        assert f"frisian_mcp.{CODE_DUPLICATE_PATH}" in ids

    def test_schema_error_is_reported_as_e005(self, settings: Any) -> None:
        """An unknown key / bad type surfaces as E005."""
        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        ids = _audit_ids(settings, {"default": {"path": "mcp", "bogus_key": True}})
        assert E005_ROUTE_SCHEMA in ids

    def test_fatal_raises_improperly_configured_at_boot(self, settings: Any) -> None:
        """`raise_on_fatal_route_config()` raises — the WI-5 mechanism.

        Gunicorn never runs `manage.py check`; only an exception out of
        `AppConfig.ready()` stops it serving traffic.
        """
        from frisian_mcp.route_audit import raise_on_fatal_route_config

        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_ROUTES = {"default": {"path": "mcp", "highest_tier": "admin"}}
        with pytest.raises(ImproperlyConfigured):
            raise_on_fatal_route_config()

    def test_clean_config_does_not_raise_at_boot(self, settings: Any) -> None:
        """A well-formed config must boot — `raise_on_fatal_route_config()` is silent."""
        from frisian_mcp.route_audit import raise_on_fatal_route_config

        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_ROUTES = {"admin": {"path": "mcp/admin", "highest_tier": "admin"}}
        raise_on_fatal_route_config()  # must not raise

    def test_warnings_alone_do_not_raise_at_boot(self, settings: Any) -> None:
        """LOUD/SOFT are checks-framework warnings; only `Error` stops the boot."""
        from frisian_mcp.route_audit import raise_on_fatal_route_config

        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        # auto_discover -> W006 (SOFT), no Error.
        settings.FRISIAN_MCP_ROUTES = {"default": {"path": "mcp", "auto_discover": True}}
        raise_on_fatal_route_config()  # warnings present, but must not raise

    def test_check_is_registered_with_the_framework(self) -> None:
        """`check_route_config` is a registered Django system check.

        This is what makes `manage.py check` surface the FATAL — the framework
        wiring, asserted without the noise of the test env's unrelated checks
        (driving the full `call_command("check")` here would fail on those, not
        on route config).
        """
        from django.core.checks import registry as checks_registry

        from frisian_mcp.route_audit import check_route_config

        assert check_route_config in checks_registry.registry.registered_checks

    def test_registered_check_emits_the_fatal_error(self, settings: Any) -> None:
        """The registered check reports the E004 `Error` `manage.py check` fails on."""
        from django.core.checks import Error as CheckError

        from frisian_mcp.route_audit import check_route_config

        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_ROUTES = {"default": {"path": "mcp", "highest_tier": "admin"}}
        messages = check_route_config()
        errors = [m for m in messages if isinstance(m, CheckError)]
        assert any(m.id == E004_OPEN_WORLD_DEFAULT_ABOVE_READ for m in errors)

    def test_registered_check_is_clean_for_a_well_formed_config(self, settings: Any) -> None:
        """A well-formed config yields no `Error` from the registered check."""
        from django.core.checks import Error as CheckError

        from frisian_mcp.route_audit import check_route_config

        settings.FRISIAN_MCP_PERMISSION_CLASSES = []
        settings.FRISIAN_MCP_ROUTES = {"admin": {"path": "mcp/admin", "highest_tier": "admin"}}
        assert not [m for m in check_route_config() if isinstance(m, CheckError)]


class TestPermissionClassAuditSeverity:
    """The FATAL/LOUD grading each bucket earns in the config-time audit.

    The bucket *predicate* is proven live in
    :class:`TestPermissionClassBucketPredicate`; these assert the audit
    *consequence* — anonymous-granting -> E006 (FATAL), opaque -> W011 (LOUD).
    """

    def test_literal_allow_any_on_admin_is_fatal(self, settings: Any) -> None:
        """Global `AllowAny` + an `admin` route -> E006.

        Not silently overridden to `IsAuthenticated` — a silent fix trains
        operators to trust a config that means the opposite of what it says.
        """
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[AllowAny],
        )
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED in ids

    def test_unmodified_allow_any_subclass_on_admin_is_fatal(self, settings: Any) -> None:
        """`class OpenPerm(AllowAny): pass` grades E006, not silence.

        `OpenPerm is AllowAny` is False; the predicate catches the subclass.
        """
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[_OpenPerm],
        )
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED in ids

    def test_allow_any_subclass_that_overrides_gate_is_loud_not_fatal(self, settings: Any) -> None:
        """Overriding `has_permission` -> opaque -> W011, and NOT E006."""
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[_NarrowedPerm],
        )
        assert W011_UNPROVABLE_PERMISSION_CLASS in ids
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED not in ids

    def test_opaque_custom_permission_class_is_loud(self, settings: Any) -> None:
        """A class the audit cannot statically classify -> W011 (LOUD), not FATAL."""
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[_CustomPerm],
        )
        assert W011_UNPROVABLE_PERMISSION_CLASS in ids
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED not in ids

    def test_auth_requiring_classes_produce_no_bucket_finding(self, settings: Any) -> None:
        """`IsAuthenticated` is the coherent choice — no E006, no W011."""
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[IsAuthenticated],
        )
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED not in ids
        assert W011_UNPROVABLE_PERMISSION_CLASS not in ids

    def test_audit_imports_the_bucket_predicate_not_a_copy(self) -> None:
        """PR-9b consumes `route_views._bucket`; it never re-derives the classification.

        Two copies of "is this class an anonymous grant" is how the audit and the
        runtime end up disagreeing. Asserted by source: `route_audit` imports the
        predicate rather than defining its own.
        """
        import inspect

        from frisian_mcp import route_audit

        source = inspect.getsource(route_audit)
        # It imports the shipped predicate rather than defining a second copy.
        assert "from frisian_mcp.route_views import" in source
        assert "_bucket" in source
        assert "def _bucket" not in source


class TestRule3StructurallyUnreachableFatal:
    """The retired empty-classes-on-admin FATAL, kept visible for the defense pass.

    Rule-3 enforcement on the wire (anonymous POST to an admin route -> 401 +
    `WWW-Authenticate`) is proven by PR-7's
    `test_route_wiring.py::TestBan6Seam.test_anonymous_post_to_admin_route_gets_401_with_challenge`;
    the resolver it rests on is `TestRule3Resolver` here.  This class exists only
    to keep the retired criterion audit-visible.
    """

    @pytest.mark.skip(
        reason="Structurally unreachable under the BLOCKER-1 ruling: rule 3 supplies "
        "[IsAuthenticated] whenever the global setting is empty, so an elevated/admin "
        "route can never resolve to empty permission classes. Retired deliberately, "
        "not forgotten. Covered by construction (TestRule3Resolver) and by PR-7's "
        "TestBan6Seam on the wire; the AllowAny FATAL (E006) still earns its place."
    )
    def test_empty_permission_classes_on_admin_is_fatal(self) -> None:
        """Unreachable by construction. See skip reason."""
        raise NotImplementedError


@pytest.mark.usefixtures("_wire_route")
class TestPartialAnonymousWireBehavior:
    """`IsAuthenticatedOrReadOnly` on an admin route: assert the split on the wire.

    The filed finding claimed an anonymous caller reaches the admin *tool
    surface* at read tier.  They do not: `tools/list` / `tools/call` ride
    `McpView.post()`, and `POST` is not a DRF `SAFE_METHOD`.  What anonymous
    *does* reach is `McpView.get()` — the SSE keepalive.  This is the wire proof
    of the split (`TestAnonymousReachabilityPredicates` proves the predicate,
    `TestLoudAndSoftTriggers` proves the W010 audit); PR-7's `TestBan6Seam`
    covers the *empty-global* rule-3 401, so this covers the genuinely-uncovered
    `IsAuthenticatedOrReadOnly` case and asserts the opposite of what was filed.
    """

    def test_anonymous_post_is_denied_even_though_class_permits_reads(
        self, _wire_route: Any
    ) -> None:
        """POST is unsafe -> 401, even under a class that permits anonymous reads."""
        view, path = _wire_route
        response = _wire_post(view, path, "tools/list")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate")

    def test_anonymous_get_opens_the_sse_stream(self, _wire_route: Any) -> None:
        """GET is safe -> the keepalive opens. Resource exhaustion, not disclosure."""
        view, path = _wire_route
        response = _wire_get(view, path)
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("Content-Type", "")


class TestLoudAndSoftTriggers:
    """LOUD warns; SOFT informs. Both boot successfully (produce no `Error`)."""

    def test_partial_anonymous_is_loud_via_sse(self, settings: Any) -> None:
        """`IsAuthenticatedOrReadOnly` on a route -> W010 (anonymous SSE reachable).

        The finding names the SSE mechanism, and its severity is never gated on
        `highest_tier` — lowering the ceiling does not close an anonymous-GET
        keepalive.
        """
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[IsAuthenticatedOrReadOnly],
        )
        assert W010_ANONYMOUS_SSE_REACHABLE in ids
        assert E006_ANONYMOUS_GRANT_ON_PRIVILEGED not in ids

    def test_partial_anonymous_sse_loud_does_not_depend_on_ceiling(self, settings: Any) -> None:
        """Same W010 whether the ceiling is `read` or `admin` — severity is tier-blind."""
        low = _audit_ids(
            settings,
            {"elevated": {"path": "mcp/e", "highest_tier": "read"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[IsAuthenticatedOrReadOnly],
        )
        high = _audit_ids(
            settings,
            {"elevated": {"path": "mcp/e", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[IsAuthenticatedOrReadOnly],
        )
        assert W010_ANONYMOUS_SSE_REACHABLE in low
        assert W010_ANONYMOUS_SSE_REACHABLE in high

    def test_sse_reachable_loud_is_suppressed_under_allow_unauthenticated(
        self, settings: Any
    ) -> None:
        """The acknowledged open demo must not be double-warned (W001 already covers it)."""
        ids = _audit_ids(
            settings,
            {"default": {"path": "mcp"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[],
            FRISIAN_MCP_ALLOW_UNAUTHENTICATED=True,
        )
        assert W010_ANONYMOUS_SSE_REACHABLE not in ids

    def test_auto_register_on_anonymous_reachable_route_is_loud(self, settings: Any) -> None:
        """`auto_register` on an anonymous-POST-reachable route -> W005."""
        ids = _audit_ids(
            settings,
            {"default": {"path": "mcp", "auto_register": True}},
            FRISIAN_MCP_PERMISSION_CLASSES=[],
        )
        assert W005_AUTO_REGISTER_ANONYMOUS in ids

    def test_auto_discover_true_is_soft(self, settings: Any) -> None:
        """`auto_discover` alone carries no privilege -> W006 (SOFT)."""
        ids = _audit_ids(
            settings,
            {"default": {"path": "mcp", "auto_discover": True}},
            FRISIAN_MCP_PERMISSION_CLASSES=[],
        )
        assert W006_AUTO_DISCOVER_ENABLED in ids

    def test_global_max_tier_capping_a_route_below_its_ceiling_is_soft(self, settings: Any) -> None:
        """`min(token, route_ceiling, FRISIAN_MCP_MAX_TIER)` — `min` narrows only.

        Where a global `MAX_TIER` caps a route below its declared `highest_tier`,
        the operator's `admin` route is silently inert. W007 (SOFT) tells them why.
        """
        ids = _audit_ids(
            settings,
            {"admin": {"path": "mcp/admin", "highest_tier": "admin"}},
            FRISIAN_MCP_PERMISSION_CLASSES=[IsAuthenticated],
            FRISIAN_MCP_MAX_TIER="read",
        )
        assert W007_MAX_TIER_CAPS_ROUTE in ids

    def test_loud_and_soft_produce_no_fatal(self, settings: Any) -> None:
        """None of the warning triggers refuses to boot."""
        from frisian_mcp.route_audit import raise_on_fatal_route_config

        settings.FRISIAN_MCP_PERMISSION_CLASSES = [IsAuthenticatedOrReadOnly]
        settings.FRISIAN_MCP_ROUTES = {
            "admin": {"path": "mcp/admin", "highest_tier": "admin", "auto_discover": True}
        }
        raise_on_fatal_route_config()  # warnings only — must not raise
