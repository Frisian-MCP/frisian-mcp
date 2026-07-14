"""
Allow/deny grammar and matcher for per-route dispatcher filtering.

Config surface (v1.1)
---------------------

Each route in :setting:`FRISIAN_MCP_ROUTES` carries an ``allow_list`` and a
``deny_list``.  The lists filter the tool registry snapshot into that route's
:class:`~frisian_mcp.route_views.RouteView` (constructed by PR-6).  This
module owns the *parser*, the *matcher*, and the *audit-time* SOFT
warnings surfaced to Django ``checks`` by PR-9.

Baseline is **deny-all**: an empty ``allow_list`` matches no tools.
``deny_list`` is a *carve-out* from ``allow_list`` — deny is evaluated
after allow and always wins.  Absence of a denied tool from the route view
is byte-identical to absence of a tool that was never registered (this is
the WI-1 absence invariant that PR-6/PR-8 will assert against).

Grammar
-------

Entries are strings and follow this exact grammar (no other forms are
accepted):

* ``"*"`` — wildcard: every currently-registered tool plus anything
  discovered later at rebuild time.  Meaningful only in ``allow_list``;
  **FATAL** in ``deny_list`` (a wildcard deny against the deny-all baseline
  is self-contradictory).
* ``"group"`` — a whole dispatch group: the group dispatcher tool itself
  plus every flat tool bundled inside it.
* ``"group:resource"`` — a single resource slice within a group; matches
  every group member whose name equals ``resource`` or starts with
  ``resource{sep}``, where ``sep`` is
  :setting:`FRISIAN_MCP_TOOL_NAME_SEPARATOR` (default ``"_"``).
* ``"group:*"`` — silently normalized to bare ``"group"`` at parse time
  (no warning).

**Bare-entry resolution** (used at :meth:`RouteMatcher.select` time against a
:class:`ToolSurface`):

1. If the label matches a registered group name, expand to the whole group.
2. Otherwise, treat as a flat resource/tool label — match any tool whose
   name equals the label exactly or starts with ``label{sep}``.

**Group-shadows-tool rule.**  A flat tool whose name collides with a
dispatch-group name is *not addressable* by its bare name; bare resolution
always binds to the group first.  Reach a shadowed flat tool via
``group:resource`` when it happens to live inside that group; otherwise
rename it.  (The rule is a documented consequence of resolution order, not
an extra check.)

Rejected patterns (FATAL at parse)
----------------------------------

Each raises :class:`GrammarError`:

* Non-string entries (``CODE_NON_STRING_ENTRY``).
* Empty / whitespace-only entries, or entries with an empty segment around
  ``:`` (``CODE_EMPTY_ENTRY``).
* Any entry containing ``"__"`` (double underscore) —
  ``CODE_DOUBLE_UNDERSCORE``.  Collides with the Django ORM lookup syntax
  and the ``_`` prefix used by the hint-key channel.
* ``"*"`` in ``deny_list`` (``CODE_DENY_WILDCARD``).
* An entry with more than one ``:`` separator, e.g. ``"group:resource:action"``
  (``CODE_TOO_MANY_SEGMENTS``).  Per-action route filtering is out of scope
  for v1.1.

Unsupported patterns (SOFT at audit)
------------------------------------

Partial globs — ``"catalog:item*"``, ``"catalog_*"``, ``"it*"``, ``"*:item"``
— are parsed as opaque *unmatchable* literals.  They will never match a tool
and :meth:`RouteMatcher.audit` emits ``CODE_PARTIAL_GLOB``.  Any entry that
resolves to zero tools at audit emits ``CODE_EMPTY_MATCH``; a qualified
entry whose group is unknown emits the more specific
``CODE_UNKNOWN_GROUP``.

Inert deny entries (LOUD at audit)
----------------------------------

An entry that resolves to nothing is **fail-closed** in ``allow_list`` — the
route merely exposes less than intended — but **fail-open** in ``deny_list``:
the tools the operator meant to carve out stay exposed.  The two directions
are opposites and are graded accordingly.

An inert ``deny_list`` entry is **LOUD** (``CODE_INERT_DENY``) when the
route's *net* surface still holds at least one tool that the entry's resource
segment would have matched.  Otherwise it stays SOFT.  The discriminator is
the **net** surface rather than the registry: a deny for a group that
``allow_list`` never exposed in the first place — an optional host component
that is not installed — removed nothing and leaked nothing, and it will bind
correctly the moment that group registers.  A blanket LOUD on every inert
deny would fire on that healthy configuration and train operators to ignore
the warning.

``CODE_INERT_DENY`` is never FATAL.  In the leaking case the named group is
*absent*, so its membership is unknowable and the surviving tools cannot be
attributed to it with certainty — an unrelated registered group may
legitimately reuse the resource name.  Refusing to boot on that ambiguity
would fail a correct configuration.  The finding's message therefore names
every surviving tool alongside the group it currently lives under (or
``flat``), which is what lets an operator distinguish a renamed group from a
coincidental name collision without re-reading the config.

The probe reuses this module's own matcher — exact name, or ``resource``
followed by the separator — and never implements glob semantics.  For a
partial glob the literal prefix before the first ``*`` is probed; a glob with
no literal prefix (``"*item"``) is unprobeable and stays SOFT.

Consumer contract (PR-6 / PR-9)
-------------------------------

::

    matcher = parse_lists(allow_list, deny_list, route_name=name)   # PR-6 build
    surface = ToolSurface.build(tool_names=..., group_members=...)  # PR-6 build
    selected: frozenset[str] = matcher.select(surface)              # PR-6 build
    findings: tuple[Finding, ...] = matcher.audit(surface,          # PR-9 startup
                                                  route_name=name)

:meth:`RouteMatcher.audit` grades an inert ``deny_list`` entry against the
route's *net* surface, so it calls :meth:`RouteMatcher.select` itself rather
than accepting the selection as an argument.  Callers that already hold a
selection must **not** pass it back in: a selection computed against a
different :class:`ToolSurface` would silently mis-grade the very finding whose
purpose is to not be silently wrong.  ``select`` remains the single source of
truth for the net surface, and ``audit`` is a boot-time-only call, so the
recomputation is free.

``parse_lists`` raises :class:`GrammarError` for every FATAL condition; the
caller (PR-6 during :meth:`RouteView.build`, PR-9 during Django
``checks``) is responsible for translating the exception into an
:class:`~django.core.checks.Error` and, per WI-5, re-raising as
:class:`~django.core.exceptions.ImproperlyConfigured` at boot so the process
actually stops.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = [
    "CODE_DENY_WILDCARD",
    "CODE_DOUBLE_UNDERSCORE",
    "CODE_EMPTY_ENTRY",
    "CODE_EMPTY_MATCH",
    "CODE_INERT_DENY",
    "CODE_NON_STRING_ENTRY",
    "CODE_PARTIAL_GLOB",
    "CODE_TOO_MANY_SEGMENTS",
    "CODE_UNKNOWN_GROUP",
    "Finding",
    "GrammarError",
    "KIND_BARE",
    "KIND_LITERAL_UNMATCHABLE",
    "KIND_QUALIFIED",
    "KIND_WILDCARD",
    "ParsedEntry",
    "RouteMatcher",
    "SEVERITY_FATAL",
    "SEVERITY_LOUD",
    "SEVERITY_SOFT",
    "ToolSurface",
    "parse_entry",
    "parse_lists",
]

# ---------------------------------------------------------------------------
# Severity + code constants
# ---------------------------------------------------------------------------

#: FATAL findings raise :class:`GrammarError`; PR-9 wraps into ``checks.Error``
#: and (WI-5) ``ImproperlyConfigured`` at boot.
SEVERITY_FATAL: str = "FATAL"

#: LOUD findings are returned by :meth:`RouteMatcher.audit`; PR-9 wraps into a
#: prominent ``checks.Warning``.  Boot succeeds — the configuration is coherent
#: but almost certainly not what the operator meant.
SEVERITY_LOUD: str = "LOUD"

#: SOFT findings are returned by :meth:`RouteMatcher.audit`; PR-9 wraps into
#: ``checks.Warning``.  They never stop the process.
SEVERITY_SOFT: str = "SOFT"

#: ``E1xx`` — FATAL grammar errors surfaced by ``parse_entry`` / ``parse_lists``.
CODE_NON_STRING_ENTRY: str = "E100"
CODE_DENY_WILDCARD: str = "E101"
CODE_DOUBLE_UNDERSCORE: str = "E102"
CODE_EMPTY_ENTRY: str = "E103"
CODE_TOO_MANY_SEGMENTS: str = "E105"

#: ``W1xx`` — audit findings surfaced by :meth:`RouteMatcher.audit`.  SOFT
#: unless escalated: an inert entry in a ``deny_list`` whose target survives on
#: the net surface is reported as :data:`CODE_INERT_DENY` at
#: :data:`SEVERITY_LOUD` instead of its SOFT code.
CODE_PARTIAL_GLOB: str = "W110"
CODE_EMPTY_MATCH: str = "W111"
CODE_UNKNOWN_GROUP: str = "W112"

#: LOUD — a ``deny_list`` entry matched nothing while tools its resource segment
#: would have matched remain exposed.  The carve-out is a silent no-op
#: (fail-open).  See the module docstring for why this is LOUD and not FATAL.
CODE_INERT_DENY: str = "W113"

#: Maximum survivors enumerated in a :data:`CODE_INERT_DENY` message.  Any
#: remainder is reported explicitly as ``+N more`` — a truncated list that reads
#: as complete is the same failure this finding exists to prevent.
_MAX_REPORTED_SURVIVORS: int = 5

# ---------------------------------------------------------------------------
# Parsed-entry kinds
# ---------------------------------------------------------------------------

KIND_WILDCARD: str = "wildcard"
KIND_BARE: str = "bare"
KIND_QUALIFIED: str = "qualified"
KIND_LITERAL_UNMATCHABLE: str = "literal_unmatchable"

# ---------------------------------------------------------------------------
# Grammar tokens (module-private)
# ---------------------------------------------------------------------------

_WILDCARD: str = "*"
_SEGMENT_SEPARATOR: str = ":"
_FORBIDDEN_SUBSTRING: str = "__"

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GrammarError(ValueError):
    """FATAL allow/deny grammar error raised during :func:`parse_lists`.

    PR-9's Django ``check`` catches this to emit a
    :class:`~django.core.checks.Error` and, per watch-item 5, re-raises as
    :class:`~django.core.exceptions.ImproperlyConfigured` at boot so the
    process actually stops rather than mounting a misconfigured view.

    Attributes:
        code: Stable identifier (e.g. ``"E101"``) suitable for use as a
            ``django.core.checks`` ID.
        entry: The raw grammar entry that triggered the error, when
            applicable.  ``None`` for list-level errors.
        list_name: ``"allow_list"`` or ``"deny_list"`` when the error was
            raised while parsing a specific list; ``None`` otherwise.
        route_name: The offending route's config key, when the caller
            supplied it via :func:`parse_lists`.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        entry: Any = None,
        list_name: str | None = None,
        route_name: str | None = None,
    ) -> None:
        """Store structured error context alongside the message."""
        self.code = code
        self.entry = entry
        self.list_name = list_name
        self.route_name = route_name
        parts: list[str] = [f"[{code}]"]
        if route_name is not None:
            parts.append(f"route={route_name!r}")
        if list_name is not None:
            parts.append(f"list={list_name}")
        prefix = " ".join(parts)
        super().__init__(f"{prefix}: {message}")


