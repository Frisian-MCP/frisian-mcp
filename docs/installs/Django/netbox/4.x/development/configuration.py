"""
configuration.py — reference NetBox 4.x configuration for frisian-mcp.

This file demonstrates the validated **hardened** posture used in production
deployments of frisian-mcp on NetBox.  It is a self-contained example you
can copy into your own NetBox deployment and adapt — replace the
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
* NetBox 4.x default-permissions override so agents see ONLY what the
  operator explicitly grants them.  Without this, NetBox's stock
  ``DEFAULT_PERMISSIONS`` auto-grants every authenticated user CRUD on
  bookmarks, notifications, subscriptions, and API tokens.

See ``installs/Django/netbox/4.x/install.md`` for the full installation
guide, including the plugin-wrapper installation and Docker harness.
"""

import os

# ---------------------------------------------------------------------------
# Required NetBox settings (secrets / DB / Redis from env).
# ---------------------------------------------------------------------------
ALLOWED_HOSTS = os.getenv("NETBOX_ALLOWED_HOSTS", "*").split()

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("NETBOX_DB_NAME", "netbox"),
        "USER": os.getenv("NETBOX_DB_USER", "netbox"),
        "PASSWORD": os.getenv("NETBOX_DB_PASSWORD", "changeme"),
        "HOST": os.getenv("NETBOX_DB_HOST", "db"),
        "PORT": os.getenv("NETBOX_DB_PORT", ""),
        "CONN_MAX_AGE": 300,
    }
}

REDIS = {
    "tasks": {
        "HOST": os.getenv("NETBOX_REDIS_HOST", "redis"),
        "PORT": int(os.getenv("NETBOX_REDIS_PORT", "6379")),
        "USERNAME": "",
        "PASSWORD": os.getenv("NETBOX_REDIS_PASSWORD", ""),
        "DATABASE": 0,
        "SSL": False,
    },
    "caching": {
        "HOST": os.getenv("NETBOX_REDIS_HOST", "redis"),
        "PORT": int(os.getenv("NETBOX_REDIS_PORT", "6379")),
        "USERNAME": "",
        "PASSWORD": os.getenv("NETBOX_REDIS_PASSWORD", ""),
        "DATABASE": 1,
        "SSL": False,
    },
}

SECRET_KEY = os.getenv(
    "NETBOX_SECRET_KEY",
    "replace-with-strong-random-secret-rotate-in-production",
)

# ---------------------------------------------------------------------------
# Optional NetBox settings.
# ---------------------------------------------------------------------------
DEBUG = False

# ---------------------------------------------------------------------------
# NetBox 4.x DEFAULT_PERMISSIONS override.
#
# NetBox 4.x ships a default ``DEFAULT_PERMISSIONS`` dict that auto-grants
# every authenticated user CRUD on their own bookmarks, notifications,
# subscriptions, and API tokens via a ``{"user": "$user"}`` constraint.  For
# a managed-agent MCP surface the operator wants EXPLICIT control: agents
# see only what was explicitly granted to their OAuthClient's user.
# Override to an empty dict so no implicit self-service grants are added to
# any user.  The admin UI still works for the NetBox superuser (which
# bypasses permission checks entirely via ``is_superuser``).
# ---------------------------------------------------------------------------
DEFAULT_PERMISSIONS: dict = {}

# ---------------------------------------------------------------------------
# Plugins — loads frisian-mcp via the thin NetBox plugin wrapper.
#
# NetBox does not use Django's standard ``INSTALLED_APPS`` list for
# third-party additions.  The ``frisian_mcp_netbox`` wrapper registers
# ``frisian_mcp``, ``frisian_mcp.contrib.oauth``, and
# ``frisian_mcp.contrib.tokens`` via ``PluginConfig.django_apps``, and
# propagates ``FRISIAN_MCP_*`` settings from this file into
# ``django.conf.settings`` at startup.
# ---------------------------------------------------------------------------
PLUGINS = ["frisian_mcp_netbox"]

# ---------------------------------------------------------------------------
# frisian-mcp settings — hardened single-endpoint posture.
# ---------------------------------------------------------------------------

# MCP endpoint mount path.  Auto-mounted by ``frisian_mcp.apps`` at
# ``AppConfig.ready()`` — no URL conf changes required in NetBox.
FRISIAN_MCP_PATH = "api/mcp"

# Hard auth.  Anonymous = 401.
FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# Authenticator chain.  **ALWAYS list FrisianMcpTokenAuthentication BEFORE
# OAuthTokenAuthentication when both are present.**  As of frisian-mcp 1.0.11
# both classes return ``None`` on lookup-miss (so either order works for
# correctness), but the FIRST authenticator in the chain emits the
# WWW-Authenticate challenge on 401 responses.  Token-first emits a bare
# ``Bearer`` challenge, which static-token MCP clients fall back to cleanly.
# OAuth-first emits ``Bearer realm="...", resource_metadata="..."`` which
# nudges discovery-first clients into the OAuth cascade — a footgun the
# moment you add a static-token coding agent.  Tokens-first serves both
# client classes correctly.
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

# Public origin = LB-terminated URL.  ``FRISIAN_MCP_TRUSTED_PROXY_COUNT``
# must match the number of trusted reverse-proxy hops in front of NetBox.
FRISIAN_MCP_OAUTH_ISSUER = os.getenv(
    "FRISIAN_MCP_OAUTH_ISSUER", "https://your-netbox.example.com"
)
FRISIAN_MCP_TRUSTED_PROXY_COUNT = int(
    os.getenv("FRISIAN_MCP_TRUSTED_PROXY_COUNT", "1")
)

