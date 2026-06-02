"""Django admin registration for FrisianMcpToken."""

from django.contrib import admin

from .models import FrisianMcpToken


@admin.register(FrisianMcpToken)
class FrisianMcpTokenAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~frisian_mcp.contrib.tokens.models.FrisianMcpToken`."""

    list_display = ("name", "user", "is_active", "permission", "created_at", "last_used_at")
    list_filter = ("is_active", "permission")
    search_fields = ("name", "user__username", "user__email")
    readonly_fields = ("token", "created_at", "last_used_at")
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "user", "is_active", "permission"),
            },
        ),
        (
            "Token",
            {
                "fields": ("token",),
                "description": (
                    "HMAC-SHA256 digest of the Bearer token (not the raw value).  "
                    "The raw Bearer token is set when the token object is first created "
                    "in code (available as <code>plaintext_token</code> on the returned "
                    "instance) and is never persisted or shown here."
                ),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_at", "last_used_at"),
                "classes": ("collapse",),
            },
        ),
    )
