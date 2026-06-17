"""
Production settings for running Paperless-ngx with frisian-mcp installed.

Usage:
    Set the following environment variables in paperless.conf or your
    container environment, then point Django at this file:

        DJANGO_SETTINGS_MODULE=paperless_frisian_mcp
        FRISIAN_MCP_OAUTH_ISSUER=https://your-paperless.example.com
        FRISIAN_MCP_HMAC_KEY=<strong-random-secret>

    All other values should be set in your existing paperless.conf.
    This file only adds frisian-mcp configuration on top of the base
    Paperless-ngx settings — no Paperless-ngx source files are modified.
"""

import os

from paperless.settings import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

INSTALLED_APPS.append("frisian_mcp")  # noqa: F405
INSTALLED_APPS.append("frisian_mcp.contrib.oauth")  # noqa: F405

# Mount path for the MCP endpoint.
FRISIAN_MCP_PATH = "mcp"

# Require authentication for all MCP requests.
# Set to "read" to allow unauthenticated callers read-only access.
FRISIAN_MCP_UNAUTHENTICATED_TIER = None

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# Static API keys (token-class) ALWAYS come before OAuth.  The first
# authenticator emits the WWW-Authenticate challenge on 401, and a bare
# `Bearer` challenge lets static-token MCP clients fall back cleanly to
# their configured Bearer instead of being nudged into the OAuth discovery
# cascade by an OAuth-first `Bearer realm=..., resource_metadata=...`
# challenge.
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

# All MCP requests must be authenticated.
FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# The Django user that OAuth service principals run as.
# Set this to a dedicated service account with the minimum required permissions.
# Avoid pointing at a superuser in production.
FRISIAN_MCP_OAUTH_SERVICE_USER = os.environ.get("FRISIAN_MCP_SERVICE_USER", "admin")

# ---------------------------------------------------------------------------
# OAuth 2.0
# ---------------------------------------------------------------------------

# Public base URL of your Paperless-ngx instance.
# Used to build well-known discovery and token endpoint URLs.
FRISIAN_MCP_OAUTH_ISSUER = os.environ.get("FRISIAN_MCP_OAUTH_ISSUER", "")

# HMAC key for signing client secrets and access tokens.
# Must be set in production. Generate with: python -c "import secrets; print(secrets.token_hex(32))"
FRISIAN_MCP_HMAC_KEY = os.environ.get("FRISIAN_MCP_HMAC_KEY", "")

# Dynamic client registration is closed by default.
# Set FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=true only if you want AI clients
# to self-register. Requires an is_approved gate — see the security guide.
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False

# Show a consent screen before issuing tokens.
# Set to True only in dev/demo environments.
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False

# PKCE auto-registration: disabled in production.
# Pre-register OAuth clients via Django admin instead.
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False

# ---------------------------------------------------------------------------
# Reverse Proxy
# ---------------------------------------------------------------------------

# Set to True when Paperless-ngx runs behind nginx, Caddy, or a cloud
# load balancer. Reads X-Forwarded-Proto and X-Forwarded-Host for
# issuer URL construction and OAuth redirect URI validation.
FRISIAN_MCP_TRUST_PROXY = os.environ.get("FRISIAN_MCP_TRUST_PROXY", "false").lower() == "true"

# Trusted origins for CSRF. Add your public domain and any AI client origins.
CSRF_TRUSTED_ORIGINS = [
    os.environ.get("FRISIAN_MCP_OAUTH_ISSUER", ""),
]

# CORS for browser-based AI client sessions.
CORS_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://chatgpt.com",
    "https://grok.com",
]
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_SAMESITE = None

# ---------------------------------------------------------------------------
# Dispatch Groups
# ---------------------------------------------------------------------------
# Bundles Paperless-ngx's 131 tools (20 ViewSets) into 7 topic-level
# dispatcher tools. Reduces initial context from ~33k tokens to ~2k tokens.
# Agents call a group tool with action="help" for progressive discovery.

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

# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

# Cache the tools/list response for 5 minutes.
# Recommended for any production deployment to avoid recomputing on
# every MCP client connection.
FRISIAN_MCP_TOOLS_LIST_CACHE_TTL = 300
