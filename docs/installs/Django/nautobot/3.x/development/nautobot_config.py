"""
nautobot_config.py — reference Nautobot 3.x configuration for frisian-mcp.

This file demonstrates the validated **hardened** posture used in production
deployments of frisian-mcp on Nautobot.  It is a self-contained example you
can copy into your own Nautobot deployment and adapt — replace the
``your-host.example.com``-style placeholders with the real values for your
environment.

Posture summary:

* Hard authentication at the MCP gateway (``IsAuthenticated``).  No anonymous
  tier.  Every caller must present a valid Bearer token (frisian-mcp static
  token *or* OAuth-issued access token).
* OAuth dynamic-client-registration locked down: operators pre-register every
  OAuth client via the Django admin and share the ``client_id`` out-of-band.
* OAuth ``.well-known`` discovery metadata hidden (``PUBLIC_DISCOVERY=False``)
  so discovery-first MCP clients fall back cleanly to their statically
  configured Bearer instead of failing with "Incompatible auth server: does
  not support dynamic client registration."  Pre-registered OAuth clients
  continue to work because they were given hard-coded endpoint URLs.
* Permission-aware discovery (``PERMISSION_AWARE_DISCOVERY=True``) so each
  agent's ``tools/list`` reflects what THAT principal is allowed to call.
  A read-tier token sees only ``list``/``retrieve`` actions; write actions
  are hidden from discovery, not just blocked at execution.

See ``installs/Django/nautobot/3.x/install.md`` for the full installation
guide.
"""

import os

# pylint: disable=wildcard-import,unused-wildcard-import
from nautobot.core.settings import *  # noqa: F403
from nautobot.core.settings_funcs import is_truthy  # noqa: F401

# ---------------------------------------------------------------------------
# Core Nautobot settings (secrets / DB / Redis from env).
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv(
    "NAUTOBOT_SECRET_KEY",
    "replace-with-strong-random-secret-rotate-in-production",
)
ALLOWED_HOSTS = os.getenv("NAUTOBOT_ALLOWED_HOSTS", "*").split()
DEBUG = is_truthy(os.getenv("NAUTOBOT_DEBUG", "False"))

