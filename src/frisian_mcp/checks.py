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
from django.core.checks import (  # pylint: disable=redefined-builtin
    Error,
    Tags,
    Warning,
    register,
)

from frisian_mcp.registry import _VALID_PERMISSION_TIERS, tool_registry

logger = logging.getLogger(__name__)

W001_NO_PERMISSION_CLASSES = "frisian_mcp.W001"
W002_PLAINTEXT_API_KEYS = "frisian_mcp.W002"
W003_PRIVILEGED_SERVICE_ACCOUNT = "frisian_mcp.W003"
E002_OAUTH_IDENTITY_GAP = "frisian_mcp.E002"
E003_UNANNOTATED_CUSTOM_ACTION = "frisian_mcp.E003"
W012_OAUTH_DISCOVERY_HIDDEN = "frisian_mcp.W012"

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
def check_permission_aware_discovery(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Error]:
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

    errors: list[Error] = []

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
