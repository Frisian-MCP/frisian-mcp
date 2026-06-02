"""
Well-known URL patterns for frisian_mcp.contrib.oauth.

Mount at ``"/.well-known/"`` in your project's ``urls.py``::

    path(".well-known/", include("frisian_mcp.contrib.oauth.wellknown_urls")),

"""

from django.urls import path

from .views import OAuthAuthorizationServerView, OAuthProtectedResourceView

app_name = "frisian_mcp_oauth_wellknown"

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
    # RFC 8707 / RFC 8414 §3: clients may append the resource path to
    # construct per-resource metadata URLs.  Return the same response
    # regardless of the suffix for both endpoints.
    path(
        "oauth-protected-resource/<path:resource>",
        OAuthProtectedResourceView.as_view(),
        name="oauth_protected_resource_path",
    ),
    path(
        "oauth-authorization-server/<path:resource>",
        OAuthAuthorizationServerView.as_view(),
        name="oauth_authorization_server_path",
    ),
]
