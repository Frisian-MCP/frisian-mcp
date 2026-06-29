"""Helpers for the T9 first-time consent gate (M-oauth-auto-approve-debug-default).

Provides the public log-event name constants, the process-local throttle for
the prior-consent fast-path log, and the consent-gate decision helpers used
by :class:`frisian_mcp.contrib.oauth.views.AuthorizeView`.  The helpers live
in this sibling module so ``views.py`` stays under pylint's
``too-many-lines`` cap and the consent-record query logic is independently
testable.

See ADR: "Unauthenticated PKCE authorize-path: request inputs are never
authority."
"""

from __future__ import annotations

import logging
from typing import Any

from django.http import HttpRequest
from django.shortcuts import render

from .models import OAuthAuthorizeConsent, OAuthClient

logger = logging.getLogger("frisian_mcp.contrib.oauth.views")


# T9 canonical log-event names.  Public symbols so tests and external log
# consumers can import the constants rather than hard-coding the strings.
OAUTH_AUTHORIZE_CONSENT_REQUIRED: str = "oauth_authorize_consent_required"
OAUTH_AUTHORIZE_CONSENT_DENIED: str = "oauth_authorize_consent_denied"
OAUTH_AUTHORIZE_AUTO_APPROVED_ON_PRIOR_CONSENT: str = (
    "oauth_authorize_auto_approved_on_prior_consent"
)

# Process-local throttle: emit the "auto-approved on prior consent" INFO log
# at most once per ``(user_id, client_id, redirect_uri, scope)`` tuple per
# process lifetime.  Bounded by distinct tuples seen; not persisted; not
# shared across workers.  Matches the throttle pattern used by T7 and T8.
LOGGED_PRIOR_CONSENT_APPROVALS: set[tuple[int, str, str, str]] = set()


def render_consent_form(
    request: HttpRequest,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> Any:
    """Render the consent template with the hidden form fields."""
    return render(
        request,
        "frisian_mcp/oauth/authorize.html",
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "state": state,
        },
    )


def has_prior_consent(request: HttpRequest, client_id: str, redirect_uri: str) -> bool:
    """Return ``True`` iff a stored consent matches ``(user, client_id, redirect_uri, scope)``.

    Scope is derived from the stored ``OAuthClient.permission`` value
    (post-T7, this is operator-set authority, never a request input).
    Anonymous requests cannot have prior consent because the consent
    record is keyed by user FK.  Requests missing ``request.user`` (e.g.
    test harnesses that bypass ``AuthenticationMiddleware``) are treated
    as anonymous.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    try:
        client = OAuthClient.objects.get(client_id=client_id, is_active=True)
    except OAuthClient.DoesNotExist:
        return False
    return OAuthAuthorizeConsent.objects.filter(
        user_id=user.pk,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=client.permission,
    ).exists()


def record_consent(request: HttpRequest, client_id: str, redirect_uri: str, scope: str) -> None:
    """Persist a consent grant for an authenticated user, no-op for anonymous.

    Idempotent via ``get_or_create``.  Anonymous and middleware-less
    requests cannot store consent (no user FK); they fall back to the
    consent-form-every-time path.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return
    OAuthAuthorizeConsent.objects.get_or_create(
        user_id=user.pk,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
    )


def log_consent_required(client_id: str, redirect_uri: str, *, reason: str) -> None:
    """Emit the ``oauth_authorize_consent_required`` INFO log."""
    logger.info(
        OAUTH_AUTHORIZE_CONSENT_REQUIRED,
        extra={"client_id": client_id, "redirect_uri": redirect_uri, "reason": reason},
    )


def log_consent_denied(client_id: str, redirect_uri: str) -> None:
    """Emit the ``oauth_authorize_consent_denied`` WARNING log."""
    logger.warning(
        OAUTH_AUTHORIZE_CONSENT_DENIED,
        extra={"client_id": client_id, "redirect_uri": redirect_uri},
    )


def log_auto_approved_on_prior_consent(
    request: HttpRequest, client_id: str, redirect_uri: str
) -> None:
    """Emit the auto-approve-on-prior-consent INFO log, throttled per tuple per process.

    Throttle key is ``(user_id, client_id, redirect_uri, scope)``.  Anonymous
    requests never reach this branch (``has_prior_consent`` requires an
    authenticated user), so ``request.user.pk`` is always populated here.
    """
    try:
        client = OAuthClient.objects.get(client_id=client_id, is_active=True)
    except OAuthClient.DoesNotExist:  # pragma: no cover - defensive
        return
    user_pk = request.user.pk
    if user_pk is None:  # pragma: no cover - authenticated users always have a pk
        return
    key = (int(user_pk), client_id, redirect_uri, client.permission)
    if key in LOGGED_PRIOR_CONSENT_APPROVALS:
        return
    LOGGED_PRIOR_CONSENT_APPROVALS.add(key)
    logger.info(
        OAUTH_AUTHORIZE_AUTO_APPROVED_ON_PRIOR_CONSENT,
        extra={
            "user_id": int(user_pk),
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": client.permission,
        },
    )
