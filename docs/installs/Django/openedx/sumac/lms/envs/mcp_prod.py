"""
Production settings for running Open edX LMS with frisian-mcp installed.

Usage:
    Add to your production settings file (lms/envs/production.py or
    lms/envs/private.py) via import or copy-paste:

        from lms.envs.mcp_prod import *  # noqa

    Or set as a standalone override:

        DJANGO_SETTINGS_MODULE=lms.envs.mcp_prod

    Required environment variables:
        FRISIAN_MCP_OAUTH_ISSUER    — public base URL of your LMS (e.g. https://lms.example.com)
        FRISIAN_MCP_HMAC_KEY        — strong random secret for signing tokens
                                     (generate: python -c "import secrets; print(secrets.token_hex(32))")

    This file only adds frisian-mcp configuration on top of the base Open edX
    LMS settings. No Open edX source files are modified.
"""

import os

from lms.envs.production import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

INSTALLED_APPS = list(INSTALLED_APPS) + [  # noqa: F405
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
    "openedx_frisian_mcp",
]

# Mount path for the MCP endpoint.
FRISIAN_MCP_PATH = "mcp"

# Require authentication for all MCP requests.
# Set to "read" to allow unauthenticated callers read-only access.
FRISIAN_MCP_UNAUTHENTICATED_TIER = None

# ---------------------------------------------------------------------------
# Authentication
#
# ALWAYS list FrisianMcpTokenAuthentication BEFORE OAuthTokenAuthentication
# when both are present.  As of frisian-mcp 1.0.11 both classes return None
# on lookup-miss (so either order works for correctness), but the FIRST
# authenticator emits the WWW-Authenticate challenge on 401 responses.
# Tokens-first emits a bare `Bearer` challenge so static-token MCP clients
# (Claude Code, Codex, Gemini CLI) fall back cleanly to their configured
# Bearer.  OAuth-first emits `Bearer realm="...", resource_metadata="..."`
# which nudges discovery-first clients into the OAuth cascade — a footgun
# the moment you add a static-token coding agent.
# ---------------------------------------------------------------------------

FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# The Django user that OAuth service principals run as.
# Set to a dedicated service account with minimum required permissions.
# The default Open edX superuser is "edx" — replace with a non-superuser
# account scoped only to the resources you want to expose via MCP.
FRISIAN_MCP_OAUTH_SERVICE_USER = os.environ.get("FRISIAN_MCP_SERVICE_USER", "edx")

# Map Open edX user roles to MCP permission tiers.
FRISIAN_MCP_TOKEN_TIER_MAP = {
    "superuser": "read_write",
    "staff":     "read_write",
    "default":   "read",
}

# ---------------------------------------------------------------------------
# OAuth 2.0
# ---------------------------------------------------------------------------

# Public base URL of your LMS instance.
# Used to build well-known discovery and token endpoint URLs.
FRISIAN_MCP_OAUTH_ISSUER = os.environ.get("FRISIAN_MCP_OAUTH_ISSUER", "")

# HMAC key for signing client secrets and access tokens.
# Must be set in production. Generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
FRISIAN_MCP_HMAC_KEY = os.environ.get("FRISIAN_MCP_HMAC_KEY", "")

# Dynamic client registration is closed by default.
# Set to True only if you want AI clients to self-register.
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False

# Show a consent screen before issuing tokens.
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False

# PKCE auto-registration: disabled in production.
# Pre-register OAuth clients via Django admin instead.
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False

# ---------------------------------------------------------------------------
# Cache — required for OAuth PKCE
#
# Open edX's default devstack/test settings configure DummyCache, which breaks
# the OAuth PKCE authorization code flow. Production deployments already use
# Redis; verify the default cache backend is Redis before enabling OAuth.
# ---------------------------------------------------------------------------

# Verify CACHES["default"] uses Redis (not DummyCache or LocMemCache).
# Open edX typically configures this in cms/envs/production.py or via
# EDXAPP_CACHES environment variable. No override needed if Redis is already
# the default backend.

# ---------------------------------------------------------------------------
# Reverse Proxy
# ---------------------------------------------------------------------------

# Required when the LMS runs behind nginx, Caddy, or a cloud load balancer.
# Reads X-Forwarded-Proto and X-Forwarded-Host for issuer URL construction
# and OAuth redirect URI validation.
FRISIAN_MCP_TRUST_PROXY = os.environ.get("FRISIAN_MCP_TRUST_PROXY", "false").lower() == "true"

# Add your LMS domain and AI client origins to CSRF trusted origins.
_lms_issuer = os.environ.get("FRISIAN_MCP_OAUTH_ISSUER", "")
CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS) + [  # noqa: F405
    _lms_issuer,
    "https://claude.ai",
    "https://chatgpt.com",
    "https://grok.com",
]

# CORS for browser-based AI client sessions.
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_SAMESITE = None

# ---------------------------------------------------------------------------
# Dispatch Groups
# ---------------------------------------------------------------------------
# Bundles the LMS's 78 auto-discovered tools into 9 topic-level dispatcher
# tools. Reduces initial context from ~20k tokens to ~2k tokens.
# Agents call a group tool with action="help" for progressive discovery.

FRISIAN_MCP_DISPATCH_GROUPS = {
    # User accounts, preferences, agreements, name changes
    "users": [
        "accounts", "me", "user", "user_agreements", "userpreference", "name_change",
    ],
    # Course enrollments, entitlements, and credit
    "enrollments": [
        "enrollments", "entitlements", "creditcourse", "creditprovider",
    ],
    # LTI (Learning Tools Interoperability) — AGS grades + NRPS memberships
    "lti": [
        "lti_ags_view", "lti_nrps_memberships_view",
    ],
    # Generic data store and key-value pairs
    "data": [
        "data", "key_value",
    ],
    # Organizations and SAML SSO configuration
    "organizations": [
        "organization", "saml_configuration",
    ],
    # Peer assessment feedback
    "assessment": [
        "assessment_feedback",
    ],
    # Auth — token creation, account confirmation, email lookup
    "auth": [
        "create_token", "confirm", "search_emails",
    ],
    # User retirement / GDPR erasure pipeline
    "retirement": [
        "cancel_retirement", "retire", "retire_misc",
        "retirement_cleanup", "retirement_partner_report",
        "retirement_queue", "retirement_status",
        "retirements_by_status_and_date", "update_retirement_status",
    ],
    # Grade submission queue
    "xqueue": [
        "xqueue",
    ],
}

# ---------------------------------------------------------------------------
# Large Response Negotiation
# ---------------------------------------------------------------------------
# Open edX ViewSets cannot be decorated with @mcp_heavy without modifying
# platform source files. FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD provides the
# equivalent behavior at the gateway level: responses larger than this byte
# threshold are cached and returned as a continuation token rather than
# inline JSON, preventing context window exhaustion on large result sets.

FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 8_000  # bytes

# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

# Cache the tools/list response for 5 minutes.
# Recommended for any production deployment.
FRISIAN_MCP_TOOLS_LIST_CACHE_TTL = 300
