"""Validated lookup for ``FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION``.

The setting is written into ``OAuthClient.permission`` for auto-registered
and DCR-registered PKCE clients (see ``views.py`` :func:`_handle_authorization_code`
and :class:`RegistrationView`).  A typo (``"redd"``), wrong type, or
oversized string would persist a mis-shaped tier onto the DB row, where it
would never match a runtime tier check and would silently downgrade affected
clients to the safe default.  This helper validates the operator-configured
value against the canonical tier set and falls back to ``"read"`` on any
unexpected shape.
"""

from __future__ import annotations

from django.conf import settings

#: Tier strings accepted as a value for ``FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION``.
#: Anything outside this set is treated as misconfiguration and falls back
#: to ``"read"`` rather than being persisted onto ``OAuthClient.permission``.
_VALID_PKCE_DEFAULT_PERMISSIONS: frozenset[str] = frozenset({"read", "read_write", "admin"})


def _pkce_default_permission() -> str:
    """Return ``FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION``, validated, default ``"read"``."""
    value = getattr(settings, "FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION", "read")
    if isinstance(value, str) and value in _VALID_PKCE_DEFAULT_PERMISSIONS:
        return value
    return "read"
