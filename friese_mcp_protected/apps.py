"""
Registers a second, auth-required MCP endpoint at api/breakingprod.

Mirrors the ProtectedGatewayView pattern in friese-mcp-api: same McpView
machinery, but get_permissions() always enforces IsAuthenticated regardless of
the global FRIESE_MCP_PERMISSION_CLASSES setting (which is AllowAny on api/mcp).

Add to INSTALLED_APPS in nautobot_config.py:
    INSTALLED_APPS.append("friese_mcp_protected")
"""

from django.apps import AppConfig


class FrieseMcpProtectedConfig(AppConfig):
    name = "friese_mcp_protected"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.urls import get_resolver, re_path
        from rest_framework.permissions import IsAuthenticated

        from friese_mcp.views import McpView

        class ProtectedMcpView(McpView):
            def get_permissions(self) -> list:  # type: ignore[override]
                return [IsAuthenticated()]

        resolver = get_resolver()

        # Avoid double-registration on autoreload.
        _MARKER = "_friese_mcp_protected_registered"
        if any(getattr(p, _MARKER, False) for p in resolver.url_patterns):
            return

        pattern = re_path(r"^api/breakingprod/?$", ProtectedMcpView.as_view())
        setattr(pattern, _MARKER, True)
        resolver.url_patterns.insert(0, pattern)