DATABASES = {
    "default": {
        "NAME": os.getenv("NAUTOBOT_DB_NAME", "nautobot"),
        "USER": os.getenv("NAUTOBOT_DB_USER", "nautobot"),
        "PASSWORD": os.getenv("NAUTOBOT_DB_PASSWORD", "changeme"),
        "HOST": os.getenv("NAUTOBOT_DB_HOST", "db"),
        "PORT": os.getenv("NAUTOBOT_DB_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("NAUTOBOT_DB_TIMEOUT", "300")),
        "ENGINE": "django.db.backends.postgresql",
    }
}

REDIS_HOST = os.getenv("NAUTOBOT_REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("NAUTOBOT_REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("NAUTOBOT_REDIS_PASSWORD", "")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1",
        "TIMEOUT": 300,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

# ---------------------------------------------------------------------------
# Optional Nautobot plugins.  Leave the list empty for a vanilla Nautobot 3.x
# deployment; add plugins your environment uses (e.g. nautobot-golden-config,
# nautobot-app-dns-models).  Plugin ViewSets are picked up by frisian-mcp's
# discovery automatically once they are installed.
# ---------------------------------------------------------------------------
PLUGINS: list[str] = []
PLUGINS_CONFIG: dict = {}

# ---------------------------------------------------------------------------
# frisian-mcp app + contribs.
# ---------------------------------------------------------------------------
EXTRA_INSTALLED_APPS = [
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
]

# ---------------------------------------------------------------------------
# frisian-mcp settings — hardened single-endpoint posture.
# ---------------------------------------------------------------------------

# MCP endpoint mount path.  Auto-mounted by ``frisian_mcp.apps`` at
# ``AppConfig.ready()`` — no URL conf changes required in Nautobot.
FRISIAN_MCP_PATH = "api/mcp"

# Hard auth.  Anonymous = 401.
FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# Authenticator chain.  Order is no longer load-bearing as of frisian-mcp
# 1.0.11 (both classes return ``None`` on lookup-miss instead of raising
# ``AuthenticationFailed``).  FrisianMcpTokenAuthentication first is the
# conventional Nautobot order — it keeps the WWW-Authenticate response shape
# consistent for static-token connectors (Claude Code, Codex, Gemini CLI).
#
# Note: frisian-mcp's authenticators MUST come before Nautobot's NTC
# ``TokenAuthentication`` in the *global* DRF chain.  NTC eats any ``Bearer``
# header it sees and rejects frisian-mcp tokens with ``AuthenticationFailed``,
# so leaving NTC ahead of frisian-mcp's classes is not safe.
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

# Public origin = LB-terminated URL (no port if a reverse proxy is in front).
# ``FRISIAN_MCP_TRUSTED_PROXY_COUNT`` must match the number of trusted proxy
# hops in front of Nautobot so the package builds correct issuer URLs from
# ``X-Forwarded-*`` headers.
FRISIAN_MCP_OAUTH_ISSUER = os.getenv(
    "FRISIAN_MCP_OAUTH_ISSUER", "https://your-nautobot.example.com"
)
FRISIAN_MCP_TRUSTED_PROXY_COUNT = int(
    os.getenv("FRISIAN_MCP_TRUSTED_PROXY_COUNT", "1")
)

# HMAC key — set via env var.  Rotating Nautobot's ``SECRET_KEY`` without
# setting this would invalidate every issued frisian-mcp token and OAuth
# client secret because they are HMAC-signed against ``SECRET_KEY`` by
# default.  Setting a dedicated ``FRISIAN_MCP_HMAC_KEY`` decouples the two
# secrets.
FRISIAN_MCP_HMAC_KEY = os.getenv("FRISIAN_MCP_HMAC_KEY", "")

# OAuth lifecycle — closed.  Operator pre-registers every client via the
# Django admin (Plugins → frisian-mcp → OAuth Clients) and shares the
# ``client_id`` out-of-band.  No anonymous /oauth/register/, no PKCE
# auto-create on unknown ``client_id``, consent screen on first authorize.
#
# Leaving ANY of the three flags below set to ``True`` re-opens an anonymous
# walk-up path: any caller can POST to ``/oauth/register/``, complete PKCE,
# and receive a Bearer token without operator intervention.  Pair this with
# the ``PKCE_DEFAULT_PERMISSION`` setting below.
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False

# Safest default tier when a client doesn't specify one.  ``read`` ensures a
# misconfigured operator does not accidentally hand out write or admin
# permissions on walk-up.
FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION = "read"

# 1-year token expiry suits long-running agents.  Shorten in environments
# with stricter rotation requirements.
FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 365

# Hide OAuth ``.well-known`` discovery metadata.  Returns JSON 404 from
# ``/.well-known/oauth-authorization-server`` and
# ``/.well-known/oauth-protected-resource`` so discovery-first MCP clients
# fall back to their configured static Bearer instead of bouncing with
# "Incompatible auth server: does not support dynamic client registration."
# Pre-registered OAuth clients (e.g. the Claude.ai connector configured via
# the Django admin) continue to work with their hard-coded endpoint URLs.
FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False

# Permission-aware discovery: rebuild dispatcher action enums per-request so
# the agent only SEES actions it's allowed to call.  A read-tier token gets
# ``{list, retrieve}``; write actions are hidden from ``tools/list``, not
# just blocked at execution.
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
FRISIAN_MCP_PERMISSION_ADAPTER = (
    "frisian_mcp.contrib.permissions.exempt_view_adapter.ExemptViewPermissionAdapter"
)

# Synthesise ``bulk_create`` for every resource.  Nautobot's BulkModelViewSet
# exposes ``bulk_update`` / ``bulk_partial_update`` / ``bulk_destroy`` as
# ``@action`` methods but NOT ``bulk_create`` — frisian-mcp synthesises it.
FRISIAN_MCP_BULK_CREATE_RESOURCES = "*"

# ---------------------------------------------------------------------------
# Dispatcher groups — bundle ~1,900 ViewSet actions into ~10 topic-level
# tools so agents see a manageable surface.  Basenames follow DRF convention:
# ``Model._meta.object_name.lower()``.
#
# Trim or extend per your installed plugins.  Groups whose basenames don't
# match any registered resource emit a startup warning and are silently
# skipped, so listing extras costs nothing.
# ---------------------------------------------------------------------------
FRISIAN_MCP_DISPATCH_GROUPS = {
    "dcim": [
        "device", "rack", "rackgroup", "rackreservation",
        "interface", "interfacetemplate",
        "cable", "location", "locationtype",
        "manufacturer", "devicetype", "devicefamily",
        "platform", "inventoryitem",
        "consoleport", "consoleporttemplate",
        "consoleserverport", "consoleserverporttemplate",
        "powerport", "powerporttemplate",
        "poweroutlet", "poweroutlettemplate",
        "powerfeed", "powerpanel",
        "frontport", "frontporttemplate",
        "rearport", "rearporttemplate",
        "module", "modulebay", "modulebaytemplate", "moduletype",
        "virtualchassis", "virtualdevicecontext",
        "softwareimagefile", "softwareversion",
        "connected_device",
    ],
    "ipam": [
        "ipaddress", "ipaddresstointerface",
        "prefix", "vlan", "vlangroup",
        "vrf", "routetarget", "namespace",
        "rir", "service",
    ],
    "circuits": [
        "circuit", "circuittype", "circuittermination",
        "provider", "providernetwork",
    ],
    "tenancy": [
        "tenant", "tenantgroup",
        "contact", "contactassociation", "team",
    ],
    "virtualization": [
        "cluster", "clustergroup", "clustertype",
        "virtualmachine", "vminterface",
    ],
    "wireless": [
        "wirelessnetwork", "radioprofile", "supporteddatarate",
    ],
    "cloud": [
        "cloudaccount", "cloudnetwork", "cloudnetworkprefixassignment",
        "cloudresourcetype", "cloudservice", "cloudservicenetworkassignment",
    ],
    "users": [
        "user", "group", "token", "objectpermission",
        "userconfig", "savedview",
    ],
    "extras": [
        "tag", "configcontext", "configcontextschema",
        "customfield", "customfieldchoice", "customlink",
        "computedfield",
        "relationship", "relationshipassociation",
        "dynamicgroup", "dynamicgroupmembership",
        "job", "jobresult", "joblogentry", "scheduledjob",
        "webhook", "gitrepository", "graphqlquery",
        "exporttemplate", "imageattachment",
        "role", "status", "secret", "secretsgroup",
        "note", "objectchange",
    ],
}

# ---------------------------------------------------------------------------
# CORS for browser-based AI clients (optional).  Add the origins of any
# MCP clients that connect from a browser session.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://chatgpt.com",
    "https://grok.com",
]
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_SAMESITE = None
