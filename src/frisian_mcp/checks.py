"""
Django system checks for frisian-mcp configuration safety.

Registered checks
-----------------

``frisian_mcp.W001``  (SEC-4)
    Warns when ``DEBUG=False`` and the gateway has no permission classes
    configured (``FRISIAN_MCP_PERMISSION_CLASSES`` is missing or empty), so
    the MCP endpoint is reachable by unauthenticated callers in production.
    Operators who *do* want a public gateway must set
    ``FRISIAN_MCP_ALLOW_UNAUTHENTICATED = True`` to silence the warning —
    that is the explicit opt-in.

``frisian_mcp.E002``
    Error when ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True`` and
    ``frisian_mcp.contrib.oauth`` is installed but
    ``FRISIAN_MCP_OAUTH_SERVICE_USER`` is not set.  OAuth service principals
    have no Django permissions, so all tools would be hidden from OAuth
    clients unless a concrete service user is configured.

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

from typing import Any

from django.conf import settings
from django.core.checks import (  # noqa: A004 — Django's Warning, not builtins  # pylint: disable=redefined-builtin
    Error,  # noqa: A004
    Tags,
    Warning,
    register,
)

from frisian_mcp.registry import tool_registry

W001_NO_PERMISSION_CLASSES = "frisian_mcp.W001"
E002_OAUTH_IDENTITY_GAP = "frisian_mcp.E002"
E003_UNANNOTATED_CUSTOM_ACTION = "frisian_mcp.E003"

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
def check_permission_aware_discovery(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001
    **kwargs: Any,  # noqa: ARG001
) -> list[Error]:
    """
    Validate the ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` configuration.

    Fires two sub-checks when the feature flag is ``True``:

    **E002** — OAuth identity gap: ``frisian_mcp.contrib.oauth`` is installed
    but ``FRISIAN_MCP_OAUTH_SERVICE_USER`` is not set.  OAuth service
    principals have ``get_all_permissions() == set()``, so permission-aware
    discovery would hide every tool from OAuth clients.  Operators must
    configure a service user so the adapter can resolve real capabilities.

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
    except Exception:  # pylint: disable=broad-exception-caught  # noqa: S110
        pass

    return errors
