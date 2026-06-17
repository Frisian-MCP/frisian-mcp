# Installing frisian-mcp with NetBox

**Audience:** NetBox administrators adding MCP gateway support  
**Platform:** NetBox 4.x · Django 5.x · Python 3.12+

---

## Overview

frisian-mcp is a Django package that turns your existing Django REST Framework ViewSets into Model Context Protocol (MCP) tools with zero boilerplate. When installed in NetBox, every API endpoint the application exposes — devices, interfaces, IP addresses, circuits, VPNs, and more — automatically becomes callable by any MCP-compatible AI client.

NetBox exposes several hundred ViewSet actions across its DCIM, IPAM, circuits, virtualization, VPN, and extras surfaces. frisian-mcp's dispatch-group system bundles those into 9 topic-level tools so agents see a manageable surface rather than all operations at once.

---

## Prerequisites

| Requirement | Version |
|---|---|
| NetBox | 4.x |
| Python | 3.12 or newer |
| Django | 5.x (bundled with NetBox 4.x) |
| Django REST Framework | 3.14+ (bundled with NetBox 4.x) |

No additional infrastructure is required for the basic install. OAuth support requires a shared cache backend (Redis) in multi-worker deployments — NetBox already ships with Redis, so no additional setup is needed.

---

## NetBox Plugin System — Why a Wrapper Is Required

NetBox does not use Django's standard `INSTALLED_APPS` list for third-party additions. Instead, it exposes a `PLUGINS` setting that accepts `PluginConfig` subclasses. Third-party apps added directly to `INSTALLED_APPS` are not recognized by NetBox's initialization sequence.

Additionally, NetBox's `settings.py` only reads known NetBox configuration keys. Any `FRISIAN_MCP_*` settings placed in `configuration.py` are silently ignored unless explicitly propagated into `django.conf.settings`.

For these reasons, installing frisian-mcp into NetBox requires a thin plugin wrapper. The wrapper is included in this repository at `development/plugin_wrapper/`. It handles:

- Registering `frisian_mcp`, `frisian_mcp.contrib.oauth`, and `frisian_mcp.contrib.tokens` via `PluginConfig.django_apps`
- Propagating `FRISIAN_MCP_*` settings from `configuration.py` into `django.conf.settings` at startup
- Re-registering the MCP endpoint URL after settings are applied
- Patching NetBox's User model for Django admin compatibility (NetBox removed `is_staff`)

No NetBox source files are modified.

---

## Step 1 — Install the Packages

```bash
pip install frisian-mcp
```

Then install the plugin wrapper from the `development/plugin_wrapper/` directory of this documentation repository:

```bash
pip install ./development/plugin_wrapper/
```

For Docker-based deployments, add both installs to the Dockerfile or entrypoint script that runs before NetBox starts. See `development/docker-entrypoint.frisian-mcp.sh` for the reference entrypoint used during integration testing.

### Official netbox-docker image (netboxcommunity/netbox)