# ---------------------------------------------------------------------------
# Finding (SOFT audit result)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """A non-FATAL finding produced by :meth:`RouteMatcher.audit`.

    ``severity`` is :data:`SEVERITY_SOFT` or :data:`SEVERITY_LOUD` — it is
    *computed* from the entry, the list it came from, and the route's net
    surface, never hard-coded per code.  Consumers wrap the severity they are
    handed and must not re-derive it.

    Findings never come from :func:`parse_lists` — FATAL grammar errors
    raise :class:`GrammarError` instead.  PR-9 wraps each finding into a
    :class:`django.core.checks.Warning` at startup.
    """

    severity: str
    code: str
    message: str
    entry: str | None = None
    list_name: str | None = None
    route_name: str | None = None


# ---------------------------------------------------------------------------
# ParsedEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    """One parsed allow/deny entry.

    Attributes:
        raw: The exact string the operator wrote.  Used verbatim in
            :class:`Finding` messages.
        kind: One of :data:`KIND_WILDCARD`, :data:`KIND_BARE`,
            :data:`KIND_QUALIFIED`, or :data:`KIND_LITERAL_UNMATCHABLE`.
        label: For ``bare`` and ``qualified``, the primary label (the whole
            entry for ``bare``, the group name for ``qualified``).  Empty
            string for ``wildcard``.  Raw entry for ``literal_unmatchable``.
        resource: For ``qualified`` only, the segment after ``:``.  ``None``
            for every other kind.

    ``kind`` records only the string shape decided at parse time.  Whether
    a ``bare`` label is *actually* a group is determined at match time by
    :meth:`RouteMatcher.select` against the supplied :class:`ToolSurface`.
    """

    raw: str
    kind: str
    label: str
    resource: str | None = None


# ---------------------------------------------------------------------------
# ToolSurface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSurface:
    """Immutable snapshot of the tool registry that a matcher resolves against.

    PR-6 builds one :class:`ToolSurface` per :meth:`RouteView.build` call
    from the live :class:`~frisian_mcp.registry.ToolRegistry`.  A ``ToolSurface``
    intentionally does *not* import Django or the registry — the caller
    is responsible for extracting the snapshot.

    Attributes:
        tool_names: Every currently-registered tool name (including group
            dispatchers, hidden bundled tools, and flat tools alike).
        group_members: Mapping from *group dispatcher name* to the frozenset
            of tool names that group bundles.  Derived from
            :class:`~frisian_mcp.registry._ToolEntry` ``group_tool_names``
            values by PR-6.  Groups with zero members must be omitted —
            an unregistered group is one whose key is absent from this map.
        tool_separator: The single-character resource/action separator used
            by the host app; the value of :setting:`FRISIAN_MCP_TOOL_NAME_SEPARATOR`
            (default ``"_"``).  Used for prefix expansion in
            :meth:`RouteMatcher.select`.
    """

    tool_names: frozenset[str]
    group_members: Mapping[str, frozenset[str]]
    tool_separator: str = "_"

    @classmethod
    def build(
        cls,
        *,
        tool_names: Iterable[str],
        group_members: Mapping[str, Iterable[str]] | None = None,
        tool_separator: str = "_",
    ) -> ToolSurface:
        """Construct an immutable :class:`ToolSurface` from loose iterables.

        Accepts any iterable of names / group-member iterables and freezes
        them into ``frozenset`` values.  Passing an empty ``group_members``
        (or ``None``) yields a flat-surface tool set with no groups.
        """
        gm_frozen: dict[str, frozenset[str]] = {}
        for gname, members in (group_members or {}).items():
            gm_frozen[gname] = frozenset(members)
        return cls(
            tool_names=frozenset(tool_names),
            group_members=MappingProxyType(gm_frozen),
            tool_separator=tool_separator,
        )


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


def parse_entry(
    raw: Any,
    *,
    list_name: str,
    route_name: str | None = None,
) -> ParsedEntry:
    """Parse a single ``allow_list`` / ``deny_list`` entry.

    Args:
        raw: The unnormalized value read from settings.  Must be a string;
            other types raise :class:`GrammarError` with
            :data:`CODE_NON_STRING_ENTRY`.
        list_name: ``"allow_list"`` or ``"deny_list"``.  Determines whether
            ``"*"`` is FATAL (``"deny_list"``) or produces a wildcard entry.
        route_name: Optional route config key for error context.

    Returns:
        A :class:`ParsedEntry`.

    Raises:
        GrammarError: For any FATAL condition.  See the module docstring
            for the full FATAL enumeration.
    """
    if not isinstance(raw, str):
        raise GrammarError(
            CODE_NON_STRING_ENTRY,
            f"entry must be a string; got {type(raw).__name__}: {raw!r}",
            entry=raw,
            list_name=list_name,
            route_name=route_name,
        )

    entry = raw.strip()
    if not entry:
        raise GrammarError(
            CODE_EMPTY_ENTRY,
            "entry is empty or whitespace-only",
            entry=raw,
            list_name=list_name,
            route_name=route_name,
        )

    if _FORBIDDEN_SUBSTRING in entry:
        raise GrammarError(
            CODE_DOUBLE_UNDERSCORE,
            (
                f"entry {entry!r} contains '__' — double-underscore is reserved "
                "(collides with Django ORM lookup syntax and the hint-key channel)"
            ),
            entry=raw,
            list_name=list_name,
            route_name=route_name,
        )

    if entry == _WILDCARD:
        if list_name == "deny_list":
            raise GrammarError(
                CODE_DENY_WILDCARD,
                (
                    "'*' is not permitted in deny_list — wildcard deny against the "
                    "deny-all baseline is self-contradictory"
                ),
                entry=raw,
                list_name=list_name,
                route_name=route_name,
            )
        return ParsedEntry(raw=raw, kind=KIND_WILDCARD, label="")

    if _SEGMENT_SEPARATOR in entry:
        segments = entry.split(_SEGMENT_SEPARATOR)
        if len(segments) != 2:
            raise GrammarError(
                CODE_TOO_MANY_SEGMENTS,
                (
                    f"entry {entry!r} has more than one ':' separator; only "
                    "'group:resource' is valid (per-action route filtering is out of scope)"
                ),
                entry=raw,
                list_name=list_name,
                route_name=route_name,
            )
        group, resource = segments
        if not group or not resource:
            raise GrammarError(
                CODE_EMPTY_ENTRY,
                f"entry {entry!r} has an empty segment around ':'",
                entry=raw,
                list_name=list_name,
                route_name=route_name,
            )
        if _WILDCARD in group:
            # ``*:foo``, ``f*o:bar`` — wildcard/partial glob in the group segment.
            return ParsedEntry(raw=raw, kind=KIND_LITERAL_UNMATCHABLE, label=entry)
        if resource == _WILDCARD:
            # ``group:*`` — silent normalization to bare group.
            return ParsedEntry(raw=raw, kind=KIND_BARE, label=group)
        if _WILDCARD in resource:
            # ``group:a*``, ``group:*foo`` — partial glob.
            return ParsedEntry(raw=raw, kind=KIND_LITERAL_UNMATCHABLE, label=entry)
        return ParsedEntry(raw=raw, kind=KIND_QUALIFIED, label=group, resource=resource)

    if _WILDCARD in entry:
        # ``foo*``, ``*foo``, ``f*o`` — partial glob without a segment separator.
        return ParsedEntry(raw=raw, kind=KIND_LITERAL_UNMATCHABLE, label=entry)

    return ParsedEntry(raw=raw, kind=KIND_BARE, label=entry)


# ---------------------------------------------------------------------------
# RouteMatcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteMatcher:
    """Immutable matcher built from parsed ``allow_list`` / ``deny_list``.

    Consumers (PR-6 during :meth:`RouteView.build`) construct one matcher
    per route and reuse it across rebuilds of the same route.  Every method
    is a pure function over :class:`ToolSurface`.

    Attributes:
        allow: Parsed allow-list entries in the order the operator wrote
            them.
        deny: Parsed deny-list entries in the order the operator wrote
            them.
        allow_wildcard: ``True`` iff any allow entry is a wildcard.
            Exposed so PR-6 can short-circuit its "everything visible"
            fast path without re-scanning ``allow``.
        route_name: The route config key, used only for audit finding
            context.  ``None`` when the caller did not supply one.
    """

    allow: tuple[ParsedEntry, ...]
    deny: tuple[ParsedEntry, ...]
    allow_wildcard: bool
    route_name: str | None = None

    def select(self, surface: ToolSurface) -> frozenset[str]:
        """Return the frozenset of tool names visible for this route.

        Semantics: ``allow_union − deny_union``.  Both unions are computed
        by resolving each parsed entry against ``surface``; ``deny`` is
        evaluated after ``allow`` and always wins.
        """
        allowed: set[str] = set()
        for entry in self.allow:
            allowed |= self._resolve(entry, surface)
        if not allowed:
            return frozenset()
        denied: set[str] = set()
        for entry in self.deny:
            denied |= self._resolve(entry, surface)
        return frozenset(allowed - denied)

    def audit(
        self,
        surface: ToolSurface,
        *,
        route_name: str | None = None,
    ) -> tuple[Finding, ...]:
        """Return SOFT and LOUD findings for this route against ``surface``.

        The ``route_name`` argument overrides the matcher's own
        ``route_name`` when supplied — letting PR-9 emit route-specific
        findings even for matchers constructed without one.  Findings are
        deterministic in insertion order: ``allow_list`` first, then
        ``deny_list``; within each list, in the operator's declared order.

        Grading an inert ``deny_list`` entry requires the route's *net*
        surface, so :meth:`select` is called here rather than accepted as an
        argument — see the module docstring for why the net set is not part of
        this signature.
        """
        rn = route_name if route_name is not None else self.route_name
        net = self.select(surface)
        findings: list[Finding] = []
        for list_name, entries in (("allow_list", self.allow), ("deny_list", self.deny)):
            for entry in entries:
                finding = self._audit_entry(
                    entry, surface, list_name=list_name, route_name=rn, net=net
                )
                if finding is not None:
                    findings.append(finding)
        return tuple(findings)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, entry: ParsedEntry, surface: ToolSurface) -> set[str]:
        """Resolve one parsed entry to the set of tool names it matches."""
        if entry.kind == KIND_WILDCARD:
            return set(surface.tool_names)
        if entry.kind == KIND_LITERAL_UNMATCHABLE:
            return set()
        if entry.kind == KIND_QUALIFIED:
            return self._resolve_qualified(entry, surface)
        # KIND_BARE.
        return self._resolve_bare(entry, surface)

    @staticmethod
    def _resolve_bare(entry: ParsedEntry, surface: ToolSurface) -> set[str]:
        """Group-first, then exact-name / prefix expansion on the flat surface."""
        label = entry.label
        if label in surface.group_members:
            result: set[str] = set(surface.group_members[label])
            if label in surface.tool_names:
                # The dispatcher tool itself is also part of the group's visible surface.
                result.add(label)
            return result
        prefix = f"{label}{surface.tool_separator}"
        return {name for name in surface.tool_names if name == label or name.startswith(prefix)}

    @staticmethod
    def _resolve_qualified(entry: ParsedEntry, surface: ToolSurface) -> set[str]:
        """Match ``group:resource`` against ``surface.group_members[group]``."""
        group = entry.label
        resource = entry.resource
        if group not in surface.group_members or resource is None:
            return set()
        members = surface.group_members[group]
        prefix = f"{resource}{surface.tool_separator}"
        return {name for name in members if name == resource or name.startswith(prefix)}

    def _audit_entry(
        self,
        entry: ParsedEntry,
        surface: ToolSurface,
        *,
        list_name: str,
        route_name: str | None,
        net: frozenset[str],
    ) -> Finding | None:
        """Return a :class:`Finding` for one entry, or ``None`` if it matched tools.

        Inertness is classified first and severity is decided second, so that
        :data:`CODE_INERT_DENY` can supersede *any* of the three SOFT codes.  A
        qualified entry naming an absent group is inert via
        :data:`CODE_UNKNOWN_GROUP` and never reaches the empty-match branch —
        grading only the empty-match branch would miss the very case that leaks.
        """
        inert = self._classify_inert(entry, surface)
        if inert is None:
            return None
        code, message = inert
        if list_name == "deny_list":
            survivors = self._inert_deny_survivors(entry, surface, net)
            if survivors:
                return Finding(
                    severity=SEVERITY_LOUD,
                    code=CODE_INERT_DENY,
                    message=self._inert_deny_message(entry, surface, survivors),
                    entry=entry.raw,
                    list_name=list_name,
                    route_name=route_name,
                )
        return Finding(
            severity=SEVERITY_SOFT,
            code=code,
            message=message,
            entry=entry.raw,
            list_name=list_name,
            route_name=route_name,
        )

    def _classify_inert(
        self,
        entry: ParsedEntry,
        surface: ToolSurface,
    ) -> tuple[str, str] | None:
        """Return ``(code, message)`` when ``entry`` resolves to nothing, else ``None``.

        The returned code is the finding's severity-independent classification:
        it says *why* the entry is inert, not how bad that is.
        """
        if entry.kind == KIND_WILDCARD:
            return None
        if entry.kind == KIND_LITERAL_UNMATCHABLE:
            return (
                CODE_PARTIAL_GLOB,
                (f"entry {entry.raw!r} is an unsupported partial glob and will not match any tool"),
            )
        if entry.kind == KIND_QUALIFIED and entry.label not in surface.group_members:
            return (
                CODE_UNKNOWN_GROUP,
                (
                    f"qualified entry {entry.raw!r} references group {entry.label!r} "
                    "which is not a registered dispatch group"
                ),
            )
        if not self._resolve(entry, surface):
            return (CODE_EMPTY_MATCH, f"entry {entry.raw!r} matched no registered tools")
        return None

    @staticmethod
    def _probe_resource(entry: ParsedEntry, tool_separator: str) -> str | None:
        """Return the resource segment used to probe the net surface, or ``None``.

        This is *not* a matcher.  Glob semantics are out of scope for v1.1, so a
        partial glob contributes only its literal prefix before the first ``*``;
        a glob with no literal prefix (``"*item"``) is unprobeable and yields
        ``None``, leaving the entry at its SOFT classification.

        A trailing *tool_separator* is stripped from a glob's literal prefix so
        that the natural typo shape ``"device_*"`` probes as resource
        ``"device"`` rather than ``"device_"``.  Without the strip,
        :meth:`_inert_deny_survivors` re-appends the separator and probes for
        ``"device__"`` (double separator), which matches no real tool — silently
        leaving an inert deny graded SOFT instead of escalating to
        :data:`CODE_INERT_DENY` even though the tools the operator meant to deny
        are still exposed.  ``"device*"`` (no trailing separator) already worked;
        this makes the two spellings behave the same.
        """
        if entry.kind == KIND_QUALIFIED:
            return entry.resource
        if entry.kind == KIND_BARE:
            return entry.label
        if entry.kind == KIND_LITERAL_UNMATCHABLE:
            raw = entry.raw.strip()
            segment = raw.rsplit(_SEGMENT_SEPARATOR, 1)[-1]
            literal = segment.split(_WILDCARD, 1)[0]
            # Strip the separator as a literal SUFFIX (one occurrence), not with
            # ``rstrip``, which treats its argument as a character *set* and would
            # over-strip a multi-character separator (e.g. ``"::"`` chewing every
            # trailing ':').  ``tool_separator`` is documented single-character but
            # is not enforced, so this stays correct if a host configures a
            # multi-character FRISIAN_MCP_TOOL_NAME_SEPARATOR.
            if tool_separator and literal.endswith(tool_separator):
                literal = literal[: -len(tool_separator)]
            return literal or None
        return None

    def _inert_deny_survivors(
        self,
        entry: ParsedEntry,
        surface: ToolSurface,
        net: frozenset[str],
    ) -> tuple[str, ...]:
        """Return net-surface tools the inert deny entry's resource would have matched.

        Probed against the **net** set, never ``surface.tool_names``: a deny for
        something ``allow_list`` never exposed removed nothing and leaked
        nothing.  The predicate is this module's own — exact name, or resource
        plus separator — so the audit only claims matches the grammar could
        actually have made.
        """
        resource = self._probe_resource(entry, surface.tool_separator)
        if resource is None:
            return ()
        prefix = f"{resource}{surface.tool_separator}"
        return tuple(sorted(n for n in net if n == resource or n.startswith(prefix)))

    @staticmethod
    def _tool_location(name: str, surface: ToolSurface) -> str:
        """Describe where ``name`` currently lives, for :data:`CODE_INERT_DENY` messages.

        The discriminator an operator needs: survivors sitting ``flat`` or under
        a group they did not name mean the entry's group was renamed or removed;
        survivors under an unrelated registered group mean the entry and that
        group merely share a resource name.
        """
        if name in surface.group_members:
            return "group dispatcher"
        owners = sorted(g for g, members in surface.group_members.items() if name in members)
        if not owners:
            return "flat"
        if len(owners) == 1:
            return f"in group {owners[0]!r}"
        return "in groups " + ", ".join(repr(g) for g in owners)

    def _inert_deny_message(
        self,
        entry: ParsedEntry,
        surface: ToolSurface,
        survivors: tuple[str, ...],
    ) -> str:
        """Build the :data:`CODE_INERT_DENY` message, naming survivors and their groups."""
        shown = survivors[:_MAX_REPORTED_SURVIVORS]
        rendered = ", ".join(f"{name!r} ({self._tool_location(name, surface)})" for name in shown)
        omitted = len(survivors) - len(shown)
        if omitted:
            rendered += f", +{omitted} more"
        probed = self._probe_resource(entry, surface.tool_separator)
        return (
            f"deny_list entry {entry.raw!r} matched no tools, but "
            f"{len(survivors)} tool(s) its resource segment "
            f"{probed!r} would have matched are still exposed "
            f"on this route: {rendered}.  The carve-out is silently a no-op "
            "(fail-open).  Survivors that are 'flat' or sit under a group this "
            "entry does not name indicate the entry's group was renamed or "
            "removed; survivors under an unrelated registered group indicate the "
            "entry and that group merely share a resource name."
        )


# ---------------------------------------------------------------------------
# Top-level parse function
# ---------------------------------------------------------------------------


def parse_lists(
    allow_list: Any,
    deny_list: Any,
    *,
    route_name: str | None = None,
) -> RouteMatcher:
    """Parse and validate ``allow_list`` / ``deny_list`` into a :class:`RouteMatcher`.

    Args:
        allow_list: Iterable of grammar entries.  ``None`` is treated as an
            empty list (the deny-all baseline).  A bare string is FATAL —
            iterating character-by-character is almost never intended.
        deny_list: Iterable of grammar entries.  Same handling of ``None``
            and bare strings as ``allow_list``.
        route_name: Optional config key used for error and finding context.

    Returns:
        An immutable :class:`RouteMatcher`.

    Raises:
        GrammarError: For any FATAL condition (non-iterable list, non-string
            entry, empty entry, ``__`` in entry, ``*`` in deny_list, or
            three-segment entry).
    """
    allow_entries = _coerce_entry_iterable(
        allow_list, list_name="allow_list", route_name=route_name
    )
    deny_entries = _coerce_entry_iterable(deny_list, list_name="deny_list", route_name=route_name)
    allow_parsed = tuple(
        parse_entry(raw, list_name="allow_list", route_name=route_name) for raw in allow_entries
    )
    deny_parsed = tuple(
        parse_entry(raw, list_name="deny_list", route_name=route_name) for raw in deny_entries
    )
    allow_wildcard = any(e.kind == KIND_WILDCARD for e in allow_parsed)
    return RouteMatcher(
        allow=allow_parsed,
        deny=deny_parsed,
        allow_wildcard=allow_wildcard,
        route_name=route_name,
    )


def _coerce_entry_iterable(
    value: Any,
    *,
    list_name: str,
    route_name: str | None,
) -> tuple[Any, ...]:
    """Coerce a raw settings value into a tuple of entries or raise."""
    if value is None:
        return ()
    if isinstance(value, str):
        raise GrammarError(
            CODE_NON_STRING_ENTRY,
            (
                f"{list_name} must be a list of strings, not a bare string "
                f"({value!r} — iterating a bare string as characters is almost "
                "never intended)"
            ),
            entry=value,
            list_name=list_name,
            route_name=route_name,
        )
    try:
        return tuple(value)
    except TypeError as exc:
        raise GrammarError(
            CODE_NON_STRING_ENTRY,
            f"{list_name} must be iterable; got {type(value).__name__}: {value!r}",
            entry=value,
            list_name=list_name,
            route_name=route_name,
        ) from exc
