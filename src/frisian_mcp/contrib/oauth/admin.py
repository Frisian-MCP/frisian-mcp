"""Django admin registration for OAuthClient and OAuthAccessToken."""

from django import forms
from django.conf import settings
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import format_html

from .models import OAuthAccessToken, OAuthAuthorizeConsent, OAuthClient


class OAuthClientAdminForm(forms.ModelForm):  # type: ignore[type-arg]
    """ModelForm for OAuthClient that suppresses verbose RFC jargon in help text."""

    class Meta:
        """Form metadata."""

        model = OAuthClient
        fields = "__all__"
        help_texts = {"redirect_uris": ""}  # suppress model-level RFC jargon


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~frisian_mcp.contrib.oauth.models.OAuthClient`."""

    form = OAuthClientAdminForm
    list_display = ("name", "is_active", "permission", "created_at")
    list_filter = ("is_active", "permission")
    search_fields = ("name", "client_id")
    readonly_fields = (
        "client_id",
        "client_secret",
        "created_at",
        "connector_sign_in_url",
        "connector_mcp_url",
    )
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "is_active", "permission", "user"),
            },
        ),
        (
            "Credentials",
            {
                "fields": ("client_id", "client_secret"),
                "description": (
                    "The client_id is the public identifier. "
                    "The client_secret is shown once at creation — only the HMAC digest is stored."
                ),
            },
        ),
        (
            "Connector URLs",
            {
                "fields": ("connector_sign_in_url", "connector_mcp_url"),
                "description": (
                    "Copy the <strong>MCP server URL</strong> into your AI assistant's connector "
                    "settings. The sign-in URL is used automatically during the connection process "
                    "— you don't need to enter it separately."
                ),
            },
        ),
        (
            "Allowed callback URLs",
            {
                "fields": ("redirect_uris",),
                "description": (
                    "Paste the callback URL shown by your AI assistant when connecting. "
                    "Add one URL per entry in the JSON list, e.g. "
                    '<code>["https://claude.ai/api/mcp/auth_callback", '
                    '"https://chatgpt.com/connector/oauth/&lt;id&gt;"]</code>. '
                    "You must add a callback URL for each assistant you want to connect."
                ),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_at",),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Sign-in URL")
    def connector_sign_in_url(self, obj: OAuthClient) -> str:  # pylint: disable=unused-argument
        """Return the OAuth authorize URL formatted as an HTML code element."""
        issuer: str = getattr(settings, "FRISIAN_MCP_OAUTH_ISSUER", "").rstrip("/")
        url = f"{issuer}/oauth/authorize/"
        return format_html("<code>{}</code>", url)

    @admin.display(description="MCP server URL")
    def connector_mcp_url(self, obj: OAuthClient) -> str:  # pylint: disable=unused-argument
        """Return the MCP gateway URL formatted as an HTML code element."""
        issuer: str = getattr(settings, "FRISIAN_MCP_OAUTH_ISSUER", "").rstrip("/")
        mcp_path: str = str(
            getattr(settings, "FRISIAN_MCP_PROTECTED_PATH", None)
            or getattr(settings, "FRISIAN_MCP_PATH", "/mcp/")
        )
        url = f"{issuer}/{mcp_path.lstrip('/')}"
        return format_html("<code>{}</code>", url)


@admin.register(OAuthAuthorizeConsent)
class OAuthAuthorizeConsentAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~frisian_mcp.contrib.oauth.models.OAuthAuthorizeConsent`.

    Operators revoke a granted consent by deleting the row.  Use the
    ``revoke_selected_consents`` action to bulk-revoke multiple grants
    at once.
    """

    list_display = ("user", "client_id", "redirect_uri", "scope", "created_at")
    list_filter = ("scope", "created_at")
    search_fields = ("user__username", "client_id", "redirect_uri")
    readonly_fields = ("created_at",)
    actions = ("revoke_selected_consents",)

    @admin.action(description="Revoke selected consent grants")
    def revoke_selected_consents(
        self, request: HttpRequest, queryset: QuerySet[OAuthAuthorizeConsent]
    ) -> None:  # pragma: no cover - admin action wiring
        """Delete the selected ``OAuthAuthorizeConsent`` rows.

        A revoked consent means the user must approve the next authorize
        request for that ``(client_id, redirect_uri, scope)`` tuple again.
        """
        count = queryset.delete()[0]
        self.message_user(request, f"Revoked {count} consent grant(s).")


@admin.register(OAuthAccessToken)
class OAuthAccessTokenAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin interface for :class:`~frisian_mcp.contrib.oauth.models.OAuthAccessToken`."""

    list_display = ("client", "permission", "expires_at", "created_at")
    list_filter = ("client__is_active", "permission")
    search_fields = ("client__name",)
    readonly_fields = ("token", "client", "expires_at", "permission", "created_at")
    fieldsets = (
        (
            None,
            {
                "fields": ("client", "permission", "expires_at"),
            },
        ),
        (
            "Token",
            {
                "fields": ("token",),
                "description": (
                    "HMAC-SHA256 digest of the Bearer token (not the raw value).  "
                    "The raw Bearer token was returned to the client exactly once "
                    "at issuance via the token endpoint and is never stored or recoverable."
                ),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_at",),
                "classes": ("collapse",),
            },
        ),
    )
