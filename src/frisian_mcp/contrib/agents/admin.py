"""Django admin registration for AgentConnection."""

from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import AgentConnection


@admin.register(AgentConnection)
class AgentConnectionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~frisian_mcp.contrib.agents.models.AgentConnection`."""

    list_display = (
        "name",
        "agent_type",
        "is_active",
        "credential_summary",
        "last_seen_at",
        "created_at",
    )
    list_filter = ("agent_type", "is_active")
    search_fields = ("name", "notes")
    readonly_fields = ("credential_summary", "last_seen_at", "created_at")
    actions = ["deactivate_agents"]
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "agent_type", "is_active"),
            },
        ),
        (
            "Credentials",
            {
                "fields": ("token", "oauth_client", "credential_summary"),
                "description": (
                    "Link this agent to a static token (contrib.tokens) or an OAuth client "
                    "(contrib.oauth).  Set one or the other — not both."
                ),
            },
        ),
        (
            "Permissions",
            {
                "fields": ("allowed_tools",),
                "description": (
                    "Enter a JSON array of exact tool names to restrict this agent, "
                    'e.g. ["users.list", "items.create"].  '
                    "Leave blank to allow all registered tools."
                ),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("last_seen_at", "created_at", "notes"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Credential")
    def credential_summary(self, obj: AgentConnection) -> str:
        """Show which credential (if any) is linked to this agent connection."""
        parts = []
        if obj.token_id:
            parts.append(f"Token: {obj.token}")
        if obj.oauth_client_id:
            parts.append(f"OAuth: {obj.oauth_client}")
        return ", ".join(parts) if parts else "—"

    @admin.action(description="Deactivate selected agent connections")
    def deactivate_agents(
        self,
        request: HttpRequest,
        queryset: QuerySet[AgentConnection],
    ) -> None:
        """Mark all selected AgentConnections as inactive."""
        updated: int = queryset.update(is_active=False)
        self.message_user(request, f"{updated} agent connection(s) deactivated.")
