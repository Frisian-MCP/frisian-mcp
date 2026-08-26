"""Namespaced patterns for :mod:`tests.urls_wellknown_partial` — auth-server only."""

from __future__ import annotations

from django.urls import path

from frisian_mcp.contrib.oauth.views import OAuthAuthorizationServerView

app_name = "frisian_mcp_oauth_wellknown"

urlpatterns = [
    path(
        "oauth-authorization-server",
        OAuthAuthorizationServerView.as_view(),
        name="oauth_authorization_server",
    ),
]
