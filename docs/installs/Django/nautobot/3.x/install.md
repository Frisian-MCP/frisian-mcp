# Installing frisian-mcp with Nautobot 3.x

**Audience:** Nautobot administrators adding MCP gateway support  
**Platform:** Nautobot 3.x · Django 5.x · Python 3.11+

---

## Overview

frisian-mcp is a Django package that turns your existing Django REST Framework ViewSets into Model Context Protocol (MCP) tools with zero boilerplate. When installed in Nautobot, every API endpoint Nautobot exposes — devices, interfaces, IP addresses, circuits, and more — automatically becomes callable by any MCP-compatible AI client.

A default Nautobot 3.x installation exposes roughly 1,900 ViewSet actions. frisian-mcp's dispatch-group system bundles those into a small set of topic-level tools (e.g. `dcim`, `ipam`, `circuits`) so agents see a manageable list rather than thousands of individual operations.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Nautobot | 3.x |
| Python | 3.11 or newer |
| Django | 5.x (bundled with Nautobot 3.x) |
| Django REST Framework | 3.14+ (bundled with Nautobot 3.x) |

No additional infrastructure is required for the basic install. OAuth support requires a shared cache backend (Redis recommended) in multi-worker deployments.

---

## Step 1 — Install the Package

```bash
pip install frisian-mcp
```

For Docker-based deployments, add this to the Dockerfile or entrypoint script that runs before Nautobot starts:

```bash
pip install frisian-mcp
```

---

## Step 2 — Add to INSTALLED_APPS

In your `nautobot_config.py`, append `frisian_mcp` to `INSTALLED_APPS`. Nautobot populates `INSTALLED_APPS` from its core settings; use `.append()` rather than redefining the list:

```python
# nautobot_config.py

INSTALLED_APPS.append("frisian_mcp")
```

### Optional: OAuth 2.0 Support

To allow AI clients (Claude.ai, ChatGPT, Grok) to connect using their built-in OAuth flow, also install the OAuth contrib app:

```python
INSTALLED_APPS.append("frisian_mcp")
INSTALLED_APPS.append("frisian_mcp.contrib.oauth")
```

---

## Step 3 — Configure Settings

Add the following block to `nautobot_config.py`. All settings are optional except `FRISIAN_MCP_PATH`, which should be set to `"api/mcp"` to match Nautobot's REST API path convention.

### Minimum configuration

The minimal configuration mounts frisian-mcp at `/api/mcp` and requires
authentication for every request — the default secure posture.  Add an MCP
token via the Django admin (Plugins → frisian-mcp → MCP Tokens) once the
package is installed.

```python
# nautobot_config.py

INSTALLED_APPS.append("frisian_mcp")
INSTALLED_APPS.append("frisian_mcp.contrib.tokens")

FRISIAN_MCP_PATH = "api/mcp"

FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
]
```

For an anonymous-readable surface (e.g. a public read-only demo), set
`FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"` and drop the `IsAuthenticated`
permission class.  Note this combination is **not** what most production
deployments want — the validated hardened posture in the next section is.

### Recommended production configuration

This is the **validated hardened posture** used in production deployments.
Hard authentication at the gateway, closed OAuth dynamic-client-registration,
hidden discovery metadata, and per-user permission-aware tool surfacing.

