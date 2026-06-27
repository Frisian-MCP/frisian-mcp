"""Django admin registration for FrisianMcpToken."""

from datetime import timedelta
from typing import Any

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import FrisianMcpToken

_PLAINTEXT_ATTR = "_frisian_mcp_plaintext_token"
_REFRESH_DETECTED_ATTR = "_frisian_mcp_refresh_detected"
# Window inside which an identical-name+user create is treated as a browser
# refresh of the just-issued admin form rather than a deliberate new token.
_REFRESH_WINDOW_SECONDS = 10


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
                    "The raw Bearer token is rendered ONCE in a dedicated admin "
                    "response immediately after first save.  After that it is "
                    "unrecoverable; delete and re-create to issue a new one."
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

    def save_model(
        self,
        request: HttpRequest,
        obj: FrisianMcpToken,
        form: Any,
        change: bool,
    ) -> None:
        """Save the token; suppress duplicate-on-refresh; stash plaintext."""
        if not change:
            # Refresh-safety: a re-POST within _REFRESH_WINDOW_SECONDS of an
            # identical name+user create is almost certainly a browser
            # refresh of the just-issued admin form, not a deliberate
            # second create.  Skip the duplicate save and point the admin
            # at the already-issued row.  The original raw value is gone
            # (intentionally — it lived only on the first response), so
            # response_add will fall through to the standard admin redirect.
            threshold = timezone.now() - timedelta(seconds=_REFRESH_WINDOW_SECONDS)
            # Match every submitted attribute the user could have set, so the
            # dedupe only fires on a TRUE repeat-submission and never collapses
            # two deliberate-but-different creates (e.g. same name, different
            # permission tier).  Skipping is_active here would also mean an
            # is_active=False submission could never match and would always
            # mint a duplicate inactive row on refresh.
            dupe = (
                FrisianMcpToken.objects.filter(
                    name=obj.name,
                    user=obj.user,
                    is_active=obj.is_active,
                    permission=obj.permission,
                    created_at__gte=threshold,
                )
                .order_by("-created_at")
                .first()
            )
            if dupe is not None:
                obj.pk = dupe.pk
                setattr(request, _REFRESH_DETECTED_ATTR, True)
                return

        super().save_model(request, obj, form, change)
        plaintext: str | None = getattr(obj, "plaintext_token", None)
        if not change and plaintext:
            # Stash on the *request* (per-request, in-memory) rather than the
            # messages framework or session storage.  All standard messages
            # backends (cookie, session, fallback) persist the message body
            # until the next page consumes it, which would leak the raw
            # Bearer into a transport store.  The request object dies at the
            # end of the response cycle, so this attribute never survives
            # past `response_add`.
            setattr(request, _PLAINTEXT_ATTR, plaintext)

    def response_add(
        self,
        request: HttpRequest,
        obj: FrisianMcpToken,
        post_url_continue: str | None = None,
    ) -> HttpResponse:
        """
        Render the stashed plaintext inline once; fall through on refresh.

        Departs from Django admin's Post/Redirect/Get pattern by serving
        the plaintext directly in the POST response body.  Full PRG would
        require parking the raw Bearer in a transport store (session or
        cache) between the POST and the redirected GET, which we
        explicitly engineered away.  The accidental-refresh case is
        handled in :meth:`save_model` by detecting a near-immediate
        re-POST and short-circuiting it, so no orphan tokens are minted
        on browser reload.
        """
        plaintext: str | None = getattr(request, _PLAINTEXT_ATTR, None)
        # Defence-in-depth: clear the stashed value immediately after reading
        # so later response middleware / hooks that introspect arbitrary
        # request attributes (custom audit logging, error reporters) cannot
        # observe the raw Bearer.
        if hasattr(request, _PLAINTEXT_ATTR):
            delattr(request, _PLAINTEXT_ATTR)
        if not plaintext:
            return super().response_add(request, obj, post_url_continue)

        # Resolve through the *active* AdminSite namespace so the links point
        # at the correct admin instance under custom / multiple admin sites
        # (the hardcoded "admin:" namespace would target the default site
        # only).  ``self.admin_site`` is the AdminSite the ModelAdmin was
        # registered against.
        admin_namespace = self.admin_site.name
        changelist_url = reverse(
            f"{admin_namespace}:{obj._meta.app_label}_{obj._meta.model_name}_changelist",
            current_app=admin_namespace,
        )
        change_url = reverse(
            f"{admin_namespace}:{obj._meta.app_label}_{obj._meta.model_name}_change",
            args=(obj.pk,),
            current_app=admin_namespace,
        )
        body = format_html(
            "<!DOCTYPE html><html><head><title>Bearer token created</title>"
            "<meta name=\"robots\" content=\"noindex,nofollow\"></head>"
            "<body style=\"font-family:sans-serif;max-width:720px;margin:2em auto;\">"
            "<h1>Bearer token created for <em>{name}</em></h1>"
            "<p><strong>Copy this value now — it will not be shown again.</strong>"
            "  Refreshing or navigating away discards it permanently.</p>"
            "<pre style=\"background:#f4f4f4;padding:1em;border:1px solid #ccc;"
            "word-break:break-all;white-space:pre-wrap;\"><code>{token}</code></pre>"
            "<p style=\"margin-top:2em;\">"
            "<a href=\"{change}\">Edit this token</a> &middot; "
            "<a href=\"{changelist}\">Back to tokens list</a></p></body></html>",
            name=obj.name,
            token=plaintext,
            change=change_url,
            changelist=changelist_url,
        )
        response = HttpResponse(body)
        # Defence-in-depth: forbid intermediaries and browsers from caching
        # the response body that contains the raw Bearer.
        response["Cache-Control"] = "no-store, max-age=0"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "no-referrer"
        return response
