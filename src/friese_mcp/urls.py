"""
URL configuration for the friese-mcp gateway.

Include in the host project's root URLconf::

    from django.urls import include, re_path

    urlpatterns = [
        ...
        re_path(r"^mcp/?", include("friese_mcp.urls")),
    ]

Use ``re_path`` with an optional trailing slash so that MCP clients such as
Claude.ai and Cursor — which strip trailing slashes from the server URL — reach
the gateway directly without a 308 redirect.  Django's ``APPEND_SLASH``
mechanism issues a 308 for ``/mcp`` → ``/mcp/`` and MCP protocol clients do
not follow 308 redirects, causing the connection to fail silently.
"""

from django.urls import URLPattern, re_path

from friese_mcp.views import McpView

app_name: str = "friese_mcp"

urlpatterns: list[URLPattern] = [
    re_path(r"^$", McpView.as_view(), name="gateway"),
]
