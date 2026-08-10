"""
Django system checks for frisian-mcp configuration safety.

Registered checks
-----------------

``frisian_mcp.W001``
    Warns when ``DEBUG=False`` and the gateway has no permission classes
    configured (``FRISIAN_MCP_PERMISSION_CLASSES`` is missing or empty), so
    the MCP endpoint is reachable by unauthenticated callers in production.
    Operators who *do* want a public gateway must set
    ``FRISIAN_MCP_ALLOW_UNAUTHENTICATED = True`` to silence the warning —
    that is the explicit opt-in.

``frisian_mcp.W002``
    Warns when any key in ``FRISIAN_MCP_API_KEYS`` does not look like a
    64-character lowercase hex string (the expected HMAC-SHA256 digest
    format), or when any value is not a valid permission tier.  Raw plaintext
    keys in settings are a security risk — if settings are captured by
    error-tracking or logging, the raw secret is directly usable as a Bearer
    token.  Use ``python manage.py mcp_hash_api_key <raw-key>`` to generate
    the correct digest.  Valid tier values are ``read``, ``read_write``, and
    ``admin``.

``frisian_mcp.W003``
    Warns when ``FRISIAN_MCP_SERVICE_ACCOUNT_USER`` is set in a non-DEBUG
    environment.  This setting substitutes the named Django user on every
    synthetic inner request for anonymous MCP callers, so if the account is
    privileged (``is_staff`` or ``is_superuser``), unauthenticated callers
    receive that user's host-app Django permissions — potentially exceeding
    the MCP tier gate.  Run ``manage.py mcp_doctor --security`` for a
    detailed privilege audit of the named account.

``frisian_mcp.E002``  (retired — constant retained for backward compat)
    This check was removed.  OAuth clients without a linked Django user are
    handled as service principals (``_mcp_is_service_principal=True``) and
    bypass capability filtering; the tier is the sole gate.  Clients with a
    linked user receive full ObjectPermission filtering.  No configuration
    gap exists for E002 to guard against.

``frisian_mcp.E003``
    Error when ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True`` and a
    ``@mcp_dispatcher`` action is a non-CRUD action (not ``list``,
    ``create``, ``retrieve``, ``update``, ``partial_update``, or ``destroy``)
    without a ``backend_action`` annotation.  The permission adapter cannot
    derive the required Django permission verb for unannotated custom actions.

``frisian_mcp.E007``
    Error when ``FRISIAN_MCP_UNAUTHENTICATED_TIER`` is set to a value that is
    neither a recognised tier nor the canonical ``"none"``.  The runtime denies
    on such a value (H7 — it used to degrade silently to ``read``), so without
    this check a typo would lock anonymous callers out with nothing explaining
    why.  An absent setting and an explicit ``None``/``"none"`` are both
    silent: absence is the documented ``read`` default, and ``none`` is a
    deliberate lockdown.

``frisian_mcp.E008``
    Error when a **group** dispatcher is registered without
    ``group_tool_names``.  That field feeds two security mechanisms in
    ``views.py`` (the caller-supplied-``resource`` membership gate and the
    audit resolver), and both fail closed on a falsy set — so the host is safe
    but uninformed, and silently loses ``@mcp_heavy`` negotiation and the
    dispatcher-routed lean write envelope.  ``@mcp_dispatcher`` classes are out
    of scope: they carry ``dispatcher_meta`` and legitimately have no
    membership set.  Silence with ``SILENCED_SYSTEM_CHECKS`` if a
    membership-less dispatcher is deliberate.

``frisian_mcp.W012``
    Warns (LOUD) when ``frisian_mcp.contrib.oauth`` is installed but
    ``FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False``.  That flag 404s the
    RFC 8414 / RFC 9728 well-known metadata endpoints *and* strips the
    ``resource_metadata`` pointer from ``WWW-Authenticate`` challenges, so
    discovery-first (spec-correct) MCP clients cannot locate the
    authorization server and the OAuth handshake silently fails — only
    clients with hard-coded endpoint URLs keep working.  Hiding metadata is
    not an authentication gate; walk-up registration is governed by
    ``FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER`` and
    ``FRISIAN_MCP_OAUTH_REGISTRATION_OPEN``.

``frisian_mcp.W013``
    Warns (LOUD) when ``FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT`` is set but not
    parseable as ``"N/period"``.  The runtime limiter fails open on a
    malformed value, so an operator who *configured* brute-force throttling
    is running without it — the "disabled looks like enabled" shape.  Boot
    validation and request-time parsing share one parser
    (:func:`frisian_mcp.contrib.oauth._rate_limiting.parse_rate_limit`) so
    they cannot drift.

``frisian_mcp.W014``
    Warns when ``FRISIAN_MCP_USAGE_REPORTING_POLICY`` is set to a value the
    token-usage resolver will not honour as ``"allow"``/``"deny"`` — including
    a non-string (``b"deny"``, a list) or a dirty string (a ``"deny"`` with a
    trailing NUL or surrounding junk).  The resolver treats such a value as
    *unset* (defer), so a deny-intended
    misconfiguration silently **fails open**: a per-request flag can then
    enable reporting the operator meant to forbid.  The check reuses
    :func:`frisian_mcp.usage.resolver.resolve_system_policy` so it flags
    exactly the values the resolver would defer on — it cannot drift from the
    runtime — and it never coerces the value (``deny`` is never guessed from an
    ambiguous config).

``frisian_mcp.W016``
    Warns when heavy-response continuation entries share an eviction domain
    with OAuth security state — either because
    ``FRISIAN_MCP_HEAVY_CACHE_ALIAS`` is unset (still ``default``) or because
    it resolves to the same ``LOCATION`` as ``default``.  Continuation entries
    are attacker-amplifiable (an unauthenticated caller can mint them) while
    authorization codes and the token-endpoint rate counter are security state,
    and the rate limiter fails **open** when its cache is unavailable.  Absence
    of this warning is **not** proof of isolation: two aliases addressing
    different logical Redis DBs on one instance have distinct ``LOCATION``
    strings and still share that instance's memory.  The requirement is an
    independent eviction *budget*, which settings alone cannot express.

Per-route configuration checks (``E004``, ``E005``, ``E1xx``, ``E2xx``,
``W004``–``W007``) live in :mod:`frisian_mcp.route_audit` and are registered
from there.  They are config-only: the tool registry is empty while system
checks run, because auto-discovery is deferred to the first request.

The checks module is imported from :class:`frisian_mcp.apps.FrisianMcpConfig`
so the ``@register`` decorators fire at app load.  It contributes nothing
at runtime beyond the registrations themselves.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.apps import apps as django_apps
from django.conf import settings
from django.core.cache import DEFAULT_CACHE_ALIAS
from django.core.checks import (  # pylint: disable=redefined-builtin
    Error,
    Tags,
    Warning,
    register,
)

from frisian_mcp.registry import (
    DENY_TIER,
    UNAUTH_TIER_INVALID,
    _VALID_PERMISSION_TIERS,
    classify_unauthenticated_tier,
    tool_registry,
)
from frisian_mcp.usage.resolver import USAGE_POLICY_SETTING, resolve_system_policy

logger = logging.getLogger(__name__)

W001_NO_PERMISSION_CLASSES = "frisian_mcp.W001"
W002_PLAINTEXT_API_KEYS = "frisian_mcp.W002"
W003_PRIVILEGED_SERVICE_ACCOUNT = "frisian_mcp.W003"
E002_OAUTH_IDENTITY_GAP = "frisian_mcp.E002"
E003_UNANNOTATED_CUSTOM_ACTION = "frisian_mcp.E003"
E007_INVALID_UNAUTHENTICATED_TIER = "frisian_mcp.E007"
E008_DISPATCHER_WITHOUT_MEMBERSHIP = "frisian_mcp.E008"
W012_OAUTH_DISCOVERY_HIDDEN = "frisian_mcp.W012"
W013_MALFORMED_RATE_LIMIT = "frisian_mcp.W013"
W014_INVALID_USAGE_POLICY = "frisian_mcp.W014"
W015_INDETERMINATE_CAPABILITY = "frisian_mcp.W015"
W016_HEAVY_CACHE_NOT_ISOLATED = "frisian_mcp.W016"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Standard DRF CRUD actions that map cleanly to a Django permission verb.
#: Non-CRUD ``@mcp_action`` methods must supply ``backend_action`` so the
#: permission adapter can derive the capability string.
_CRUD_ACTIONS: frozenset[str] = frozenset(
    {"list", "create", "retrieve", "update", "partial_update", "destroy"}
)


@register(Tags.security)
def check_permission_classes_in_production(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001 — Django check signature
    **kwargs: Any,  # noqa: ARG001 — Django check signature
) -> list[Warning]:
    """
    Warn loudly when ``DEBUG=False`` and the MCP gateway is unauthenticated.

    The MCP gateway only enforces gateway-level auth via
    :data:`~django.conf.settings.FRISIAN_MCP_PERMISSION_CLASSES`.  When that
    setting is absent or an empty list, every ``tools/call`` reaches the
    registry without an authenticated principal, relying solely on per-tool
    tier filtering and tool-level permission classes.

    For a production deployment that is almost never the intent.  Emit
    ``frisian_mcp.W001`` so the misconfiguration shows up in
    ``manage.py check`` output and CI before it ships.

    Operators who explicitly want an open gateway (e.g. behind their own
    reverse-proxy auth, or a deliberate public demo) silence the warning by
    setting ``FRISIAN_MCP_ALLOW_UNAUTHENTICATED = True``.
    """
    if getattr(settings, "DEBUG", False):
        # Developers don't need a nag at every runserver restart.
        return []

    if getattr(settings, "FRISIAN_MCP_ALLOW_UNAUTHENTICATED", False):
        # Explicit opt-in — operator has acknowledged the open gateway.
        return []

    permission_classes = getattr(settings, "FRISIAN_MCP_PERMISSION_CLASSES", None)
    if permission_classes:
        # Truthy list → at least one class configured; no warning.
        return []

    return [
        Warning(
            "FRISIAN_MCP_PERMISSION_CLASSES is empty in a non-DEBUG environment. "
            "The MCP gateway accepts unauthenticated requests at the HTTP layer; "
            "only per-tool tier filtering will gate tools/call.  In production this "
            "is almost certainly a misconfiguration.",
            hint=(
                "Set FRISIAN_MCP_PERMISSION_CLASSES to a list of DRF permission "
                "classes (e.g. ['rest_framework.permissions.IsAuthenticated']) and "
                "configure FRISIAN_MCP_AUTHENTICATION_CLASSES to match.  If an open "
                "gateway is intentional (e.g. behind reverse-proxy auth, or a "
                "deliberate public demo), set FRISIAN_MCP_ALLOW_UNAUTHENTICATED=True "
                "to silence this warning."
            ),
            id=W001_NO_PERMISSION_CLASSES,
        )
    ]


@register(Tags.security)
def check_api_keys_are_hashed(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Warning]:
    """
    Warn when any ``FRISIAN_MCP_API_KEYS`` entry is unsafe or invalid.

    :class:`~frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication`
    now hashes the incoming Bearer value before comparison, so keys stored in
    ``FRISIAN_MCP_API_KEYS`` must be 64-character lowercase hex HMAC-SHA256
    digests.  A key that is not 64 lowercase hex characters is almost certainly
    a raw plaintext value left over from a pre-hardening configuration.

    Values must also be valid MCP permission tiers.  Typos such as ``"write"``
    or ``"Admin"`` otherwise resolve to the lowest tier at runtime, creating
    confusing and potentially unsafe policy drift.

    Generate the correct digest with::

        python manage.py mcp_hash_api_key <raw-key>
    """
    api_keys: dict[str, str] = getattr(settings, "FRISIAN_MCP_API_KEYS", {})
    if not api_keys:
        return []

    warnings: list[Warning] = []

    plain_keys = [k for k in api_keys if not _HEX64_RE.match(k)]
    if plain_keys:
        count = len(plain_keys)
        noun = "key does" if count == 1 else "keys do"
        warnings.append(
            Warning(
                f"FRISIAN_MCP_API_KEYS contains {count} entr{'y' if count == 1 else 'ies'} "
                f"that {noun} not look like HMAC-SHA256 digests (64 lowercase hex characters). "
                "Raw plaintext keys in settings are a security risk — if settings are captured "
                "by error-tracking or logging, the raw secret is directly usable as a "
                "Bearer token.",
                hint=(
                    "Replace each raw key with its HMAC-SHA256 digest: "
                    "python manage.py mcp_hash_api_key <raw-key>.  "
                    "Update FRISIAN_MCP_API_KEYS to use the printed digest as the dict key."
                ),
                id=W002_PLAINTEXT_API_KEYS,
            )
        )

    invalid_tiers = sorted(
        {str(tier) for tier in api_keys.values() if tier not in _VALID_PERMISSION_TIERS}
    )
    if invalid_tiers:
        valid = ", ".join(sorted(_VALID_PERMISSION_TIERS))
        warnings.append(
            Warning(
                "FRISIAN_MCP_API_KEYS contains invalid permission tier value"
                f"{'s' if len(invalid_tiers) != 1 else ''}: {', '.join(invalid_tiers)}.",
                hint=(
                    "Set every FRISIAN_MCP_API_KEYS value to one of: "
                    f"{valid}. Values are case-sensitive."
                ),
                id=W002_PLAINTEXT_API_KEYS,
            )
        )

    return warnings


@register(Tags.security)
def check_service_account_user(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Warning]:
    """
    Warn when ``FRISIAN_MCP_SERVICE_ACCOUNT_USER`` is configured in production.

    When this setting is present, the invocation backend substitutes the named
    Django user on every synthetic inner request for anonymous MCP callers, so
    host-app ViewSets see an authenticated identity.  If the named account is
    privileged (``is_staff`` or ``is_superuser``), unauthenticated callers
    receive that user's Django object-permissions at the host-app layer —
    potentially exceeding what the MCP tier gate allows.

    This check does not query the database; it fires whenever the setting is
    present in a non-DEBUG environment to prompt a manual audit.  Run
    ``manage.py mcp_doctor --security`` for a privilege check that actually
    looks up the user record.
    """
    if getattr(settings, "DEBUG", False):
        return []

    service_user: str | None = getattr(settings, "FRISIAN_MCP_SERVICE_ACCOUNT_USER", None)
    if not service_user:
        return []

    return [
        Warning(
            f"FRISIAN_MCP_SERVICE_ACCOUNT_USER='{service_user}' is set. "
            "Anonymous MCP callers will be presented to host-app ViewSets as this Django user. "
            "If the account is privileged (is_staff or is_superuser), unauthenticated callers "
            "may receive permissions beyond what the MCP tier gate intends.",
            hint=(
                "Ensure FRISIAN_MCP_SERVICE_ACCOUNT_USER points to a dedicated low-privilege "
                "service account (not staff or superuser).  "
                "Run 'manage.py mcp_doctor --security' to verify the account's privilege level."
            ),
            id=W003_PRIVILEGED_SERVICE_ACCOUNT,
        )
    ]


@register(Tags.security)
def check_unauthenticated_tier_value(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Error]:
    """
    H7: an unrecognised ``FRISIAN_MCP_UNAUTHENTICATED_TIER`` must be loud.

    The runtime already fails closed on an unrecognised value, so this check
    exists for the operator, not the gateway.  A typo used to degrade silently
    to ``read`` — the setting was present, spelled plausibly, and did nothing,
    which is undetectable by reading the config.  Now it denies, and denying
    silently is its own trap: a host that intended ``read_write`` and typed
    ``readwrite`` would lose anonymous access with no explanation.

    Three cases, matching ``registry._resolve_unauthenticated_tier``:

    * absent → no error (documented default ``read``)
    * ``None`` or ``"none"`` → no error (a deliberate lockdown)
    * anything else → this error
    """
    # H13: classification comes from the registry, not a local re-derivation.
    # This check previously carried its own sentinel and vocabulary comparison —
    # a second answer to a question the runtime already answers, and mcp_doctor
    # carried a third that had drifted.
    case, _tier = classify_unauthenticated_tier()
    if case != UNAUTH_TIER_INVALID:
        return []

    raw = getattr(settings, "FRISIAN_MCP_UNAUTHENTICATED_TIER", None)
    accepted = ", ".join(sorted(_VALID_PERMISSION_TIERS) + [DENY_TIER])
    return [
        Error(
            f"FRISIAN_MCP_UNAUTHENTICATED_TIER={raw!r} is not a recognised tier. "
            "Anonymous callers are being DENIED all access, which may not be what "
            "this host intended.",
            hint=(
                f"Set it to one of: {accepted}. Use {DENY_TIER!r} (or None) to deny "
                "anonymous access deliberately, or remove the setting entirely to "
                "keep the documented default of 'read'."
            ),
            id=E007_INVALID_UNAUTHENTICATED_TIER,
        )
    ]


@register(Tags.security)
def check_dispatcher_membership(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Error]:
    """
    H5: a group dispatcher registered without ``group_tool_names`` is an Error.

    ``group_tool_names`` is no longer a negotiation hint.  It is the input to
    two security mechanisms in ``views.py`` — the gate that stops a
    caller-supplied ``resource`` resolving against the *global* registry, and
    the audit resolver deciding whether caller text reaches the sink.  Both
    fail closed on a falsy membership set, so the host is safe; what it is not
    is *informed*.  A dispatcher registered this way silently loses
    ``@mcp_heavy`` negotiation and the dispatcher-routed lean write envelope,
    with no runtime signal at all.

    ``Error``, not ``Warning``, for consistency with this module's own
    convention: every ``Warning`` here is configuration hygiene where the
    package can still tell what the operator meant, and the one existing
    ``Error`` — ``E003`` — is indeterminate security metadata on a dispatcher.
    This is the second instance of that category, not a new one.

    Only **group** dispatchers are in scope.  A ``@mcp_dispatcher`` class
    carries ``dispatcher_meta`` and legitimately has no membership set — its
    actions are methods, not registry entries — so firing on it would break
    startup for every correctly-configured host.  The two kinds are mutually
    exclusive at registration: ``decorators.py`` sets ``dispatcher_meta`` and
    never ``group_tool_names``; ``apps.py`` and ``route_views.py`` do the
    reverse.

    The package cannot manufacture this state itself.  Full route pruning
    yields *absent, not empty* — ``route_views`` drops a wholly-denied group
    rather than rebuilding it membership-less — so this is confined to
    hand-registration, which is why a startup-time instrument is the right
    place for it.
    """
    errors: list[Error] = []
    try:
        for tool_name in tool_registry.list_names():
            entry = tool_registry.get_entry(tool_name)
            if entry is None or not entry.is_dispatcher:
                continue
            # A class dispatcher is identified by its meta, not by membership.
            if getattr(entry, "dispatcher_meta", None) is not None:
                continue
            if getattr(entry, "group_tool_names", None):
                continue
            errors.append(
                Error(
                    f"Group dispatcher {tool_name!r} is registered without "
                    "'group_tool_names'. It cannot route to any resource: "
                    "@mcp_heavy negotiation and the dispatcher-routed lean write "
                    "envelope are both inert on this tool, and the membership gate "
                    "that keeps a caller-supplied 'resource' from resolving against "
                    "the global registry has nothing to check against.",
                    hint=(
                        "Pass group_tool_names=frozenset({...}) naming the flat tools "
                        f"this dispatcher bundles, as apps.py does when it builds one "
                        f"from FRISIAN_MCP_DISPATCH_GROUPS. If {tool_name!r} is "
                        "deliberately membership-less, silence this check by adding "
                        f"'{E008_DISPATCHER_WITHOUT_MEMBERSHIP}' to "
                        "SILENCED_SYSTEM_CHECKS."
                    ),
                    id=E008_DISPATCHER_WITHOUT_MEMBERSHIP,
                )
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "frisian_mcp E008 check failed during registry iteration: %s", exc, exc_info=True
        )
    return errors


@register(Tags.security)
def check_permission_aware_discovery(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Error | Warning]:
    """
    Validate the ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` configuration.

    Fires one sub-check when the feature flag is ``True``:

    **E003** — Unannotated custom action: a ``@mcp_dispatcher`` action is a
    non-CRUD action without a ``backend_action`` keyword argument.  The
    permission adapter cannot derive a Django permission verb for unrecognised
    action names.  Annotate the ``@mcp_action`` with
    ``backend_action='view'`` (or ``'add'`` / ``'change'`` / ``'delete'``).
    """
    if not getattr(settings, "FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY", False):
        return []

    errors: list[Error | Warning] = []

    # E003 — Unannotated non-CRUD dispatcher actions.
    try:
        for tool_name in tool_registry.list_names():
            entry = tool_registry.get_entry(tool_name)
            if entry is None or not entry.is_dispatcher or entry.dispatcher_meta is None:
                continue
            for action_name, action_entry in entry.dispatcher_meta.actions.items():
                if action_name in _CRUD_ACTIONS:
                    continue
                if action_entry.backend_action is None:
                    errors.append(
                        Error(
                            f"Dispatcher {tool_name!r} has a non-CRUD action "
                            f"{action_name!r} without a backend_action annotation. "
                            "FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY cannot determine "
                            "the required Django permission for this action.",
                            hint=(
                                f"Add backend_action='view' (or 'add', 'change', 'delete') "
                                f"to @mcp_action for {action_name!r} on {tool_name!r}."
                            ),
                            id=E003_UNANNOTATED_CUSTOM_ACTION,
                        )
                    )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "frisian_mcp E003 check failed during registry iteration: %s", exc, exc_info=True
        )

    # W015 — H3: entries whose capability cannot be determined.
    #
    # These are now HIDDEN rather than universally visible, so the failure mode
    # is a tool that silently stops appearing in tools/list.  That is the safe
    # direction but a confusing one to debug from the outside, which is why the
    # ruling requires a loud diagnostic rather than silence.
    #
    # A Warning, not an Error: hiding is already the safe outcome, and a host
    # upgrading into this should be told what disappeared without having its
    # startup blocked.  Declaring `capability` or `universal_discovery` clears
    # it, and either way the operator has made a choice rather than inherited
    # one.
    try:
        indeterminate = sorted(
            name
            for name in tool_registry.list_names()
            if (entry := tool_registry.get_entry(name)) is not None
            and not entry.hidden
            and not entry.universal_discovery
            and not entry.capability
            and not (entry.perm_app_label and entry.perm_model)
            # Group dispatchers are judged by their children in list_tools, so
            # they are never indeterminate in their own right.
            and not entry.group_tool_names
        )
        if indeterminate:
            shown = ", ".join(repr(n) for n in indeterminate[:10])
            more = f" (and {len(indeterminate) - 10} more)" if len(indeterminate) > 10 else ""
            errors.append(
                Warning(
                    f"FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY is on and "
                    f"{len(indeterminate)} tool(s) have no determinable capability, "
                    f"so they are HIDDEN from tools/list for every caller: "
                    f"{shown}{more}.",
                    hint=(
                        "Pass capability='app_label.verb_model' to make the tool "
                        "participate in capability filtering, or "
                        "universal_discovery=True to state that it is meant to be "
                        "visible to everyone. Auto-discovered ViewSet tools derive "
                        "this automatically; decorator and imperative registrations "
                        "must declare it."
                    ),
                    id=W015_INDETERMINATE_CAPABILITY,
                )
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "frisian_mcp W015 check failed during registry iteration: %s", exc, exc_info=True
        )

    return errors


@register(Tags.security)
def check_oauth_discovery_not_hidden(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Warning]:
    """
    Warn (LOUD) when OAuth is installed but its discovery metadata is hidden.

    ``FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False`` makes the RFC 8414 and
    RFC 9728 well-known endpoints return 404 and strips the
    ``resource_metadata`` pointer from ``WWW-Authenticate`` challenges.
    Spec-correct MCP clients bootstrap the OAuth handshake from exactly those
    two surfaces, so with the flag off they cannot find the authorization
    server at all — the handshake fails with nothing in the server logs
    (live Claude.ai failure, V11-12 check 2).  Only clients with hard-coded
    endpoint URLs keep working.

    The configuration is coherent for a deployment whose every client is
    pre-registered with pre-shared endpoints, which is why this is LOUD
    (a :class:`Warning`) rather than FATAL.  No ``DEBUG`` gate: the handshake
    is equally broken in development, which is where the live failure
    happened.  Operators who genuinely mean it silence the check with
    ``SILENCED_SYSTEM_CHECKS = ["frisian_mcp.W012"]``.
    """
    if not django_apps.is_installed("frisian_mcp.contrib.oauth"):
        return []
    if getattr(settings, "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY", True):
        return []

    return [
        Warning(
            "FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY=False while frisian_mcp.contrib.oauth "
            "is installed. The RFC 8414 (/.well-known/oauth-authorization-server) and "
            "RFC 9728 (/.well-known/oauth-protected-resource) endpoints return 404 and "
            "401 challenges omit resource_metadata, so discovery-first MCP clients "
            "(Claude.ai, Cursor, ...) cannot locate the authorization server and the "
            "OAuth handshake silently fails. Only clients with hard-coded endpoint "
            "URLs will connect.",
            hint=(
                "Remove FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY (default True) or set it to "
                "True. Hiding discovery metadata is not an authentication gate — to keep "
                "walk-up clients out, leave FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER and "
                "FRISIAN_MCP_OAUTH_REGISTRATION_OPEN at their False defaults instead. "
                "If every client genuinely uses pre-shared endpoint URLs, silence this "
                "check with SILENCED_SYSTEM_CHECKS=['frisian_mcp.W012']."
            ),
            id=W012_OAUTH_DISCOVERY_HIDDEN,
        )
    ]


@register(Tags.security)
def check_oauth_token_rate_limit_format(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Warning]:
    """
    Warn (LOUD) when the configured token rate limit is unparseable.

    The runtime limiter deliberately fails open on a malformed
    ``FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT`` (a cache/config problem must not
    become a token-issuance outage), which means a typo like ``"20/minutes"``
    silently disables the brute-force throttle the operator believes is on.
    Validation shares :func:`~frisian_mcp.contrib.oauth._rate_limiting.parse_rate_limit`
    with the runtime so the boot check and request-time behavior cannot
    disagree about what parses (V11-26 #5).
    """
    if not django_apps.is_installed("frisian_mcp.contrib.oauth"):
        return []
    raw = getattr(settings, "FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT", None)
    if not raw:
        return []

    from frisian_mcp.contrib.oauth._rate_limiting import (  # pylint: disable=import-outside-toplevel
        parse_rate_limit,
    )

    if parse_rate_limit(raw) is not None:
        return []

    return [
        Warning(
            f"FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT={raw!r} is not parseable as 'N/period'. "
            "The token-endpoint rate limiter fails open on a malformed value, so "
            "brute-force throttling you configured is NOT active.",
            hint=(
                "Use the format 'N/period' with period one of: second, minute, hour, "
                "day — e.g. FRISIAN_MCP_OAUTH_TOKEN_RATE_LIMIT='20/minute'."
            ),
            id=W013_MALFORMED_RATE_LIMIT,
        )
    ]


@register(Tags.security)
def check_usage_reporting_policy_value(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Warning]:
    """
    Warn when ``FRISIAN_MCP_USAGE_REPORTING_POLICY`` is set to an unhonoured value.

    The layered token-usage opt-in treats the system policy as authoritative:
    ``"deny"`` forces reporting OFF and cannot be re-enabled by a per-request
    flag.  The resolver honours only the exact strings ``"allow"`` and
    ``"deny"`` (after strip + lowercase); any other value — a non-string such
    as ``b"deny"`` or a list, or a dirty string like a ``"deny"`` with a
    trailing NUL — resolves to ``None`` (defer).  A deny-intended
    misconfiguration therefore silently **fails open**: a request flag can
    enable reporting the operator meant to forbid.

    This check surfaces that at startup so the operator corrects the value
    before it matters.  It reuses
    :func:`~frisian_mcp.usage.resolver.resolve_system_policy` — the same
    normalisation the runtime uses — so the boot check and request-time
    behaviour cannot disagree about what is honoured (the "disabled looks like
    enabled" shape).  It deliberately does **not** coerce an unknown value to
    ``deny``: that would trade a visible fail-open for an invisible behaviour
    change and add an implicit path the deny-authority invariant must hold
    across.
    """
    raw = getattr(settings, USAGE_POLICY_SETTING, None)
    if raw is None:
        # Unset is the default and fully valid (defer to request / global).
        return []
    if resolve_system_policy() is not None:
        # The resolver honours this value as "allow"/"deny" — correctly set.
        return []

    return [
        Warning(
            f"{USAGE_POLICY_SETTING}={raw!r} is set but is not one of the honoured "
            'string values "allow" / "deny". The token-usage resolver treats it as '
            "unset and DEFERS to the per-request flag, so if you intended 'deny' the "
            "control silently fails open — a request header/query can enable reporting.",
            hint=(
                f"Set {USAGE_POLICY_SETTING} to the exact string 'allow' or 'deny' "
                "(case-insensitive), or remove it (None) to defer to "
                "FRISIAN_MCP_USAGE_REPORTING. Non-string values (bytes, lists) and "
                "dirty strings are never coerced to 'deny'."
            ),
            id=W014_INVALID_USAGE_POLICY,
        )
    ]


@register(Tags.security)
def check_heavy_cache_isolation(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Warning]:
    """
    H6: continuation state must not share an eviction domain with security state.

    Continuation entries are **attacker-amplifiable** — an unauthenticated
    caller can mint them — while OAuth authorization codes, the consumed-code
    gate and the token-endpoint rate counter are authentication and
    brute-force-control state.  When both draw on one pool, exhausting the
    first can take the second with it, and the token-endpoint rate limiter
    fails **open** when its cache is unavailable.

    ``FRISIAN_MCP_HEAVY_CACHE_ALIAS`` selects the continuation cache.  This
    check reports the two unseparated states it can actually see:

    * the alias is still ``default``, so nothing was separated at all;
    * the alias exists but resolves to the same ``LOCATION`` as ``default``,
      so the pool was renamed rather than divided.

    It **cannot** confirm the converse.  Two aliases addressing distinct
    logical Redis DBs on one instance have different ``LOCATION`` strings and
    still share that instance's memory, so one exhausts the other.  Absence of
    this warning is therefore not proof of isolation — the requirement is an
    independent eviction *budget* (a separate instance, or ``maxmemory`` set
    per instance), which is an operator obligation the package cannot verify
    from settings.

    A Warning rather than an Error: the unseparated arrangement is what every
    host ran before this setting existed, so failing startup would break
    upgrades that are no worse off than they were.  The exposure is real but
    it is not created by the upgrade.
    """
    alias = getattr(settings, "FRISIAN_MCP_HEAVY_CACHE_ALIAS", None)
    hint = (
        "Point FRISIAN_MCP_HEAVY_CACHE_ALIAS at a cache with its own eviction "
        "budget — a separate Redis instance, or one with maxmemory set. A second "
        "logical DB on the same instance is NOT sufficient: DBs share the "
        "instance's memory, so exhausting one takes the other down with it."
    )

    if alias is None or alias == DEFAULT_CACHE_ALIAS:
        return [
            Warning(
                "Heavy-response continuation entries share the 'default' cache with "
                "OAuth authorization codes and the token-endpoint rate counter. An "
                "unauthenticated caller can mint continuation entries, so cache "
                "exhaustion is reachable from outside; the rate limiter fails OPEN "
                "when its cache is unavailable.",
                hint=hint,
                id=W016_HEAVY_CACHE_NOT_ISOLATED,
            )
        ]

    caches_setting = getattr(settings, "CACHES", {}) or {}
    heavy_location = (caches_setting.get(alias) or {}).get("LOCATION")
    default_location = (caches_setting.get(DEFAULT_CACHE_ALIAS) or {}).get("LOCATION")
    if heavy_location is not None and heavy_location == default_location:
        return [
            Warning(
                f"FRISIAN_MCP_HEAVY_CACHE_ALIAS={alias!r} resolves to the same "
                f"LOCATION as 'default' ({heavy_location!r}), so continuation state "
                "and OAuth security state still share one eviction domain. The alias "
                "renames the pool; it does not divide it.",
                hint=hint,
                id=W016_HEAVY_CACHE_NOT_ISOLATED,
            )
        ]
    return []
