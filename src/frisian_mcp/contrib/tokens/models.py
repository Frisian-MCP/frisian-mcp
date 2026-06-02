"""
FrisianMcpToken model — static Bearer token for MCP endpoint authentication.

Tokens are generated automatically on first save.  Each token can optionally
be associated with a Django user; service-to-service tokens (e.g. AI agent
clients) may omit the user relationship entirely.

Usage::

    # In INSTALLED_APPS
    INSTALLED_APPS = [
        ...
        "frisian_mcp.contrib.tokens",
    ]

    # In settings
    FRISIAN_MCP_AUTHENTICATION_CLASSES = [
        "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    ]
    FRISIAN_MCP_PERMISSION_CLASSES = [
        "rest_framework.permissions.IsAuthenticated",
    ]

    # Then run: python manage.py migrate
    # Create tokens in Django admin or via the shell:
    #   FrisianMcpToken.objects.create(name="claude-agent")

"""

from __future__ import annotations

import hashlib
import hmac as _hmac_lib
import secrets
from typing import Any

from django.conf import settings
from django.db import models

_PERMISSION_CHOICES = [
    ("read", "Read Only"),
    ("read_write", "Read Write"),
    ("admin", "Admin"),
]


def _hmac_token(raw: str) -> str:
    """Return HMAC-SHA256 of *raw* keyed by FRISIAN_MCP_HMAC_KEY (or SECRET_KEY) as hex."""
    hmac_key: str = getattr(settings, "FRISIAN_MCP_HMAC_KEY", "") or settings.SECRET_KEY
    return _hmac_lib.new(hmac_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


class FrisianMcpToken(models.Model):
    """
    Static Bearer token for authenticating MCP clients.

    The ``token`` field is auto-generated on first save using
    :func:`secrets.token_hex` (32 bytes → 64 hex characters).  It is stored
    as an HMAC-SHA256 digest; the raw value is exposed once via
    ``plaintext_token`` immediately after creation and is never persisted.

    Service tokens (e.g. AI agent clients) may leave ``user`` unset.  In that
    case :class:`~frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication`
    returns :class:`~django.contrib.auth.models.AnonymousUser` as the
    ``request.user``.  Pair with a permissive
    ``FRISIAN_MCP_PERMISSION_CLASSES`` or omit the permission check entirely
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
    permission = models.CharField(
        max_length=10,
        choices=_PERMISSION_CHOICES,
        default="read_write",
        help_text=(
            "Controls which tier of tools this token can access: "
            "Read Only (read tools only), Read Write (read + write tools), "
            "or Admin (all tools)."
        ),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="frisian_mcp_tokens",
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

        verbose_name = "Frisian MCP Token"
        verbose_name_plural = "Frisian MCP Tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token", "is_active"], name="frisian_mcp_tok_active_idx"),
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
