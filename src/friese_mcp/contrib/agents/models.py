"""
AgentConnection model — admin-managed registry of coding agent MCP connections.

Stores a named agent profile (Claude Code, Cursor, GPT, etc.) linked to a
static Bearer token or OAuth client credential.  An optional per-agent
``allowed_tools`` allowlist restricts which tools the agent may see and call.

Requirements
~~~~~~~~~~~~
This module requires both ``friese_mcp.contrib.tokens`` and
``friese_mcp.contrib.oauth`` to be present in ``INSTALLED_APPS`` so that the
foreign key migrations can resolve.  Install at least one credential app before
running ``migrate``::

    INSTALLED_APPS = [
        ...
        "friese_mcp.contrib.tokens",   # and/or contrib.oauth
        "friese_mcp.contrib.agents",
    ]

Usage::

    from friese_mcp.contrib.tokens.models import FrieseMcpToken
    from friese_mcp.contrib.agents.models import AgentConnection

    token = FrieseMcpToken.objects.create(name="claude-agent")
    AgentConnection.objects.create(
        name="Claude Code — production",
        agent_type="claude-code",
        token=token,
        allowed_tools=["users.list", "items.create"],
    )

"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


def validate_tool_name_list(value: Any) -> None:
    """
    Validate that *value* is a list of non-empty strings.

    Called by Django at form and model validation time.  Raises
    :exc:`~django.core.exceptions.ValidationError` when *value* is not a list
    or when any element is not a non-empty string.
    """
    if not isinstance(value, list):
        raise ValidationError(
            "allowed_tools must be a JSON array, not %(type)s.",
            params={"type": type(value).__name__},
        )
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                "allowed_tools[%(i)d] must be a non-empty string.",
                params={"i": i},
            )

AGENT_TYPE_CHOICES: list[tuple[str, str]] = [
    ("claude-code", "Claude Code"),
    ("cursor", "Cursor"),
    ("gpt", "GPT"),
    ("github-copilot", "GitHub Copilot"),
    ("generic", "Generic"),
]


class AgentConnection(models.Model):
    """
    Admin-registered coding agent profile with optional per-agent tool allowlist.

    Each ``AgentConnection`` represents a registered MCP client (Claude Code,
    Cursor, GPT, etc.) and optionally links it to a static token or OAuth client
    credential so the gateway can identify the agent from ``request.auth``.

    Credential link
    ~~~~~~~~~~~~~~~
    Set ``token`` for agents using
    :class:`~friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication`,
    or ``oauth_client`` for agents using
    :class:`~friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication`.
    Both may be left blank for a profile-only entry.

    Tool filtering
    ~~~~~~~~~~~~~~
    When ``allowed_tools`` is a non-empty JSON array of tool names, the gateway
    will restrict ``tools/list`` and ``tools/call`` to only those tools for
    requests authenticated with the linked credential.  Set to ``null`` (the
    default) to allow all registered tools.
    """

    name = models.CharField(
        max_length=200,
        help_text=(
            "Human-readable label for this agent connection "
            "(e.g. 'Claude Code — production')."
        ),
    )
    agent_type = models.CharField(
        max_length=50,
        choices=AGENT_TYPE_CHOICES,
        default="generic",
        help_text="The type of coding agent or MCP client.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Inactive connections are ignored by the gateway. "
            "Linked credentials remain valid but tool filtering is disabled."
        ),
    )
    allowed_tools = models.JSONField(
        null=True,
        blank=True,
        validators=[validate_tool_name_list],
        help_text=(
            'Optional JSON array of tool names this agent may see and call '
            '(e.g. ["users.list", "items.create"]). '
            "Leave blank to allow all registered tools."
        ),
    )
    token = models.ForeignKey(
        "friese_mcp_tokens.FrieseMcpToken",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="agent_connections",
        help_text=(
            "Static Bearer token credential for this agent. "
            "Set either this or oauth_client — not both."
        ),
    )
    oauth_client = models.ForeignKey(
        "friese_mcp_oauth.OAuthClient",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="agent_connections",
        help_text=(
            "OAuth 2.0 client credential for this agent. "
            "Set either this or token — not both."
        ),
    )
    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent tools/call request from this agent.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Optional free-text notes (owner, purpose, rotation schedule, etc.).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata."""

        verbose_name = "Agent Connection"
        verbose_name_plural = "Agent Connections"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(token__isnull=True) | Q(oauth_client__isnull=True),
                name="agent_connection_xor_credential",
            ),
        ]

    def clean(self) -> None:
        """
        Enforce XOR: at most one of token or oauth_client may be set.

        Note: Django only calls ``clean()`` during form validation (admin, ModelForm).
        Direct ORM ``save()`` skips it.  The ``agent_connection_xor_credential``
        ``CheckConstraint`` enforces this at the database level and raises
        ``IntegrityError`` (not ``ValidationError``) for ORM-level violations.
        """
        if self.token_id is not None and self.oauth_client_id is not None:
            raise ValidationError(
                "An AgentConnection may have a token or an oauth_client, but not both."
            )

    def __str__(self) -> str:
        """Return a human-readable representation."""
        state = "active" if self.is_active else "inactive"
        return f"{self.name} ({self.get_agent_type_display()}, {state})"
