"""Django admin registration for FrieseMcpToken."""

from django.contrib import admin

from .models import FrieseMcpToken


@admin.register(FrieseMcpToken)
class FrieseMcpTokenAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~friese_mcp.contrib.tokens.models.FrieseMcpToken`."""

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
                    "The Bearer token secret. Shown once after creation — "
                    "copy it before navigating away."
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
