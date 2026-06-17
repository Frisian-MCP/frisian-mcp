"""
Local dev settings for running paperless-ngx with frisian-mcp installed.

Usage:
    DJANGO_SETTINGS_MODULE=paperless_frisian_mcp_dev \
    PAPERLESS_SECRET_KEY=dev-secret-key \
    PAPERLESS_DBENGINE=sqlite \
    PAPERLESS_APPS=frisian_mcp \
    uv run python src/manage.py <command>
"""

import os

from paperless.settings import *  # noqa: F401, F403

# frisian-mcp core dev configuration
FRISIAN_MCP_PATH = "mcp"
# GPT strips the path and POSTs to the origin root — also serve McpView at /
FRISIAN_MCP_EXTRA_PATHS = [""]
FRISIAN_MCP_UNAUTHENTICATED_TIER = "admin"  # dev only: Claude.ai doesn't send Bearer token with tool calls
FRISIAN_MCP_ALLOW_UNAUTHENTICATED = True  # silences W001 — open endpoint is intentional in dev

# OAuth contrib — wired for PPN-4
INSTALLED_APPS.append("frisian_mcp.contrib.oauth")  # noqa: F405
FRISIAN_MCP_OAUTH_ISSUER = os.environ.get("FRISIAN_MCP_OAUTH_ISSUER", "http://localhost:8765")

# Trust ngrok tunnels for dev testing (wildcard subdomain, Django 4.0+)
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:8765",
    "https://*.ngrok-free.app",
    "https://*.ngrok.io",
]
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = True       # enables DCR endpoint
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = True      # PKCE clients skip pre-registration
FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION = "admin"
FRISIAN_MCP_OAUTH_AUTO_APPROVE = True            # skip consent form — dev only
# Paperless-ngx permission classes check user.id; the auto-detect fallback uses
# OAuthServicePrincipal (pk=None) if the superuser query fails, which causes 403s.
# Explicitly naming the default admin user avoids that silent failure path.
FRISIAN_MCP_OAUTH_SERVICE_USER = "admin"
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
    # Dev-only: unauthenticated requests (e.g. Claude.ai which omits Bearer) run as superuser
    "paperless_frisian_mcp_dev_auth.DevFallbackSuperuserAuthentication",
]

# Dispatch groups — designed from PPN-2 flat audit (53 read-tier tools, 20 ViewSets → 7 groups)
FRISIAN_MCP_DISPATCH_GROUPS = {
    # Core document management (UnifiedSearchViewSet + all custom actions)
    "documents": ["document"],
    # Document classification metadata
    "classification": ["correspondent", "documenttype", "tag", "storagepath", "customfield"],
    # Email ingestion pipeline
    "mail": ["mailaccount", "mailrule", "processedmail"],
    # Automation rules
    "workflow": ["workflow", "workflowtrigger", "workflowaction"],
    # Document sharing
    "sharing": ["sharelink", "sharelinkbundle"],
    # Users, groups, and application configuration
    "system": ["users", "groups", "applicationconfiguration"],
    # Tasks, logs, and saved views
    "monitoring": ["tasks", "logs", "savedview"],
}