```python
# nautobot_config.py

INSTALLED_APPS.append("frisian_mcp")
INSTALLED_APPS.append("frisian_mcp.contrib.oauth")
INSTALLED_APPS.append("frisian_mcp.contrib.tokens")

FRISIAN_MCP_PATH = "api/mcp"

# Hard auth at the gateway.  Anonymous = 401.
FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# Authentication chain.  Order is no longer load-bearing as of frisian-mcp
# 1.0.11 — both classes return None on lookup-miss instead of raising
# AuthenticationFailed, so either order works.  FrisianMcpToken first is
# conventional on Nautobot for static-token connectors (Claude Code, Codex,
# Gemini CLI).
#
# Nautobot's NTC TokenAuthentication MUST NOT appear in this list — it eats
# any Bearer header it sees and rejects frisian-mcp tokens with
# AuthenticationFailed.
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]

# Public origin — LB-terminated URL of your Nautobot instance.  Do NOT
# include a port if a reverse proxy terminates TLS on 443.
FRISIAN_MCP_OAUTH_ISSUER = "https://your-nautobot.example.com"

# Trusted reverse-proxy hops in front of Nautobot (1 for a single nginx,
# 2 for nginx behind a cloud load balancer).
FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1

# Dedicated HMAC key — decouples frisian-mcp token / client-secret digests
# from Nautobot's SECRET_KEY so rotating one does not invalidate the other.
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
FRISIAN_MCP_HMAC_KEY = "replace-with-generated-secret"

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
# their configured static Bearer instead of bouncing with "Incompatible auth
# server: does not support dynamic client registration."  Pre-registered
# OAuth clients (e.g. the Claude.ai connector configured in admin) continue
# to work because they were given hard-coded endpoint URLs.
FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False

# Permission-aware discovery: rebuild dispatcher action enums per-request
# so each agent's tools/list reflects what THAT principal is allowed to
# call.  A read-tier token sees only list/retrieve; write actions are
# hidden from discovery, not just blocked at execution.
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
FRISIAN_MCP_PERMISSION_ADAPTER = (
    "frisian_mcp.contrib.permissions.exempt_view_adapter.ExemptViewPermissionAdapter"
)
```

A full reference config file (drop-in, with the same hardened posture and
all dispatcher groups for Nautobot 3.x core) is provided at
[`development/nautobot_config.py`](development/nautobot_config.py).

### CORS for AI clients

If AI clients connect from browser-based sessions, add their origins to your CORS allow-list:

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

frisian-mcp adds database tables for OAuth clients, tokens, and access tokens. Run migrations after updating `INSTALLED_APPS`:

```bash
nautobot-server migrate
```

---

## Step 5 — No URL Wiring Required

frisian-mcp does not modify any Nautobot source files.

No changes to Nautobot's `urls.py`, models, serializers, views, or middleware.  The MCP endpoint is registered entirely from within the installed package via Django's `AppConfig.ready()` — the same mechanism Nautobot itself uses for plugin registration. Nautobot has no knowledge of frisian-mcp beyond seeing it in `INSTALLED_APPS`.

This means frisian-mcp is upgrade-safe. When Nautobot releases a new version, frisian-mcp re-discovers the updated ViewSet tree on first request. No migration of integration code required.

frisian-mcp auto-registers its endpoint during Django's app startup (`AppConfig.ready()`). You do **not** need to modify Nautobot's `urls.py`. The gateway will be available at:

```text
https://your-nautobot.example.com/api/mcp/
```

> **How it works:** frisian-mcp inserts its URL pattern at position 0 of the root URL resolver when `ready()` fires. This is idempotent — subsequent process restarts do not create duplicate entries. If you prefer explicit control, you can add `path("api/mcp/", include("frisian_mcp.urls"))` to your URL configuration and the auto-registration logic will detect it and skip.

---

## Step 6 — Verify Startup

Start Nautobot normally. On the first incoming request, frisian-mcp scans the URL tree and registers all discovered tools. Look for these lines in the server output:

```text
[frisian-mcp] registered 1967 tools at /api/mcp/
[frisian-mcp] 13 dispatch group(s) bundling 1967 tools
```

The tool count reflects your installed Nautobot version and any plugins. The number after the bracket reflects dispatch group compression (see Step 7).

If you see `registered 0 tools`, verify that `FRISIAN_MCP_AUTODISCOVER` is not set to `False` and that the `frisian_mcp` app appears in `INSTALLED_APPS` before the first request is served.

---

## Step 7 — Configure Dispatch Groups (Recommended)

Nautobot exposes nearly 2,000 ViewSet actions. Without grouping, an MCP client receives all 2,000 tool definitions on connect — roughly 490,000 tokens of context. Dispatch groups compress this into one tool per Nautobot app boundary, reducing the initial context to approximately 3,000 tokens.