The official netbox-docker image (`netboxcommunity/netbox`) uses Python 3.14 and ships `uv` as the package manager. The Python venv does **not** include `pip`. Use `uv pip install` instead, and install `setuptools` first (frisian-mcp's build backend requires it):

The venv (`/opt/netbox/venv`) is owned by root. Run the install commands as root (`docker exec -u root` or `user: "0:0"` in docker-compose).

---

## Step 2 — Register the Plugin

In your `configuration.py`, add the plugin wrapper to the `PLUGINS` list:

```python
# configuration.py

PLUGINS = [
    "frisian_mcp_netbox",
]
```

If you have existing plugins, append rather than replace:

```python
PLUGINS = [
    "my_existing_plugin",
    "frisian_mcp_netbox",
]
```

The plugin wrapper automatically adds `frisian_mcp`, `frisian_mcp.contrib.oauth`, and `frisian_mcp.contrib.tokens` to `INSTALLED_APPS` via `PluginConfig.django_apps`. You do not need to add these manually.

---

## Step 3 — Configure Settings

All `FRISIAN_MCP_*` settings go in `configuration.py` alongside your existing NetBox settings. The plugin wrapper copies them into Django's settings at startup.

### netbox-docker: pass settings as environment variables

The official netbox-docker image loads configuration from multiple `.py` files in `/etc/netbox/config/`. You can add `FRISIAN_MCP_*` settings to any of those files (for example `plugins.py`), and the plugin wrapper will find them via `loaded_configurations`.

The simplest approach is to pass settings as **environment variables** in `docker-compose.override.yml`. The plugin wrapper reads `os.environ` first, so this works without touching any config file:

```yaml
# docker-compose.override.yml
services:
  netbox:
    environment:
      FRISIAN_MCP_HMAC_KEY: "your-secret-key"
      FRISIAN_MCP_OAUTH_ISSUER: "https://your-netbox.example.com"
      FRISIAN_MCP_PATH: "api/mcp"
```

> **Do not set `NETBOX_CONFIGURATION`** to point to a frisian-mcp config file. NetBox's own `settings.py` reads the `NETBOX_CONFIGURATION` environment variable to locate its primary config module. Overriding it causes NetBox to fail at startup with `Required parameter ALLOWED_HOSTS is missing from configuration`.

### Minimum configuration

The minimal configuration mounts frisian-mcp at `/api/mcp` and requires
authentication for every request — the default secure posture.  Add an MCP
token via the Django admin (Plugins → frisian-mcp → MCP Tokens) once the
plugin wrapper is installed.

```python
# configuration.py

PLUGINS = ["frisian_mcp_netbox"]

FRISIAN_MCP_PATH = "api/mcp"

FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

For an anonymous-readable surface (e.g. a public read-only demo), set
`FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"` and drop the `IsAuthenticated`
permission class.  Note this combination is **not** what most production
deployments want — the validated hardened posture in the next section is.

### Recommended production configuration

This is the **validated hardened posture** used in production deployments.
Hard authentication at the gateway, closed OAuth dynamic-client-registration,
hidden discovery metadata, per-user permission-aware tool surfacing, and a
NetBox-4.x default-permissions override so agents see only what the operator
explicitly grants.

```python
# configuration.py

PLUGINS = ["frisian_mcp_netbox"]

FRISIAN_MCP_PATH = "api/mcp"

# Hard auth at the gateway.  Anonymous = 401.
FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# Authentication chain.  **ALWAYS list FrisianMcpTokenAuthentication BEFORE
# OAuthTokenAuthentication when both are present.**  As of frisian-mcp 1.0.11
# both classes return None on lookup-miss (so either order works for
# correctness), but the FIRST authenticator in the chain emits the
# WWW-Authenticate challenge on 401 responses.  Token-first emits a bare
# `Bearer` challenge, which static-token MCP clients (Claude Code, Codex,
# Gemini CLI) accept and fall back to their configured Bearer cleanly.
# OAuth-first emits `Bearer realm="...", resource_metadata="..."` which
# nudges discovery-first clients into the OAuth cascade — fine if every
# client is an OAuth client, but a footgun the moment you add a static-token
# coding agent.  Tokens-first serves both client classes correctly.
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

# Public base URL — LB-terminated URL of your NetBox instance.
FRISIAN_MCP_OAUTH_ISSUER = "https://your-netbox.example.com"

# Trusted reverse-proxy hops in front of NetBox (1 for nginx, 2 for nginx
# behind a cloud load balancer).
FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1

# Dedicated HMAC key for signing client secrets and access tokens.
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
FRISIAN_MCP_HMAC_KEY = "replace-with-strong-random-secret"

# OAuth lifecycle — locked down.  Operator pre-registers every OAuth client
# via the Django admin (Plugins → frisian-mcp → OAuth Clients) and shares
# the client_id out-of-band.  Leaving ANY of the three flags below set to
# True re-opens an anonymous walk-up path: any caller can POST to
# /oauth/register/, complete PKCE, and receive a Bearer token without
# operator intervention.
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False

# Safest default tier when a client doesn't specify one.
FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION = "read"

# 1-year token expiry.  Shorten in environments with stricter rotation.
FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 365

# Hide OAuth .well-known discovery metadata.  Returns JSON 404 from the
# .well-known endpoints so discovery-first MCP clients fall back cleanly to
# their configured static Bearer.  Pre-registered OAuth clients continue
# to work with hard-coded endpoint URLs.
FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False

# Permission-aware discovery: rebuild dispatcher action enums per-request
# so each agent's tools/list reflects what THAT principal is allowed to
# call.  A read-tier token sees only list/retrieve; write actions are
# hidden from discovery, not just blocked at execution.
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True

# NetBox 4.x DEFAULT_PERMISSIONS override.  Stock NetBox auto-grants every
# authenticated user CRUD on their own bookmarks / notifications /
# subscriptions / API tokens via a {"user": "$user"} constraint.  For a
# managed-agent MCP surface the operator wants explicit control — agents
# see only what was explicitly granted to their OAuthClient's user.
DEFAULT_PERMISSIONS: dict = {}

# Discovery denylist — NetBox has a small set of endpoints with no model
# behind them (rq* wraps django-rq queue objects; connected_device is a
# derived cable-trace query; userconfig is a self-service singleton).
# Discovery can't attach permission metadata to these so the
# PERMISSION_AWARE_DISCOVERY per-user filter's early-exit rule
# ("no perm metadata = always visible") lets them through unconditionally.
# Hide them via the package denylist.
FRISIAN_MCP_TOOL_DENYLIST = [
    "rqqueue_list", "rqqueue_retrieve",
    "rqworker_list", "rqworker_retrieve",
    "rqtask_list", "rqtask_retrieve",
    "rqtask_delete", "rqtask_enqueue", "rqtask_requeue", "rqtask_stop",
    "connected_device_list",
    "userconfig_list",
]
```

A full reference config file (drop-in, with all dispatcher groups for
NetBox 4.x) is provided at
[`development/configuration.py`](development/configuration.py).

### CORS for AI clients

```python
CORS_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://chatgpt.com",
    "https://grok.com",
]
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_SAMESITE = None
```

---

## Step 4 — Run Migrations

frisian-mcp adds database tables for OAuth clients, tokens, and access tokens. Run migrations after adding the plugin:

```bash
python manage.py migrate
```

In a Docker deployment, this runs as part of the entrypoint script before the server starts.

---

## Step 5 — No NetBox Source Files Modified

frisian-mcp does not modify any NetBox source files.

The plugin wrapper's `ready()` method handles all wiring at startup — settings propagation, URL registration, and User model compatibility — entirely within the installed package. NetBox core code, models, serializers, views, and `urls.py` are untouched.

This means frisian-mcp is upgrade-safe. When NetBox releases a new version, frisian-mcp re-discovers the updated ViewSet tree on first request. The plugin wrapper's `ready()` method re-applies its patches against whatever version of NetBox is running.

The gateway will be available at:

```
https://your-netbox.example.com/api/mcp/
```

---

## Step 6 — Verify Startup

Start NetBox normally. Look for this line in the server output:

```
[frisian-mcp] registered N tools at /api/mcp/
```

The path shown reflects your `FRISIAN_MCP_PATH` setting. If `FRISIAN_MCP_PATH` is not set, the default path is `mcp` and the endpoint is `/mcp/` rather than `/api/mcp/`.

If you see `registered 0 tools`, verify that `frisian_mcp_netbox` appears in `PLUGINS` and that the plugin wrapper is installed in the Python environment NetBox is running from.

You may also see schema derivation warnings for a small number of NetBox ViewSets whose `get_serializer_class()` requires a live request object. These are cosmetic — the affected tools are still registered with an empty input schema and remain callable:

```
frisian_mcp: schema derivation failed for DeviceViewSet.create — falling back to empty schema.
```

---

## Step 7 — Configure Dispatch Groups (Recommended)

NetBox exposes a large ViewSet surface across DCIM, IPAM, circuits, and more. Dispatch groups compress this into one tool per NetBox app boundary, dramatically reducing the context an AI client loads on connection.

Add `FRISIAN_MCP_DISPATCH_GROUPS` to `configuration.py`. See `development/configuration.py` for the full reference grouping used during integration testing. A trimmed version covering the most common surfaces:

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    "dcim": [
        "region", "sitegroup", "site", "location",
        "rack", "rackrole", "racktype", "rackreservation",
        "manufacturer", "devicetype", "devicerole", "platform", "device",
        "interface", "cable", "powerpanel", "powerfeed",
        "virtualchassis", "module", "moduletype",
        "inventoryitem", "inventoryitemrole",
        "connected_device",
    ],
    "ipam": [
        "asn", "asnrange", "vrf", "routetarget", "rir", "aggregate",
        "role", "prefix", "iprange", "ipaddress",
        "vlangroup", "vlan", "servicetemplate", "service",
    ],
    "circuits": [
        "provider", "provideraccount", "providernetwork",
        "circuittype", "circuit", "circuittermination",
    ],
    "tenancy": [
        "tenantgroup", "tenant",
        "contactgroup", "contactrole", "contact", "contactassignment",
    ],
    "virtualization": [
        "clustertype", "clustergroup", "cluster",
        "virtualmachine", "vminterface", "virtualdisk",
    ],
    "vpn": [
        "tunnelgroup", "tunnel", "tunneltermination",
        "l2vpn", "l2vpntermination",
        "ikepolicy", "ipsecpolicy", "ipsecprofile",
    ],
    "wireless": [
        "wirelesslangroup", "wirelesslan", "wirelesslink",
    ],
    "extras": [
        "tag", "customfield", "customlink", "exporttemplate",
        "eventrule", "webhook", "configcontext", "configtemplate",
        "savedfilter", "journalentry", "script", "scriptmodule",
    ],
    "users": [
        "user", "group", "token", "objectpermission", "userconfig",
    ],
}
```

> **Full group listing:** `development/configuration.py` contains the complete dispatch group configuration including all VPN, wireless, and extras resources discovered during integration testing.

> **Basename tip:** Dispatch group basenames must match DRF's ViewSet basename — always `Model._meta.object_name.lower()`. Three explicit exceptions in NetBox: `connected-device` → `"connected_device"`, the scripts endpoint → `"script"`, and the user config endpoint → `"userconfig"`.

---

## Step 8 — Connect an MCP Client

The MCP gateway always expects the **`Bearer`** auth scheme — `Authorization:
Token <key>` is the scheme NetBox's own API uses, but it is NOT recognised
by any of the frisian-mcp authenticators.  A NetBox API token (v1 hex or v2
`nbt_<key>.<plaintext>` format) is therefore not usable at the MCP endpoint
in the recommended configuration, because that chain does not include
NetBox's `TokenAuthentication` class.  Use a frisian-mcp MCP token or an
OAuth-issued Bearer instead, as described below.

### Using a frisian-mcp MCP Token (recommended for static-token clients)

Generate a token in the Django admin (**Plugins → frisian-mcp → MCP Tokens
→ Add**) or via `manage.py shell`:

```python
from frisian_mcp.contrib.tokens.models import FrisianMcpToken
from django.contrib.auth import get_user_model

