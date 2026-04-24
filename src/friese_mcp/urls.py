"""
URL configuration for the friese-mcp gateway.

Include in the host project's root URLconf::

    from django.urls import include, path

    urlpatterns = [
        ...
        path("mcp/", include("friese_mcp.urls")),
    ]

This exposes a single endpoint at ``<prefix>/`` (e.g. ``/mcp/``) that handles
all JSON-RPC 2.0 traffic over HTTP POST.
"""

from django.urls import URLPattern, path

from friese_mcp.views import McpView

app_name: str = "friese_mcp"

urlpatterns: list[URLPattern] = [
    path("", McpView.as_view(), name="gateway"),
]
