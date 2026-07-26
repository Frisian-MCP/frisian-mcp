r"""Layered opt-in resolution for token-usage reporting (TUR-3).

Implements the three-layer precedence from the TUR-1 spec (sec 4/5) as a pure
function of ``(global setting, system policy, request)`` with no side effects,
so the full truth-table can be exercised directly (TUR-5).

Layers, lowest to highest authority:

* **L0 global default** -- ``FRISIAN_MCP_USAGE_REPORTING`` (bool, default
  ``False``).  Ships the feature OFF for every existing consumer.
* **L1 system policy** -- ``FRISIAN_MCP_USAGE_REPORTING_POLICY``
  (``"allow" | "deny" | None``).  ``"deny"`` forces the feature OFF and is
  **authoritative**: a per-request flag can never re-enable a denied system.
  ``"allow"`` turns it on but still lets a request opt out.  ``None`` (or any
  unrecognised value) defers to the request / global layers.
* **L2 per-request** -- transport-level tri-state (on / off / unset) read from
  the ``X-Frisian-MCP-Usage`` request header (WSGI
  ``HTTP_X_FRISIAN_MCP_USAGE``) or the ``usage`` query parameter.  The header
  wins over the query parameter.

Resolution algorithm (TUR-1 sec 4)::

    if policy == "deny":  return False        # authoritative -- locked OFF
    if request_flag is not None:  return request_flag
    if policy == "allow": return True
    return bool(global_default)

Truth table (system x request), the shared source of truth::

    System \\ Request | unset | on              | off
    deny             | OFF   | OFF (deny wins) | OFF
    allow            | ON    | ON              | OFF (opts out)
    unset            | dflt  | ON              | OFF
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

#: Setting names (L0 / L1).  Exposed as constants so TUR-4 and the TUR-5 test
#: matrix reference the exact same identifiers rather than string literals.
USAGE_REPORTING_SETTING = "FRISIAN_MCP_USAGE_REPORTING"
USAGE_POLICY_SETTING = "FRISIAN_MCP_USAGE_REPORTING_POLICY"

#: System-policy values (L1).
POLICY_ALLOW = "allow"
POLICY_DENY = "deny"

#: Per-request transport names (L2).  ``USAGE_HEADER`` is the human-facing HTTP
#: header; ``USAGE_HEADER_META`` is the WSGI/``request.META`` key Django exposes
#: it under.  ``USAGE_QUERY_PARAM`` has no leading underscore by design.
USAGE_HEADER = "X-Frisian-MCP-Usage"
USAGE_HEADER_META = "HTTP_X_FRISIAN_MCP_USAGE"
USAGE_QUERY_PARAM = "usage"

#: Content-visible usage line (TUR-11/TUR-12): a SEPARATE opt-in surface that
#: chooses *where* usage is surfaced once the master gate above has already
#: turned it ON.  It has an L0 setting and an L2 request flag, but **no**
#: allow/deny system policy of its own -- it can never turn usage on, so the
#: single deny-first master resolver stays the only authority.
USAGE_IN_CONTENT_SETTING = "FRISIAN_MCP_USAGE_IN_CONTENT"
USAGE_CONTENT_HEADER = "X-Frisian-MCP-Usage-Content"
USAGE_CONTENT_HEADER_META = "HTTP_X_FRISIAN_MCP_USAGE_CONTENT"
USAGE_CONTENT_QUERY_PARAM = "usage_content"

#: Case-insensitive truthy / falsy request-flag tokens (TUR-1 sec 5).  Anything
#: outside these sets -- including the empty string and arbitrary garbage --
#: parses to ``None`` (unset) and can therefore never *enable* the feature.
_TRUE_TOKENS = frozenset({"on", "1", "true", "yes"})
_FALSE_TOKENS = frozenset({"off", "0", "false", "no"})


def parse_flag_value(raw: Any) -> bool | None:
    """Parse a single request-flag token into ``True`` / ``False`` / ``None``.

    ``None`` (unset) is returned for a missing value or any token not in the
    recognised truthy/falsy sets, so a malformed flag is treated as *absent*
    and never silently enables reporting.
    """
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def parse_request_flag(request: Any) -> bool | None:
    """Return the per-request (L2) tri-state flag for *request*.

    Reads the ``X-Frisian-MCP-Usage`` header first; when the header yields a
    definite on/off value it wins over the query parameter.  Falls back to the
    ``?usage=`` query parameter otherwise.  Returns ``None`` when neither layer
    supplies a recognised value.  Never raises -- a request object missing
    ``META``/``GET`` simply resolves to ``None``.
    """
    meta_get = getattr(getattr(request, "META", None), "get", None)
    if callable(meta_get):
        header_flag = parse_flag_value(meta_get(USAGE_HEADER_META))
        if header_flag is not None:
            return header_flag

    query_get = getattr(getattr(request, "GET", None), "get", None)
    if callable(query_get):
        return parse_flag_value(query_get(USAGE_QUERY_PARAM))
    return None


def _coerce_bool_setting(raw: Any) -> bool:
    """Coerce a boolean-intent settings value, treating config-confused strings safely.

    A real ``bool`` is used as-is.  A ``str`` is routed through
    :func:`parse_flag_value` so a config-type-confused value such as
    ``"false"`` / ``"no"`` / ``"0"`` -- all Python-truthy as bare strings --
    resolves to ``False`` rather than silently turning the flag ON (an
    off-by-default footgun); an unrecognized string likewise resolves to
    ``False``.  Any other type falls back to ``bool()``.

    Shared by both boolean opt-in defaults (:func:`_global_default` and
    :func:`_in_content_default`) so their parsing can never drift.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return parse_flag_value(raw) is True
    return bool(raw)


