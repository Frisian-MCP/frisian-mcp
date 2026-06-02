"""
URL patterns for frisian_mcp.contrib.oauth.

Mount these in your project's ``urls.py``::

    from django.urls import include, path

    urlpatterns = [
        # OAuth token and registration endpoints
        path("oauth/", include("frisian_mcp.contrib.oauth.urls")),

        # Well-known discovery endpoints (must be at server root)
        path(".well-known/", include("frisian_mcp.contrib.oauth.wellknown_urls")),

        # MCP gateway
        path("mcp/", include("frisian_mcp.urls")),
    ]

"""

from django.urls import path

from .views import AuthorizeView, RegistrationView, TokenView

app_name = "frisian_mcp_oauth"

urlpatterns = [
    path("authorize/", AuthorizeView.as_view(), name="authorize"),
    path("token/", TokenView.as_view(), name="token"),
    path("register/", RegistrationView.as_view(), name="register"),
]
