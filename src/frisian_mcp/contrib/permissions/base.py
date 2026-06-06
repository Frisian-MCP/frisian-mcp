"""
Permission adapter protocol and default Django implementation.

When ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` is ``True``, frisian-mcp
loads the adapter class named in ``FRISIAN_MCP_PERMISSION_ADAPTER`` (default:
:class:`DjangoPermissionAdapter`) and calls it once per ``tools/list`` request
to decide which tools a given user is allowed to see.

The adapter answers two questions:

1. ``get_capabilities(user)`` — what Django-style permissions does this user
   hold?  Returns a ``frozenset`` of ``"app_label.action_model"`` strings,
   matching the format produced by ``user.get_all_permissions()``.

2. ``is_unrestricted(user)`` — should this user see ALL tools, skipping the
   per-capability filter entirely?  Typically ``True`` for superusers.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PermissionAdapter(Protocol):
    """
    Protocol for permission-aware discovery adapters.

    Implementors map a Django request user to the set of Django-style
    permission strings that determine which MCP tools are included in the
    ``tools/list`` response.
    """

    def get_capabilities(self, user: Any) -> frozenset[str]:
        """Return frozenset of ``'app_label.action_model'`` strings held by *user*."""
        ...

    def is_unrestricted(self, user: Any) -> bool:
        """Return ``True`` when *user* should see all tools regardless of permissions."""
        ...


class DjangoPermissionAdapter:
    """
    Default adapter: delegates to Django's ``user.get_all_permissions()``.

    Works for any Django project using the standard authentication backend.
    Returns an empty frozenset on error so a broken permission backend cannot
    accidentally expose all tools.
    """

    def get_capabilities(self, user: Any) -> frozenset[str]:
        """Return frozenset of ``'app_label.action_model'`` strings held by *user*."""
        try:
            perms = user.get_all_permissions()
        except Exception:  # pylint: disable=broad-exception-caught
            return frozenset()
        return frozenset(str(p) for p in perms)

    def is_unrestricted(self, user: Any) -> bool:
        """Return ``True`` when *user* is a superuser and should see all tools."""
        return bool(getattr(user, "is_superuser", False))
