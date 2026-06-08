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
    format).  Raw plaintext keys in settings are a security risk — if
    settings are captured by error-tracking or logging, the raw secret is
    directly usable as a Bearer token.  Use
    ``python manage.py mcp_hash_api_key <raw-key>`` to generate the correct
    digest.

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

The checks module is imported from :class:`frisian_mcp.apps.FrisianMcpConfig`
so the ``@register`` decorators fire at app load.  It contributes nothing
at runtime beyond the registrations themselves.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.conf import settings
from django.core.checks import (  # noqa: A004 — Django's Warning, not builtins  # pylint: disable=redefined-builtin
    Error,  # noqa: A004
    Tags,
    Warning,
    register,
)

from frisian_mcp.registry import tool_registry

logger = logging.getLogger(__name__)

W001_NO_PERMISSION_CLASSES = "frisian_mcp.W001"
W002_PLAINTEXT_API_KEYS = "frisian_mcp.W002"
W003_PRIVILEGED_SERVICE_ACCOUNT = "frisian_mcp.W003"
E002_OAUTH_IDENTITY_GAP = "frisian_mcp.E002"
E003_UNANNOTATED_CUSTOM_ACTION = "frisian_mcp.E003"

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
    Warn when any ``FRISIAN_MCP_API_KEYS`` entry does not look like a hashed key.

    :class:`~frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication`
    now hashes the incoming Bearer value before comparison, so keys stored in
    ``FRISIAN_MCP_API_KEYS`` must be 64-character lowercase hex HMAC-SHA256
    digests.  A key that is not 64 lowercase hex characters is almost certainly
    a raw plaintext value left over from a pre-hardening configuration.

    Generate the correct digest with::

        python manage.py mcp_hash_api_key <raw-key>
    """
    api_keys: dict[str, str] = getattr(settings, "FRISIAN_MCP_API_KEYS", {})
    if not api_keys:
        return []

    plain_keys = [k for k in api_keys if not _HEX64_RE.match(k)]
    if not plain_keys:
        return []

    count = len(plain_keys)
    noun = "key does" if count == 1 else "keys do"
    return [
        Warning(
            f"FRISIAN_MCP_API_KEYS contains {count} entr{'y' if count == 1 else 'ies'} "
            f"that {noun} not look like HMAC-SHA256 digests (64 lowercase hex characters). "
            "Raw plaintext keys in settings are a security risk — if settings are captured "
            "by error-tracking or logging, the raw secret is directly usable as a Bearer token.",
            hint=(
                "Replace each raw key with its HMAC-SHA256 digest: "
                "python manage.py mcp_hash_api_key <raw-key>.  "
                "Update FRISIAN_MCP_API_KEYS to use the printed digest as the dict key."
            ),
            id=W002_PLAINTEXT_API_KEYS,
        )
    ]


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
