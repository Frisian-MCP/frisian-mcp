"""
FrieseMcpToken model — static Bearer token for MCP endpoint authentication.

Tokens are generated automatically on first save.  Each token can optionally
be associated with a Django user; service-to-service tokens (e.g. AI agent
clients) may omit the user relationship entirely.

Usage::

    # In INSTALLED_APPS
    INSTALLED_APPS = [
        ...
        "friese_mcp.contrib.tokens",
    ]

    # In settings
    FRIESE_MCP_AUTHENTICATION_CLASSES = [
        "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication",
    ]
    FRIESE_MCP_PERMISSION_CLASSES = [
        "rest_framework.permissions.IsAuthenticated",
    ]

    # Then run: python manage.py migrate
    # Create tokens in Django admin or via the shell:
    #   FrieseMcpToken.objects.create(name="claude-agent")

"""

from __future__ import annotations

import hashlib
import hmac as _hmac_lib
import secrets
from typing import Any

from django.conf import settings
from django.db import models


def _hmac_token(raw: str) -> str:
    """Return HMAC-SHA256 of *raw* keyed by Django's SECRET_KEY (64 hex chars)."""
    key = settings.SECRET_KEY.encode()
    return _hmac_lib.new(key, raw.encode(), hashlib.sha256).hexdigest()


class FrieseMcpToken(models.Model):
    """
    Static Bearer token for authenticating MCP clients.

    The ``token`` field is auto-generated on first save using
    :func:`secrets.token_hex` (32 bytes → 64 hex characters).  It is stored
    in plaintext and must be treated as a secret by the host application.

    Service tokens (e.g. AI agent clients) may leave ``user`` unset.  In that
    case :class:`~friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication`
    returns :class:`~django.contrib.auth.models.AnonymousUser` as the
    ``request.user``.  Pair with a permissive
    ``FRIESE_MCP_PERMISSION_CLASSES`` or omit the permission check entirely
    for unauthenticated-but-token-gated access.

    """

    token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="HMAC-SHA256 of the raw Bearer token keyed by SECRET_KEY.  Never the raw value.",
    )
    name = models.CharField(
        max_length=200,
        help_text="Human-readable label for this token (e.g. 'claude-agent', 'staging-client').",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive tokens are rejected by the authentication class.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="friese_mcp_tokens",
        help_text="Optional user associated with this token.  Leave blank for service tokens.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Updated automatically each time the token authenticates a request.",
    )

    class Meta:
        """Model metadata."""

        verbose_name = "Friese MCP Token"
        verbose_name_plural = "Friese MCP Tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token", "is_active"], name="friese_mcp_tok_active_idx"),
        ]

    def __str__(self) -> str:
        """Return a human-readable representation."""
        state = "active" if self.is_active else "inactive"
        return f"{self.name} ({state})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Auto-generate ``token`` on first save; exposes raw value once via ``plaintext_token``."""
        if not self.token:
            raw = secrets.token_hex(32)
            self.plaintext_token: str = raw  # pylint: disable=attribute-defined-outside-init
            self.token = _hmac_token(raw)
        super().save(*args, **kwargs)
