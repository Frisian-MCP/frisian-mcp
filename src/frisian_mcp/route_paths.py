"""
Route path normalisation and collision validation for the frisian-mcp gateway.

This module owns watch-item 7: every ``path`` string in
:setting:`FRISIAN_MCP_ROUTES` is normalised to a canonical form, then checked
for two distinct classes of collision.  Every condition here is **FATAL** —
there is no SOFT path finding.  PR-9 catches :class:`RoutePathError`, emits a
:class:`django.core.checks.Error`, and re-raises as
:class:`~django.core.exceptions.ImproperlyConfigured` in ``AppConfig.ready()``
so a misconfigured mount stops the process rather than shipping a banner.

Normalisation
-------------

:func:`normalize_route_path` strips surrounding whitespace, strips leading and
trailing slashes, and collapses repeated internal slashes.  ``"/mcp"``,
``"mcp/"``, and ``"//mcp//"`` all canonicalise to ``"mcp"``.  Comparison
*always* happens on the canonical form; the raw string is never compared.

The canonical form is slash-stripped rather than slash-prefixed to match the
existing mount code, which builds its patterns from
``settings.FRISIAN_MCP_PATH.strip("/")``.

The two collision classes are deliberately asymmetric
-----------------------------------------------------

**Route vs. route** — only *exact* normalised equality is FATAL.  Shared-prefix
nesting between routes is legal and encouraged::

    default:  /mcp
    elevated: /mcp/elevated     # legal — longest match wins
    admin:    /mcp/admin        # legal

**Route vs. reserved** — conflict in *either* direction is FATAL.  A route may
neither nest under a package-reserved path (``oauth/authorize``) nor swallow one
(``backend`` swallows the reserved ``backend/healthcheck``).  A route that
normalises to the empty string is a greedy root mount that swallows every
reserved path at once, and gets its own diagnostic.

Both directions compare on **segment boundaries**, so ``oauthx`` does not
collide with the reserved ``oauth``.

Reserved paths are unconditional
--------------------------------

``oauth`` and ``.well-known`` are reserved whether or not
``frisian_mcp.contrib.oauth`` is currently in ``INSTALLED_APPS``.  Gating the
reservation on the app being installed would let a config that validates today
start failing the moment an operator enables OAuth — the failure would surface
as a shadowed token endpoint at runtime, not as a config error at boot.  These
are package-owned namespaces; a route may not claim them.

Deferral defence
----------------

v1.1 route paths are **literal strings**.  A path containing ``{`` or ``}`` is
FATAL (:data:`CODE_PATH_TEMPLATE`), which is where deferral #1 — no
``{optional_principal_id}`` templating, the path segment is a routing label and
never a credential — is mechanically enforced rather than merely documented.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "CODE_DUPLICATE_PATH",
    "CODE_EMPTY_PATH",
    "CODE_INVALID_PATH_TYPE",
    "CODE_PATH_TEMPLATE",
    "CODE_RESERVED_PATH",
    "RESERVED_ROUTE_PATHS",
    "RoutePathError",
    "normalize_route_path",
    "reserved_route_paths",
    "validate_route_paths",
]

# ---------------------------------------------------------------------------
# Stable error codes — ``E2xx`` is the path-validation block.  ``E1xx`` belongs
# to :mod:`frisian_mcp.route_grammar`; the ranges must not overlap because PR-9
# uses the code verbatim as a ``django.core.checks`` ID.
# ---------------------------------------------------------------------------

CODE_INVALID_PATH_TYPE: str = "E200"
CODE_EMPTY_PATH: str = "E201"
CODE_PATH_TEMPLATE: str = "E202"
CODE_DUPLICATE_PATH: str = "E203"
CODE_RESERVED_PATH: str = "E204"

# ---------------------------------------------------------------------------
# Reserved package paths (canonical, slash-stripped form)
# ---------------------------------------------------------------------------

#: Namespaces the package mounts itself.  A route may not match, nest under,
#: or swallow any of these.  ``oauth`` and ``.well-known`` are prefixes that
#: cover every endpoint beneath them (``oauth/token``, ``oauth/authorize``,
#: ``.well-known/oauth-protected-resource``, ...), so reserving the prefix
#: reserves the whole subtree via the segment-boundary check.
#:
#: ``register`` is the bare RFC 7591 JSON-404 stub installed by
#: ``apps._install_bare_register_url``.
RESERVED_ROUTE_PATHS: tuple[str, ...] = (".well-known", "oauth", "register")

_TEMPLATE_CHARS: tuple[str, str] = ("{", "}")
_SLASH_RUN: re.Pattern[str] = re.compile(r"/{2,}")


class RoutePathError(ValueError):
    """A FATAL route-path error.

    PR-9 catches this, emits a :class:`~django.core.checks.Error` keyed on
    :attr:`code`, and re-raises as
    :class:`~django.core.exceptions.ImproperlyConfigured` at boot per
    watch-item 5.

    Attributes:
        code: Stable identifier (e.g. ``"E204"``) suitable for use as a
            ``django.core.checks`` ID.
        path: The offending path, raw when the failure was a type/shape
            problem and canonical once normalisation has succeeded.
        route_name: The offending route's config key.
        conflict: The other party to a collision — the colliding route's name
            for :data:`CODE_DUPLICATE_PATH`, or the reserved path for
            :data:`CODE_RESERVED_PATH`.  ``None`` for single-path failures.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Any = None,
        route_name: str | None = None,
        conflict: str | None = None,
    ) -> None:
        """Store structured error context alongside the message."""
        self.code = code
        self.path = path
        self.route_name = route_name
        self.conflict = conflict
        parts: list[str] = [f"[{code}]"]
        if route_name is not None:
            parts.append(f"route={route_name!r}")
        super().__init__(f"{' '.join(parts)}: {message}")