def _global_default() -> bool:
    """Return the L0 global default (``FRISIAN_MCP_USAGE_REPORTING``).

    Uses :func:`_coerce_bool_setting`, so the global layer's token parsing is
    identical to the request layer (single source of truth) and a truthy
    config string such as ``"false"`` can never silently enable reporting.
    """
    return _coerce_bool_setting(getattr(settings, USAGE_REPORTING_SETTING, False))


def resolve_system_policy() -> str | None:
    """Return the normalised L1 system policy: ``"allow"``, ``"deny"``, or ``None``.

    The raw setting is stripped and lower-cased; only exact ``"allow"`` /
    ``"deny"`` are honoured.  Any other value -- including ``None`` or a
    misconfigured string -- resolves to ``None`` (defer), which can never
    force the feature on and never overrides a request/global decision.
    """
    raw = getattr(settings, USAGE_POLICY_SETTING, None)
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token == POLICY_DENY:
            return POLICY_DENY
        if token == POLICY_ALLOW:
            return POLICY_ALLOW
    return None


def resolve_usage_reporting(request: Any) -> bool:
    """Return the single ``usage_enabled`` boolean for *request*.

    Applies the L1 -> L2 -> L0 precedence with system-``deny`` authoritative.
    Pure with respect to its inputs (Django settings + the request's header /
    query) and free of side effects, so TUR-5 can drive the full matrix.
    """
    policy = resolve_system_policy()
    if policy == POLICY_DENY:
        # Authoritative: a denied system can never be re-enabled by a request.
        return False

    request_flag = parse_request_flag(request)
    if request_flag is not None:
        return request_flag

    if policy == POLICY_ALLOW:
        return True

    return _global_default()


def parse_content_request_flag(request: Any) -> bool | None:
    """Return the per-request tri-state flag for the content-visible usage line.

    Mirrors :func:`parse_request_flag` exactly but reads the content-surface
    transport names: the ``X-Frisian-MCP-Usage-Content`` header first (wins on a
    definite on/off value), then the ``?usage_content=`` query parameter.
    Returns ``None`` when neither supplies a recognised value.  Never raises.
    """
    meta_get = getattr(getattr(request, "META", None), "get", None)
    if callable(meta_get):
        header_flag = parse_flag_value(meta_get(USAGE_CONTENT_HEADER_META))
        if header_flag is not None:
            return header_flag

    query_get = getattr(getattr(request, "GET", None), "get", None)
    if callable(query_get):
        return parse_flag_value(query_get(USAGE_CONTENT_QUERY_PARAM))
    return None


def _in_content_default() -> bool:
    """Return the L0 default for the content-visible line (``FRISIAN_MCP_USAGE_IN_CONTENT``).

    Uses :func:`_coerce_bool_setting`, so a truthy config string such as
    ``"false"`` can never silently enable the content line (same footgun guard
    as the master global default).
    """
    return _coerce_bool_setting(getattr(settings, USAGE_IN_CONTENT_SETTING, False))


def resolve_usage_in_content(request: Any) -> bool:
    """Return whether the content-visible usage line should be emitted for *request*.

    This is the SUBORDINATE surface gate (TUR-11): it only chooses *where* usage
    is surfaced and is consulted **after** :func:`resolve_usage_reporting` has
    already resolved the master decision to ON.  It has no allow/deny policy of
    its own, so it can never turn usage on -- the single deny-first master
    resolver remains the only authority over whether usage is computed at all.

    An explicit per-request flag (header > query) wins; otherwise the L0
    ``FRISIAN_MCP_USAGE_IN_CONTENT`` setting (default OFF) applies.
    """
    request_flag = parse_content_request_flag(request)
    if request_flag is not None:
        return request_flag
    return _in_content_default()
