"""Django admin registration for FrisianMcpToken."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.html import format_html

from .models import FrisianMcpToken

_PLAINTEXT_ATTR = "_frisian_mcp_plaintext_token"


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
        """Save the token; stash plaintext on the request for ``response_add`` to surface."""
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
        On first create with a stashed plaintext, render it inline once.

        This deliberately departs from Django admin's Post/Redirect/Get
        (PRG) pattern: a normal add-flow would return a 302 redirect to a
        GET-only confirmation page, which would survive a browser refresh
        cleanly.  Implementing PRG here requires somewhere for the redirect
        target to read the plaintext from — session, cache, or a query
        string — all of which re-introduce the exact transport-store
        persistence we explicitly rejected when designing this flow.

        Trade-offs:

        * **Cost of non-PRG**: a deliberate browser refresh on this page
          re-POSTs the add form.  ``name`` has no unique constraint and
          ``token`` is regenerated per save, so the refresh succeeds and
          mints a NEW token (different raw + HMAC, same name).  The
          orphaned row is harmless and visible in the changelist for
          deletion.  Two warnings already discourage refresh: the inline
          "Refreshing... discards it permanently" copy below, and the
          browser's native "Confirm form resubmission" dialog.

        * **Cost of PRG**: the raw Bearer would live in session or cache
          storage between the POST and the redirected GET, which is the
          persistence window we explicitly engineered away.

        Of the two, the orphan-row risk on accidental refresh is lower
        impact than persisting the raw secret in a transport store.
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
