"""
DRF permission classes for friese_mcp.contrib.tokens.

Provides :class:`IsAuthenticatedOrServiceToken`, which grants access to both
authenticated users and service tokens (tokens with no linked Django user).
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission

from .models import FrieseMcpToken


class IsAuthenticatedOrServiceToken(BasePermission):
    """
    Allow access to authenticated users and service-token callers.

    DRF's built-in :class:`~rest_framework.permissions.IsAuthenticated` rejects
    service tokens because
    :class:`~friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication`
    sets ``request.user = AnonymousUser()`` when the token has no linked Django
    user.  This permission grants access in either of two situations:

    1. ``request.user.is_authenticated`` is ``True`` — the token is linked to a
       real Django user.  Identical to ``IsAuthenticated``.
    2. ``request.auth`` is a :class:`~friese_mcp.contrib.tokens.models.FrieseMcpToken`
       — the request was authenticated by a valid (active) service token even
       though no Django user is associated.

    Usage::

        FRIESE_MCP_PERMISSION_CLASSES = [
            "friese_mcp.contrib.tokens.permissions.IsAuthenticatedOrServiceToken",
        ]

    """

    def has_permission(self, request: Any, view: Any) -> bool:
        """Return ``True`` when the caller is authenticated or holds an active service token."""
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return True
        auth = getattr(request, "auth", None)
        return isinstance(auth, FrieseMcpToken) and auth.is_active
