"""
Well-known URL patterns for friese_mcp.contrib.oauth.

Mount at ``"/.well-known/"`` in your project's ``urls.py``::

    path(".well-known/", include("friese_mcp.contrib.oauth.wellknown_urls")),

"""

from django.urls import path

from .views import OAuthAuthorizationServerView, OAuthProtectedResourceView

app_name = "friese_mcp_oauth_wellknown"

urlpatterns = [
    path(
        "oauth-authorization-server",
        OAuthAuthorizationServerView.as_view(),
        name="oauth_authorization_server",
    ),
    path(
        "oauth-protected-resource",
        OAuthProtectedResourceView.as_view(),
        name="oauth_protected_resource",
    ),
]
