"""
Per-route configuration schema for the frisian-mcp gateway.

This module owns the *parsed* shape of ``settings.FRISIAN_MCP_ROUTES``.  It
introduces the deliberate split between the two flags that used to share the
``auto_register`` name in earlier design drafts:

``auto_discover``
    Tool discovery.  When ``True``, newly-discovered DRF ViewSets automatically
    join this route's exposed surface (and, in combination with
    ``allow_list: ["*"]``, are picked up on the next process start).  Secure
    default: ``False``.

``auto_register``
    Client / agent self-enrollment.  Semantically aligned with the existing
    ``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER`` setting.  Enabling this on a
    ``default`` (anonymous-reachable) route lets an unknown client register
    itself on first contact.  Secure default: ``False``.

The two flags carry different severities in the startup audit — see the
``PR-9`` config-audit for the FATAL/LOUD/SOFT matrix.  They are never merged
into a single flag by this parser: an old single-name entry (e.g. dropping in
just ``auto_register: true`` intending "tool discovery") does **not** work
silently.  The parser raises :class:`~django.core.exceptions.ImproperlyConfigured`
on unknown keys and always names both flags in the diagnostic so the caller
picks the right one.

This module owns the schema shape and the canonical tier vocabulary.
Downstream PRs consume the resulting :class:`RouteConfig` instances:

* PR-4 validates the allow/deny grammar entries.
* PR-5 validates and normalises :attr:`RouteConfig.path`.
* PR-6 builds ``RouteView`` from the parsed :class:`RouteConfig`.
* PR-9 audits severity-tier warnings against the parsed flags.

Tier vocabulary (PR-3)
----------------------

The outer :setting:`FRISIAN_MCP_ROUTES` mapping is keyed by one of the three
fixed :data:`TIER_KEYS` — ``default``, ``elevated``, ``admin`` — and any
other outer key is a hard error.  Absent tiers are simply not mounted.

Route permission-tier ceilings (:attr:`RouteConfig.highest_tier`) draw from
the canonical :data:`PERMISSION_TIERS` set (``read`` < ``read_write`` <
``admin``) via :func:`canonical_permission_tier`.  No synonyms are accepted
anywhere — the helper rejects ``readonly``, ``rw``, ``read-only``, case
variants, and any other alias so the parser cannot silently drift from the
runtime tier comparisons in :mod:`frisian_mcp.registry`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from frisian_mcp.registry import _TIER_RANK, _VALID_PERMISSION_TIERS

__all__ = [
    "PERMISSION_TIERS",
    "PERMISSION_TIER_RANK",
    "ROUTE_CONFIG_KEYS",
    "RouteConfig",
    "TIER_KEYS",
    "canonical_permission_tier",
    "parse_route_config",
    "parse_route_configs",
]

#: The exhaustive set of top-level keys accepted inside a single route's
#: config block.  Unknown keys cause a hard rejection at parse time —
#: extending this schema requires a schema-level change and a matching
#: audit entry in :mod:`frisian_mcp.checks` / the startup audit pass.
ROUTE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "path",
        "highest_tier",
        "auto_discover",
        "auto_register",
        "allow_list",
        "deny_list",
    }
)

#: The three fixed tier keys accepted at the top level of
#: :setting:`FRISIAN_MCP_ROUTES`.  A tier absent from config is not mounted
#: (pure absence — see plan v2 "Route existence = pure absence").  Any other
#: outer key is rejected at parse.
TIER_KEYS: frozenset[str] = frozenset({"default", "elevated", "admin"})

#: Canonical permission-tier names in strict order.  Public re-export of the
#: canonical set already used by :mod:`frisian_mcp.registry` — kept in a
#: single source of truth so the config-parse surface and the runtime tier
#: comparisons cannot drift.  No synonyms (``readonly``, ``rw``,
#: ``read-only``) are accepted anywhere.
PERMISSION_TIER_RANK: dict[str, int] = dict(_TIER_RANK)
PERMISSION_TIERS: frozenset[str] = _VALID_PERMISSION_TIERS


def canonical_permission_tier(value: Any, *, field_name: str = "highest_tier") -> str:
    """
    Return *value* when it is a canonical permission-tier name, else raise.

    Canonical names are ``read`` < ``read_write`` < ``admin``.  Any synonym
    (``readonly``, ``rw``, ``read-only``, ``RO``, ``write``, case variants,
    surrounding whitespace) is rejected — this parser does not silently
    normalise, so an operator who used a synonym gets a hard error naming
    the canonical set.

    Args:
        value: Raw value read from config.  Must already be a ``str``.
        field_name: Name of the field being validated, used in the error
            message (defaults to ``highest_tier`` which is the only current
            call site inside :mod:`frisian_mcp.route_config`).

    Returns:
        The canonical tier name (identical to *value* when it validates).

    Raises:
        :exc:`~django.core.exceptions.ImproperlyConfigured`: When *value* is
            not a canonical permission-tier name.

    """
    if not isinstance(value, str) or value not in PERMISSION_TIERS:
        canonical = ", ".join(sorted(PERMISSION_TIERS, key=PERMISSION_TIER_RANK.__getitem__))
        raise ImproperlyConfigured(
            f"{field_name}={value!r} is not a canonical permission tier.  "
            f"Valid values in strict order are: {canonical}.  No synonyms "
            "(readonly, rw, read-only, RO, write, case variants) are "
            "accepted — see plan v2 §Tier model."
        )
    return value


@dataclass(frozen=True)
class RouteConfig:
    """
    Parsed configuration for a single route in ``FRISIAN_MCP_ROUTES``.

    Instances are immutable; downstream PRs treat this as a value type.
    Field-level validation stays intentionally shallow at this stage — the
    parser only enforces *shape* (correct keys, correct primitive types),
    leaving richer semantic validation to later PRs so responsibility does
    not blur across the config surface.

    Attributes:
        name: Route key from the top-level ``FRISIAN_MCP_ROUTES`` mapping —
            one of :data:`TIER_KEYS` (``"default"``, ``"elevated"``,
            ``"admin"``).  Not written by the caller inside the per-route
            block — the parser copies it in from the outer key so the parsed
            value can travel independently.
        path: URL path at which the route mounts.  Kept as the raw string
            here; PR-5 normalises leading/trailing slashes and rejects
            collisions with reserved paths.
        highest_tier: Canonical permission-tier ceiling for the route
            (``read`` / ``read_write`` / ``admin``) or ``None``.  Synonyms
            (``readonly``, ``rw``, ``read-only``) are rejected at parse.
        auto_discover: Tool-discovery flag (see module docstring).  Secure
            default ``False``.
        auto_register: Client self-enrollment flag (see module docstring).
            Secure default ``False``.
        allow_list: Raw entries permitting exposure.  Grammar is validated
            by PR-4.
        deny_list: Raw entries denying exposure.  Grammar is validated
            by PR-4; ``"*"`` is rejected as an entry (see plan v2).

    """

    name: str
    path: str
    highest_tier: str | None = None
    auto_discover: bool = False
    auto_register: bool = False
    allow_list: tuple[str, ...] = field(default_factory=tuple)
    deny_list: tuple[str, ...] = field(default_factory=tuple)


def _require_bool(name: str, key: str, value: Any) -> bool:
    """Return *value* if it is a strict :class:`bool`, else raise."""
    if not isinstance(value, bool):
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES[{name!r}][{key!r}] must be a bool "
            f"(got {type(value).__name__}={value!r}).  "
            "The two flags `auto_discover` (tool discovery) and "
            "`auto_register` (client self-enrollment) are distinct — see "
            "the frisian_mcp.route_config module docstring."
        )
    return value


def _require_str_list(name: str, key: str, value: Any) -> tuple[str, ...]:
    """Return *value* as an immutable tuple of strings, else raise."""
    if not isinstance(value, (list, tuple)):
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES[{name!r}][{key!r}] must be a list of "
            f"strings (got {type(value).__name__}={value!r})."
        )
    for entry in value:
        if not isinstance(entry, str):
            raise ImproperlyConfigured(
                f"FRISIAN_MCP_ROUTES[{name!r}][{key!r}] must contain "
                f"strings only (found {type(entry).__name__}={entry!r})."
            )
    return tuple(value)


def parse_route_config(name: str, raw: Any) -> RouteConfig:
    """
    Parse a single route's raw config block into a :class:`RouteConfig`.

    Args:
        name: Route key from the outer :setting:`FRISIAN_MCP_ROUTES` mapping.
        raw: Raw value at ``FRISIAN_MCP_ROUTES[name]``.  Must be a mapping.

    Returns:
        The parsed :class:`RouteConfig`.

    Raises:
        :exc:`~django.core.exceptions.ImproperlyConfigured`: When *raw* is
            not a mapping, contains unknown keys (including any legacy
            single-name conflation of the two flags), omits a required key
            (``path``), or supplies a value of the wrong primitive type.
            The error message always names both ``auto_discover`` and
            ``auto_register`` when the offending key is a plausible
            near-miss for either.

    """
    if not isinstance(name, str) or not name:
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES: route names must be non-empty strings "
            f"(got {type(name).__name__}={name!r})."
        )
    if name not in TIER_KEYS:
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES: unknown tier key {name!r}.  Valid tier "
            f"keys are {sorted(TIER_KEYS)!r}.  A tier absent from config "
            "is simply not mounted — there is no need to declare a "
            "placeholder for tiers you do not use."
        )

    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES[{name!r}] must be a mapping (got {type(raw).__name__}={raw!r})."
        )

    unknown = set(raw) - ROUTE_CONFIG_KEYS
    if unknown:
        sorted_unknown = sorted(unknown)
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES[{name!r}] contains unknown key(s): "
            f"{sorted_unknown!r}.  Valid keys are "
            f"{sorted(ROUTE_CONFIG_KEYS)!r}.  Note that `auto_discover` "
            "(tool discovery) and `auto_register` (client self-enrollment) "
            "are two distinct flags — see the frisian_mcp.route_config "
            "module docstring for the difference."
        )

    if "path" not in raw:
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES[{name!r}] is missing the required 'path' key."
        )

    path = raw["path"]
    if not isinstance(path, str) or not path:
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES[{name!r}]['path'] must be a non-empty "
            f"string (got {type(path).__name__}={path!r})."
        )

    highest_tier: str | None = raw.get("highest_tier")
    if highest_tier is not None:
        if not isinstance(highest_tier, str):
            raise ImproperlyConfigured(
                f"FRISIAN_MCP_ROUTES[{name!r}]['highest_tier'] must be a "
                f"string or None (got {type(highest_tier).__name__}={highest_tier!r})."
            )
        highest_tier = canonical_permission_tier(
            highest_tier, field_name=f"FRISIAN_MCP_ROUTES[{name!r}]['highest_tier']"
        )

    auto_discover = _require_bool(name, "auto_discover", raw.get("auto_discover", False))
    auto_register = _require_bool(name, "auto_register", raw.get("auto_register", False))

    allow_list = _require_str_list(name, "allow_list", raw.get("allow_list", ()))
    deny_list = _require_str_list(name, "deny_list", raw.get("deny_list", ()))

    return RouteConfig(
        name=name,
        path=path,
        highest_tier=highest_tier,
        auto_discover=auto_discover,
        auto_register=auto_register,
        allow_list=allow_list,
        deny_list=deny_list,
    )


def parse_route_configs(raw: Any) -> dict[str, RouteConfig]:
    """
    Parse a full ``FRISIAN_MCP_ROUTES`` mapping into :class:`RouteConfig` instances.

    Args:
        raw: The value at ``settings.FRISIAN_MCP_ROUTES``.  Must be a mapping
            keyed by route name.

    Returns:
        A dict mapping the route name to its parsed :class:`RouteConfig`.

    Raises:
        :exc:`~django.core.exceptions.ImproperlyConfigured`: When *raw* is
            not a mapping, or when any nested route block fails validation
            (see :func:`parse_route_config`).

    """
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured(
            f"FRISIAN_MCP_ROUTES must be a mapping keyed by route name "
            f"(got {type(raw).__name__}={raw!r})."
        )
    return {name: parse_route_config(name, block) for name, block in raw.items()}