Add `FRISIAN_MCP_DISPATCH_GROUPS` to `nautobot_config.py`. The key is the group tool name the agent will call; the value is a list of DRF ViewSet basenames (derived from `Model._meta.object_name.lower()`, not URL slugs).

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    "dcim": [
        "device", "rack", "rackgroup", "rackreservation",
        "interface", "interfacetemplate",
        "cable", "location", "locationtype",
        "manufacturer", "devicetype",
        "platform", "inventoryitem",
        "consoleport", "powerport", "powerfeed", "powerpanel",
        "module", "modulebay", "moduletype",
        "virtualchassis",
        "softwareimagefile", "softwareversion",
    ],
    "ipam": [
        "ipaddress", "ipaddresstointerface",
        "prefix", "vlan", "vlangroup",
        "vrf", "routetarget", "iprange", "namespace",
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
    "extras": [
        "tag", "configcontext", "customfield", "customlink",
        "relationship", "dynamicgroup",
        "job", "jobresult", "scheduledjob",
        "webhook", "gitrepository",
        "role", "status", "secret", "secretsgroup",
        "note", "objectchange",
    ],
}
```

Extend or trim the list based on the Nautobot plugins you have installed. After restarting, the startup log will confirm the group count:

```text
[frisian-mcp] 6 dispatch group(s) bundling 847 tools
```

An agent calling `dcim` with `action="help"` receives a structured listing of every resource and action within that group, enabling progressive discovery without context exhaustion.

> **Basename tip:** Dispatch group basenames must match DRF's ViewSet basename — always `Model._meta.object_name.lower()`. For example, Nautobot's `IPAddress` model has basename `ipaddress`, not `ip-address` or `ip_address`. If a group registers with 0 members, frisian-mcp logs a warning with suggestions.

---

## Step 8 — Connect an MCP Client

The MCP gateway always expects the **`Bearer`** auth scheme.  Nautobot's own
API uses `Authorization: Token <key>`, which is NOT recognised by any of
the frisian-mcp authenticators — a Nautobot API token is therefore not
usable at the MCP endpoint in the recommended configuration, because that
chain does not include Nautobot's `TokenAuthentication` class.  Use a
frisian-mcp MCP token or an OAuth-issued Bearer instead.

### Using a frisian-mcp MCP Token (recommended for static-token clients)

Generate a token in the Django admin (**Plugins → frisian-mcp → MCP Tokens
→ Add**) or via `nautobot-server nbshell`:

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
    "nautobot": {
      "type": "http",
      "url": "https://your-nautobot.example.com/api/mcp/",
      "headers": {
        "Authorization": "Bearer <plaintext-token-from-above>"
      }
    }
  }
}
```

### Using a frisian-mcp Settings-Backed Static API Key

For internal agents or scripts where adding a DB row is overkill, configure
HMAC-hashed static keys in `nautobot_config.py`:

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
`nautobot_config.py` does not directly expose usable credentials.

### Using OAuth (Claude.ai, ChatGPT, Grok)

With `frisian_mcp.contrib.oauth` installed, AI clients connect via the
OAuth 2.1 PKCE authorization-code flow.  In the hardened production posture
(closed dynamic-client-registration), the operator pre-registers each
client via the Django admin:

1. Sign in to the Nautobot admin and navigate to **Plugins → frisian-mcp
   → OAuth Clients → Add**.
2. Pick a permission tier (`read`, `read_write`, or `admin`), attach a
   default Django user via the `user` field (this is the principal the
   client's MCP requests run as), and submit.  A `client_id` and one-time
   `client_secret` are displayed on the success page.
3. Paste those values into the AI client's connector settings (Claude.ai:
   **Connect MCP Server → Advanced**; ChatGPT and Grok have equivalent
   forms).  Point the client URL at:

   ```text
   https://your-nautobot.example.com/api/mcp/
   ```

4. The client completes the PKCE flow against `/oauth/authorize/`, you
   approve the consent screen (because `FRISIAN_MCP_OAUTH_AUTO_APPROVE =
   False`), and the client receives an access token.

The `/.well-known/oauth-authorization-server` metadata is hidden under the
hardened posture (`FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = False`) — clients
that try discovery-based onboarding will see a JSON 404 and either fall
back to the configured Bearer (static-token connectors) or require manual
client-credential entry as described above.

---

## Next Steps

- [Nginx Configuration](nginx.md) — proxy settings for production deployments behind a reverse proxy
- [Troubleshooting](../../../../troubleshooting/Django/nautobot/3.x/troubleshooting.md) — common problems and solutions from real deployments
- [Installation & Configuration Reference](../../../../Reference/installation-configuration-reference.md) — complete settings reference

---

*Document written: 2026-05-21*