def normalize_route_path(value: Any, *, route_name: str | None = None) -> str:
    """
    Return the canonical form of a route *value*, or raise :class:`RoutePathError`.

    Canonicalisation strips surrounding whitespace, strips leading and trailing
    slashes, and collapses repeated internal slashes, so ``"/mcp/"`` and
    ``"//mcp"`` both yield ``"mcp"``.

    A greedy root mount (``"/"``, ``""``, or whitespace) is FATAL rather than
    normalising to the empty string, because an empty prefix swallows every
    reserved path.

    Args:
        value: The raw ``path`` entry from a route config block.
        route_name: The route's config key, propagated into the raised error.

    Returns:
        The canonical, slash-stripped path (e.g. ``"mcp/elevated"``).

    Raises:
        RoutePathError: ``E200`` when *value* is not a string, ``E202`` when it
            contains templating braces, ``E201`` when it normalises to the
            empty string.
    """
    if not isinstance(value, str):
        raise RoutePathError(
            CODE_INVALID_PATH_TYPE,
            f"path must be a string (got {type(value).__name__}={value!r}).",
            path=value,
            route_name=route_name,
        )

    if any(char in value for char in _TEMPLATE_CHARS):
        raise RoutePathError(
            CODE_PATH_TEMPLATE,
            f"path={value!r} contains templating braces.  v1.1 route paths are "
            "literal strings — `{optional_principal_id}` templating is not "
            "supported.  A path segment is a routing label, never a credential.",
            path=value,
            route_name=route_name,
        )

    normalized = _SLASH_RUN.sub("/", value.strip()).strip("/")
    if not normalized:
        raise RoutePathError(
            CODE_EMPTY_PATH,
            f"path={value!r} normalises to the empty string, mounting the route "
            "greedily at the site root where it shadows every reserved package "
            f"path ({', '.join(RESERVED_ROUTE_PATHS)}).  Give the route a "
            "non-empty path.",
            path=value,
            route_name=route_name,
        )
    return normalized


