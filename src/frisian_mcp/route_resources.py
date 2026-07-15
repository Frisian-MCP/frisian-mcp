"""
RFC 9728 protected-resource resolution for the per-route gateway.

This module is the **single source of truth** for the question "which URL is the
OAuth-protected resource?"  Before V11-16 that derivation was open-coded in two
places (``contrib/oauth/views.py`` and ``contrib/oauth/admin.py``) and the 401
challenge hard-coded a third answer; they drifted, which is the whole defect.
Every caller now routes through here.

The defect this closes
----------------------

The old derivation was ``base + (FRISIAN_MCP_PROTECTED_PATH or
FRISIAN_MCP_PATH or "/mcp/")``.  When :setting:`FRISIAN_MCP_ROUTES` is set the
legacy ``FRISIAN_MCP_PATH`` mount is **skipped** (PR-7 JC1), so that metadata
advertised a ``resource`` URL which mapped to no route at all — or, the real
hazard, to the **open** ``default`` route.  An OAuth-discovering MCP client was
being told that the *unauthenticated* door was the protected resource.

Two invariants follow, and both are enforced here rather than at the call sites:

* **An anonymous-reachable route is never a protected resource.**  It is not
  protected, so advertising it as such is simply false.  Asking for its metadata
  yields *absence* (the view 404s) — the same honesty principle as WI-1 tool
  absence: we do not describe a door we are not guarding.
* **The resource a client is told about is the resource it is standing at.**
  Per-resource metadata (RFC 9728 §3 permits a path-suffixed metadata URL) means
  a client that gets a 401 from ``/elevated`` is handed ``/elevated``'s metadata,
  not some other route's.  Naming one blessed route for the whole server would
  merely trade one route-confusion for a quieter one.

Selection is deterministic
--------------------------

:func:`protected_resources` orders by tier ceiling ascending, then by name, so a
host with several authenticated routes gets a stable answer rather than one that
depends on dict iteration order.  :func:`default_protected_resource` — used for
the *bare* ``/.well-known/oauth-protected-resource``, i.e. clients that ignore
the ``resource_metadata`` pointer — therefore returns the **lowest-privilege**
authenticated route.  A client that will not say which door it wants is steered
to the least dangerous one; failing toward least privilege is the same lesson
that ``FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION`` learned the hard way.

Backward compatibility
----------------------

Hosts with no :setting:`FRISIAN_MCP_ROUTES` keep the legacy derivation verbatim,
including the static three-scope advertisement.  Nothing about a single-door
deployment changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from django.conf import settings

from frisian_mcp.registry import _TIER_RANK
from frisian_mcp.route_config import RouteConfig, parse_route_configs
from frisian_mcp.route_paths import normalize_route_path
from frisian_mcp.route_views import (
    LEGACY_ROUTE_NAME,
    resolve_route_ceiling,
    route_is_anonymous_reachable,
)

__all__ = [
    "METADATA_PREFIX",
    "ProtectedResource",
    "challenge_metadata_url",
    "default_protected_resource",
    "protected_resources",
    "resource_for_path",
    "scopes_for_ceiling",
]

#: Path of the RFC 9728 metadata endpoint, relative to the server base URL.
METADATA_PREFIX: str = "/.well-known/oauth-protected-resource"

#: Scopes advertised for each tier ceiling.  A ``read_write`` door must not
#: advertise ``mcp:admin`` — the old static list did, on every route.
_SCOPES_BY_CEILING: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "read": ("mcp:read",),
        "read_write": ("mcp:read", "mcp:write"),
        "admin": ("mcp:read", "mcp:write", "mcp:admin"),
    }
)

#: What the legacy (no-``FRISIAN_MCP_ROUTES``) mount advertises.  Frozen at the
#: pre-V11-16 value so single-door hosts see byte-identical metadata.
_LEGACY_SCOPES: tuple[str, ...] = ("mcp:read", "mcp:write", "mcp:admin")


def scopes_for_ceiling(ceiling: str | None) -> tuple[str, ...]:
    """Return the scopes advertised for a route whose tier ceiling is *ceiling*."""
    if ceiling is None:
        return _LEGACY_SCOPES
    return _SCOPES_BY_CEILING.get(ceiling, ("mcp:read",))


@dataclass(frozen=True)
class ProtectedResource:
    """A single OAuth-protected resource — one authenticated route."""

    name: str
    #: Canonical path: no leading or trailing slash (``"fixbrokenprod"``).  Used
    #: for *matching* a request path or metadata suffix to this resource.
    path: str
    #: Tier ceiling, or ``None`` for the legacy mount (which has no route config).
    ceiling: str | None
    #: What the ``resource`` URL actually renders as.  Normally identical to
    #: :attr:`path`, but the legacy mount preserves the operator's exact spelling
    #: — see :func:`_legacy_resource`.
    url_path: str = ""

    def __post_init__(self) -> None:
        """Default ``url_path`` to the canonical path when not given explicitly."""
        if not self.url_path:
            object.__setattr__(self, "url_path", self.path)

    @property
    def scopes(self) -> tuple[str, ...]:
        """Scopes this resource advertises, derived from its tier ceiling."""
        return scopes_for_ceiling(self.ceiling)

    def resource_url(self, base: str) -> str:
        """Return the RFC 9728 ``resource`` value — the URL clients call."""
        return f"{base.rstrip('/')}/{self.url_path}"

    def metadata_url(self, base: str) -> str:
        """Return the per-resource metadata URL for the 401 challenge."""
        return f"{base.rstrip('/')}{METADATA_PREFIX}/{self.path}"


def _legacy_resource() -> ProtectedResource:
    """
    Return the pre-per-route resource: ``PROTECTED_PATH`` or ``PATH`` or ``/mcp/``.

    The rendered URL preserves the operator's spelling **byte for byte**, trailing
    slash included.  ``resource`` is an audience identifier, compared literally by
    authorization servers and clients; normalising ``/mcp/`` to ``/mcp`` here would
    silently change the audience of every already-deployed single-door host and
    break token validation for a purely cosmetic gain.
    """
    raw = str(
        getattr(settings, "FRISIAN_MCP_PROTECTED_PATH", None)
        or getattr(settings, "FRISIAN_MCP_PATH", "/mcp/")
    )
    return ProtectedResource(
        name=LEGACY_ROUTE_NAME,
        path=raw.strip("/"),
        ceiling=None,
        url_path=raw.lstrip("/"),
    )


def _configured_routes() -> dict[str, RouteConfig] | None:
    """
    Return the parsed ``FRISIAN_MCP_ROUTES``, or ``None`` when the host has none.

    A malformed mapping is *not* this module's problem to report — the startup
    audit owns that — but it must not be allowed to turn a metadata request into
    a 500.  We fall back to legacy behaviour, which the audit will already have
    flagged FATAL at boot.
    """
    raw = getattr(settings, "FRISIAN_MCP_ROUTES", None)
    if not raw:
        return None
    try:
        return parse_route_configs(raw)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def protected_resources() -> tuple[ProtectedResource, ...]:
    """
    Return every protected resource, **lowest privilege first**.

    With :setting:`FRISIAN_MCP_ROUTES` set, that is every route an anonymous
    caller *cannot* reach — an open route is not a protected resource and is
    excluded, which is what keeps us from ever advertising the public door as
    the thing OAuth guards.

    With no routes configured, the single legacy mount is the resource, exactly
    as before.
    """
    routes = _configured_routes()
    if routes is None:
        return (_legacy_resource(),)

    resources = [
        ProtectedResource(
            name=cfg.name,
            path=normalize_route_path(cfg.path, route_name=cfg.name),
            ceiling=resolve_route_ceiling(cfg),
        )
        for cfg in routes.values()
        if not route_is_anonymous_reachable(cfg)
    ]
    # Deterministic: tier ascending, then name.  Never dict-iteration order.
    resources.sort(key=lambda r: (_TIER_RANK.get(r.ceiling or "read", 0), r.name))
    return tuple(resources)


def default_protected_resource() -> ProtectedResource | None:
    """
    Return the resource advertised at the **bare** metadata endpoint.

    This is the fallback for clients that do not follow the ``resource_metadata``
    pointer in the 401 challenge.  It is the lowest-privilege authenticated
    route; see the module docstring for why.

    Returns ``None`` when the host has routes but *none* of them require auth
    (an all-open, edge-gated posture).  There is genuinely no protected resource
    to name, and inventing one would be the original defect.
    """
    resources = protected_resources()
    return resources[0] if resources else None


def challenge_metadata_url(base: str, request_path: str) -> str:
    """
    Return the ``resource_metadata`` URL to advertise in a 401 challenge.

    This is what makes per-resource metadata *reachable*: a client only fetches
    the document we point it at.  When the 401 came from a protected route we
    point at **that route's** metadata, so the client is told about the door it
    is standing at rather than some other one.  A 401 from anywhere else (the
    legacy mount, a host view) falls back to the bare endpoint, which resolves
    to the lowest-privilege authenticated route.
    """
    resource = resource_for_path(request_path)
    if resource is not None:
        return resource.metadata_url(base)
    return f"{base.rstrip('/')}{METADATA_PREFIX}"


def resource_for_path(path: str) -> ProtectedResource | None:
    """
    Return the protected resource mounted at *path*, or ``None``.

    *path* may be a request path (``"/fixbrokenprod/"``), a metadata URL suffix
    (``"fixbrokenprod"``), or either with surrounding slashes — all canonicalise
    to the same key.  ``None`` means "not a protected resource", which covers
    both *unknown* and *known-but-open*.  Both must be indistinguishable to the
    caller: telling an anonymous prober which of its guesses named the public
    door would be a disclosure with no upside.
    """
    try:
        wanted = normalize_route_path(path)
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if not wanted:
        return None
    for resource in protected_resources():
        if resource.path == wanted:
            return resource
    return None
