"""
Django system checks for friese-mcp configuration safety.

Registered checks
-----------------

``friese_mcp.W001``  (SEC-4)
    Warns when ``DEBUG=False`` and the gateway has no permission classes
    configured (``FRIESE_MCP_PERMISSION_CLASSES`` is missing or empty), so
    the MCP endpoint is reachable by unauthenticated callers in production.
    Operators who *do* want a public gateway must set
    ``FRIESE_MCP_ALLOW_UNAUTHENTICATED = True`` to silence the warning —
    that is the explicit opt-in.

The checks module is imported from :class:`friese_mcp.apps.FrieseMcpConfig`
so the ``@register`` decorators fire at app load.  It contributes nothing
at runtime beyond the registrations themselves.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import (  # noqa: A004 — Django's Warning, not builtins  # pylint: disable=redefined-builtin
    Tags,
    Warning,
    register,
)

W001_NO_PERMISSION_CLASSES = "friese_mcp.W001"


@register(Tags.security)
def check_permission_classes_in_production(  # pylint: disable=unused-argument
    app_configs: Any = None,  # noqa: ARG001 — Django check signature
    **kwargs: Any,  # noqa: ARG001 — Django check signature
) -> list[Warning]:
    """
    Warn loudly when ``DEBUG=False`` and the MCP gateway is unauthenticated.

    The MCP gateway only enforces gateway-level auth via
    :data:`~django.conf.settings.FRIESE_MCP_PERMISSION_CLASSES`.  When that
    setting is absent or an empty list, every ``tools/call`` reaches the
    registry without an authenticated principal, relying solely on per-tool
    tier filtering and tool-level permission classes.

    For a production deployment that is almost never the intent.  Emit
    ``friese_mcp.W001`` so the misconfiguration shows up in
    ``manage.py check`` output and CI before it ships.

    Operators who explicitly want an open gateway (e.g. behind their own
    reverse-proxy auth, or a deliberate public demo) silence the warning by
    setting ``FRIESE_MCP_ALLOW_UNAUTHENTICATED = True``.
    """
    if getattr(settings, "DEBUG", False):
        # Developers don't need a nag at every runserver restart.
        return []

    if getattr(settings, "FRIESE_MCP_ALLOW_UNAUTHENTICATED", False):
        # Explicit opt-in — operator has acknowledged the open gateway.
        return []

    permission_classes = getattr(settings, "FRIESE_MCP_PERMISSION_CLASSES", None)
    if permission_classes:
        # Truthy list → at least one class configured; no warning.
        return []

    return [
        Warning(
            "FRIESE_MCP_PERMISSION_CLASSES is empty in a non-DEBUG environment. "
            "The MCP gateway accepts unauthenticated requests at the HTTP layer; "
            "only per-tool tier filtering will gate tools/call.  In production this "
            "is almost certainly a misconfiguration.",
            hint=(
                "Set FRIESE_MCP_PERMISSION_CLASSES to a list of DRF permission "
                "classes (e.g. ['rest_framework.permissions.IsAuthenticated']) and "
                "configure FRIESE_MCP_AUTHENTICATION_CLASSES to match.  If an open "
                "gateway is intentional (e.g. behind reverse-proxy auth, or a "
                "deliberate public demo), set FRIESE_MCP_ALLOW_UNAUTHENTICATED=True "
                "to silence this warning."
            ),
            id=W001_NO_PERMISSION_CLASSES,
        )
    ]