User = get_user_model()
token = FrisianMcpToken.objects.create(
    name="claude-code-agent",
    user=User.objects.get(username="admin"),
)
print(token.plaintext_token)  # shown ONCE; copy it now
```

Then point your MCP client at the gateway:

```json
{
  "mcpServers": {
    "netbox": {
      "type": "http",
      "url": "https://your-netbox.example.com/api/mcp/",
      "headers": {
        "Authorization": "Bearer <plaintext-token-from-above>"
      }
    }
  }
}
```

### Using a frisian-mcp Settings-Backed Static API Key

For internal agents or scripts where adding a DB row is overkill, configure
HMAC-hashed static keys in `configuration.py`:

```python
import hmac
import hashlib
from django.conf import settings

def _hash(raw: str) -> str:
    return hmac.new(
        settings.FRISIAN_MCP_HMAC_KEY.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()

FRISIAN_MCP_API_KEYS = {
    _hash("my-agent-key"):    "read_write",
    _hash("readonly-agent"):  "read",
}
```

Add `FrisianMcpApiKeyAuthentication` to your authenticator chain to enable
this path:

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

Keys are stored as HMAC-SHA256 digests (not raw values) so a leaked
`configuration.py` does not directly expose usable credentials.

### Using OAuth (Claude.ai, ChatGPT, Grok)

AI clients connect via the OAuth 2.1 PKCE authorization-code flow.  In the
hardened production posture (closed dynamic-client-registration), the
operator pre-registers each client via the Django admin:

1. Sign in to the NetBox admin and navigate to **Plugins → frisian-mcp →
   OAuth Clients → Add**.
2. Pick a permission tier (`read`, `read_write`, or `admin`), attach a
   default Django user via the `user` field (this is the principal the
   client's MCP requests run as), and submit.  A `client_id` and one-time
   `client_secret` are displayed on the success page.
3. Paste those values into the AI client's connector settings (Claude.ai:
   **Connect MCP Server → Advanced**; ChatGPT and Grok have equivalent
   forms).  Point the client URL at:

   ```
   https://your-netbox.example.com/api/mcp/
   ```

4. The client completes the PKCE flow against `/oauth/authorize/`, you
   approve the consent screen (because `FRISIAN_MCP_OAUTH_AUTO_APPROVE =
   False`), and the client receives an access token.

The `/.well-known/oauth-authorization-server` metadata is hidden under the
hardened posture (`FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False`) — clients
that try discovery-based onboarding will see a JSON 404 and either fall
back to the configured Bearer (static-token connectors) or require manual
client-credential entry as described above.

### Verifying the OAuth Flow

A standalone end-to-end OAuth test script is included at `development/test_oauth_flow.py`. It validates well-known discovery, PKCE authorization code flow, token exchange, and a live `tools/list` call against a running NetBox instance. Run it against your dev environment before deploying to production:

```bash
NETBOX_BASE_URL=http://localhost:8080 python development/test_oauth_flow.py
```

---

## Next Steps

- [Troubleshooting](../../../../troubleshooting/Django/netbox/4.x/troubleshooting.md) — common problems and solutions
- [Installation & Configuration Reference](../../../../Reference/installation-configuration-reference.md) — complete settings reference

---

*Document written: 2026-05-22*