def reserved_route_paths() -> tuple[str, ...]:
    """
    Return every reserved path in canonical form, including healthcheck paths.

    :data:`RESERVED_ROUTE_PATHS` is static; the healthcheck paths are read from
    ``settings.FRISIAN_MCP_HEALTHCHECK_PATHS`` at call time so a host that
    relocates its healthcheck still has that path protected from shadowing.

    Settings are read lazily inside the function body so importing this module
    never requires a configured Django settings object — the same reason
    :mod:`frisian_mcp.apps` defers its own imports.
    """
    from django.conf import settings  # pylint: disable=import-outside-toplevel

    from frisian_mcp.apps import (  # pylint: disable=import-outside-toplevel
        _DEFAULT_HEALTHCHECK_PATHS,
    )

    raw_healthchecks: Iterable[Any] = getattr(
        settings, "FRISIAN_MCP_HEALTHCHECK_PATHS", _DEFAULT_HEALTHCHECK_PATHS
    )
    healthchecks = tuple(
        cleaned
        for entry in raw_healthchecks
        if isinstance(entry, str) and (cleaned := _SLASH_RUN.sub("/", entry.strip()).strip("/"))
    )
    return tuple(dict.fromkeys(RESERVED_ROUTE_PATHS + healthchecks))


def _is_same_or_under(child: str, parent: str) -> bool:
    """
    Return ``True`` when *child* is *parent* or sits beneath it.

    The comparison respects segment boundaries, so ``oauthx`` is not considered
    to sit under ``oauth`` — only ``oauth`` itself and ``oauth/...`` are.
    """
    return child == parent or child.startswith(f"{parent}/")


def _reject_reserved(route_name: str, normalized: str) -> None:
    """Raise ``E204`` when *normalized* collides with a reserved path either way."""
    for reserved in reserved_route_paths():
        if _is_same_or_under(normalized, reserved):
            claim = (
                f"path={normalized!r} is reserved by the package."
                if normalized == reserved
                else f"path={normalized!r} nests under the reserved package path {reserved!r}."
            )
            raise RoutePathError(
                CODE_RESERVED_PATH,
                f"{claim}  Reserved paths are mounted by frisian-mcp itself and "
                "cannot be claimed by a route.",
                path=normalized,
                route_name=route_name,
                conflict=reserved,
            )
        if _is_same_or_under(reserved, normalized):
            raise RoutePathError(
                CODE_RESERVED_PATH,
                f"path={normalized!r} would swallow the reserved package path "
                f"{reserved!r}, shadowing it at the URL resolver.  Move the "
                "route to a path that does not contain a reserved path.",
                path=normalized,
                route_name=route_name,
                conflict=reserved,
            )


def validate_route_paths(configs: Mapping[str, Any]) -> dict[str, str]:
    """
    Normalise and validate every route path, returning the canonical mapping.

    *configs* maps a route name to any object exposing a ``path`` attribute —
    in practice :class:`frisian_mcp.route_config.RouteConfig`.  PR-6 mounts from
    the returned canonical paths rather than re-deriving them, so mounting and
    validation cannot drift.

    Exact duplicates between two routes are FATAL.  Shared-prefix nesting
    between routes (``mcp`` and ``mcp/elevated``) is explicitly legal and is
    never flagged: the URL resolver picks the longest match.

    Args:
        configs: Route name to parsed route config.

    Returns:
        Route name to canonical, slash-stripped path, in input order.

    Raises:
        RoutePathError: On the first FATAL condition, in deterministic order —
            routes are visited sorted by name so a config with several distinct
            errors always reports the same one first.
    """
    canonical: dict[str, str] = {}
    seen: dict[str, str] = {}

    for route_name in sorted(configs):
        raw_path = getattr(configs[route_name], "path", None)
        normalized = normalize_route_path(raw_path, route_name=route_name)
        _reject_reserved(route_name, normalized)

        if (owner := seen.get(normalized)) is not None:
            raise RoutePathError(
                CODE_DUPLICATE_PATH,
                f"path={normalized!r} is already claimed by route {owner!r}.  "
                "Two routes cannot resolve to the same normalised path.  Note "
                "that shared-prefix nesting IS allowed — 'mcp' and "
                "'mcp/elevated' may coexist; only exact matches collide.",
                path=normalized,
                route_name=route_name,
                conflict=owner,
            )
        seen[normalized] = route_name
        canonical[route_name] = normalized

    return canonical
