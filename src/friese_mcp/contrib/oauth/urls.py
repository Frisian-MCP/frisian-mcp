"""
URL patterns for friese_mcp.contrib.oauth.

Mount these in your project's ``urls.py``::

    from django.urls import include, path

    urlpatterns = [
        # OAuth token and registration endpoints
        path("oauth/", include("friese_mcp.contrib.oauth.urls")),

        # Well-known discovery endpoints (must be at server root)
        path(".well-known/", include("friese_mcp.contrib.oauth.wellknown_urls")),

        # MCP gateway
        path("mcp/", include("friese_mcp.urls")),
    ]

"""

from django.urls import path

from .views import RegistrationView, TokenView

app_name = "friese_mcp_oauth"

urlpatterns = [
    path("token/", TokenView.as_view(), name="token"),
    path("register/", RegistrationView.as_view(), name="register"),
]