# HMAC key — set via env var.  Required to decouple frisian-mcp token /
# client-secret HMAC digests from NetBox's ``SECRET_KEY``.  Generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
FRISIAN_MCP_HMAC_KEY = os.getenv("FRISIAN_MCP_HMAC_KEY", "")

# OAuth lifecycle — closed.  Operator pre-registers every client via the
# Django admin (Plugins → frisian-mcp → OAuth Clients) and shares the
# ``client_id`` out-of-band.  Leaving ANY of the three flags below set to
# ``True`` re-opens an anonymous walk-up path: any caller can POST to
# ``/oauth/register/``, complete PKCE, and receive a Bearer token without
# operator intervention.
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False

# Safest default tier when a client doesn't specify one.
FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION = "read"

# 1-year token expiry suits long-running agents.  Shorten in environments
# with stricter rotation requirements.
FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 365

# Hide OAuth ``.well-known`` discovery metadata.  Returns JSON 404 from
# ``/.well-known/oauth-authorization-server`` and
# ``/.well-known/oauth-protected-resource`` so discovery-first MCP clients
# fall back to their configured static Bearer.  Pre-registered OAuth
# clients continue to work with hard-coded endpoint URLs.
FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False

# Permission-aware discovery: rebuild dispatcher action enums per-request so
# each agent's ``tools/list`` reflects what THAT principal is allowed to call.
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True

# Discovery denylist — NetBox has a small number of endpoints with no model
# behind them (``rq*`` wraps django-rq queue objects; ``connected_device`` is
# a derived cable-trace query; ``userconfig`` is a self-service singleton).
# Discovery can't attach permission metadata to them, so the
# ``PERMISSION_AWARE_DISCOVERY`` per-user filter's early-exit rule
# ("no perm metadata = always visible") lets them through unconditionally
# regardless of user permissions.  Hide them via the package denylist.
FRISIAN_MCP_TOOL_DENYLIST = [
    "rqqueue_list", "rqqueue_retrieve",
    "rqworker_list", "rqworker_retrieve",
    "rqtask_list", "rqtask_retrieve",
    "rqtask_delete", "rqtask_enqueue", "rqtask_requeue", "rqtask_stop",
    "connected_device_list",
    "userconfig_list",
]

# ---------------------------------------------------------------------------
# Dispatcher groups — bundle NetBox's REST surface into one tool per app.
# Basenames follow DRF convention: ``Model._meta.object_name.lower()``.
# Three explicit exceptions in NetBox: ``connected-device`` → ``"connected_device"``,
# scripts endpoint → ``"script"``, user config endpoint → ``"userconfig"``.
# ---------------------------------------------------------------------------
FRISIAN_MCP_DISPATCH_GROUPS = {
    "dcim": [
        "region", "sitegroup", "site", "location",
        "rackgroup", "racktype", "rackrole", "rack", "rackreservation",
        "manufacturer", "devicetype", "moduletype", "moduletypeprofile",
        "consoleporttemplate", "consoleserverporttemplate",
        "powerporttemplate", "poweroutlettemplate",
        "interfacetemplate", "frontporttemplate", "rearporttemplate",
        "modulebaytemplate", "devicebaytemplate", "inventoryitemtemplate",
        "devicerole", "platform", "device", "virtualdevicecontext", "module",
        "consoleport", "consoleserverport", "powerport", "poweroutlet",
        "interface", "frontport", "rearport",
        "modulebay", "devicebay", "inventoryitem", "inventoryitemrole",
        "macaddress",
        "cable", "cabletermination", "cablebundle",
        "virtualchassis",
        "powerpanel", "powerfeed",
        "connected_device",
    ],
    "ipam": [
        "asn", "asnrange", "vrf", "routetarget", "rir", "aggregate",
        "role", "prefix", "iprange", "ipaddress",
        "fhrpgroup", "fhrpgroupassignment",
        "vlangroup", "vlan", "vlantranslationpolicy", "vlantranslationrule",
        "servicetemplate", "service",
    ],
    "circuits": [
        "provider", "provideraccount", "providernetwork",
        "circuittype", "circuit", "circuittermination",
        "circuitgroup", "circuitgroupassignment",
        "virtualcircuit", "virtualcircuittype", "virtualcircuittermination",
    ],
    "tenancy": [
        "tenantgroup", "tenant",
        "contactgroup", "contactrole", "contact", "contactassignment",
    ],
    "virtualization": [
        "clustertype", "clustergroup", "cluster",
        "virtualmachinetype", "virtualmachine",
        "vminterface", "virtualdisk",
    ],
    "vpn": [
        "ikepolicy", "ikeproposal",
        "ipsecpolicy", "ipsecproposal", "ipsecprofile",
        "tunnelgroup", "tunnel", "tunneltermination",
        "l2vpn", "l2vpntermination",
    ],
    "wireless": [
        "wirelesslangroup", "wirelesslan", "wirelesslink",
    ],
    "extras": [
        "eventrule", "webhook",
        "customfield", "customfieldchoiceset", "customlink",
        "exporttemplate", "savedfilter", "tableconfig",
        "bookmark", "notification", "notificationgroup", "subscription",
        "tag", "taggeditem", "imageattachment", "journalentry",
        "configcontext", "configcontextprofile", "configtemplate",
        "script", "scriptmodule",
    ],
    "users": [
        "user", "group", "token", "objectpermission",
        "ownergroup", "owner", "userconfig",
    ],
    "core": [
        "datasource", "datafile",
        "rqqueue", "rqworker", "rqtask",
        "job", "objectchange", "objecttype",
        "configrevision", "managedfile", "autosyncrecord",
        "background",
    ],
}

# ---------------------------------------------------------------------------
# CORS for browser-based AI clients (optional).
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://chatgpt.com",
    "https://grok.com",
]
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_SAMESITE = None
