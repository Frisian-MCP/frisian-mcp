# Installing frisian-mcp with Open edX (LMS)

**Audience:** Open edX platform engineers adding MCP gateway support  
**Platform:** Open edX (Sumac / Redwood) · Django 4.2 · Python 3.11+

> **Integration status:** The plugin scaffold described here was built and validated through migrations and server startup. Full end-to-end OAuth and tool call testing was not completed. Treat this as a working starting point, not a fully validated production guide.

---

## Overview

frisian-mcp is a Django package that turns your existing Django REST Framework ViewSets into Model Context Protocol (MCP) tools with zero boilerplate. When installed in the Open edX LMS, the platform's user, enrollment, assessment, organization, and LTI surfaces automatically become callable by any MCP-compatible AI client.

A default Open edX LMS installation exposes 78 auto-discoverable ViewSet actions. frisian-mcp's dispatch-group system bundles those into 9 topic-level tools — users, enrollments, LTI, organizations, assessment, auth, retirement, data, and xqueue.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Open edX | Sumac / Redwood (Django 4.2) |
| Python | 3.11 or newer |
| Django REST Framework | 3.14+ (bundled with Open edX) |
| Redis | Required — OAuth PKCE stores authorization codes in the default cache |

> **Cache requirement:** Open edX's test/devstack settings configure `DummyCache` for all backends. frisian-mcp's OAuth PKCE flow stores authorization codes in the default cache, so a real backend (Redis) is required in any environment where OAuth will be used. See Step 3.

---

## Open edX Plugin System — Why a Plugin App Is Required

Open edX uses `edx_django_utils.plugins` for URL injection rather than Django's standard URL routing. Third-party apps that need to add URL patterns must register them via a `PluginURLs.CONFIG` entry in their `AppConfig`. There is no equivalent to the `frisian_mcp.AppConfig.ready()` auto-injection that works in standard Django projects.

Additionally, `admin/login/` in the LMS is hard-wired to redirect to a React login page. A thin URL override is needed to expose Django admin's plain HTML login view (required for frisian-mcp's OAuth admin interface).

For these reasons, installing frisian-mcp into Open edX requires a small plugin app. A reference implementation is included in this repository at `openedx_frisian_mcp/`. It handles:

- Registering the MCP endpoint, OAuth, and well-known URLs via `PluginURLs`
- Providing a dev-only `ROOT_URLCONF` override that fixes the admin login redirect

No Open edX source files are modified.

---

## Step 1 — Install the Packages

```bash
pip install frisian-mcp
```

Then install the plugin app from this repository:

```bash
pip install -e ./openedx_frisian_mcp/
```

Or copy `openedx_frisian_mcp/` into your Open edX platform tree and add it to your requirements.

---

## Step 2 — Add to INSTALLED_APPS

In your LMS settings file (e.g. `lms/envs/private.py` or `lms/envs/production.py`), add the required apps:

```python
INSTALLED_APPS = list(INSTALLED_APPS) + [
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
    "openedx_frisian_mcp",
]
```

Use `list(INSTALLED_APPS) + [...]` rather than `.append()` — Open edX's `common.py` calls `get_plugin_apps()` which may return a tuple; the `+` operator ensures a mutable list.

The `openedx_frisian_mcp` app's `AppConfig` registers the MCP URL patterns automatically via the `PluginURLs` mechanism when the LMS starts.

---

## Step 3 — Ensure a Real Cache Backend

OAuth PKCE stores authorization codes in Django's default cache. Verify your production settings use Redis (Open edX ships with Redis, so this is typically already configured):

```python
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://localhost:6379/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    },
}
```

If you see `OAuth authorization code not found` errors, the cache backend is the first thing to verify.

---

## Step 4 — Configure Settings

All `FRISIAN_MCP_*` settings go in your LMS settings file. See `lms/envs/mcp_prod.py` in this repository for the full production template.

### Minimum configuration

```python
# lms/envs/private.py (or production.py override)

INSTALLED_APPS = list(INSTALLED_APPS) + [
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
    "openedx_frisian_mcp",
]

FRISIAN_MCP_PATH = "mcp"
FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"
```

### Recommended production configuration

See [`lms/envs/mcp_prod.py`](lms/envs/mcp_prod.py) for the full settings template including OAuth, dispatch groups, authentication chain, and reverse proxy support.

### Authentication class order

**ALWAYS list `FrisianMcpTokenAuthentication` BEFORE `OAuthTokenAuthentication` when both are present.** As of frisian-mcp 1.0.11 both classes return `None` on lookup-miss (so either order works for correctness), but the FIRST authenticator in the chain emits the WWW-Authenticate challenge on 401 responses. Tokens-first emits a bare `Bearer` challenge, which static-token MCP clients (Claude Code, Codex, Gemini CLI) accept and fall back to their configured Bearer cleanly. OAuth-first emits `Bearer realm="...", resource_metadata="..."`, which nudges discovery-first clients into the OAuth cascade — fine if every client is an OAuth client, but a footgun the moment you add a static-token coding agent.

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

> **Historical note.** Earlier versions of frisian-mcp (pre-1.0.11) raised `AuthenticationFailed` on lookup-miss in both classes, which DID make ordering load-bearing for correctness in the *opposite* direction — token-first would reject OAuth tokens. Docs from that era recommended OAuth-first. The 1.0.11 chain fix removed that constraint; the new convention is tokens-first for the WWW-Authenticate-shape reason above.

---

## Step 5 — Run Migrations

frisian-mcp adds database tables for OAuth clients, tokens, and access tokens:

```bash
python manage.py lms migrate
```

---

## Step 6 — No Open edX Source Files Modified

frisian-mcp does not modify any Open edX source files.

The `openedx_frisian_mcp` plugin app wires URLs and settings entirely through Open edX's own extension points (`PluginURLs`, `AppConfig.ready()`). Open edX core code, models, serializers, views, and URL configurations are untouched.

The gateway will be available at:

```
https://your-lms.example.com/mcp/
```

---

## Step 7 — Verify Startup

Start the LMS normally. On the first incoming request, frisian-mcp scans the URL tree and registers all discovered tools:

```
[frisian-mcp] registered 78 tools at /mcp/
[frisian-mcp] 9 dispatch group(s) bundling 78 tools
```

If you see `registered 0 tools`, verify that `openedx_frisian_mcp` is in `INSTALLED_APPS` and that the app appears **after** `frisian_mcp` in the list.

---

## Step 8 — Configure Dispatch Groups (Recommended)

Open edX exposes 78 tools across user, enrollment, LTI, and operational surfaces. The dispatch group configuration below was derived from the full auto-discovered surface during integration testing:

```python
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
```

### Large response negotiation

Open edX ViewSets cannot be decorated with `@mcp_heavy` without modifying platform source files. Use `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` instead — responses larger than the threshold are cached and returned as a continuation token rather than inline JSON:

```python
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 8_000  # bytes
```

---

## Step 9 — Connect an MCP Client

### Using a frisian-mcp Static API Key

```python
FRISIAN_MCP_API_KEYS = {
    "my-agent-key": "read_write",
    "readonly-agent": "read",
}
```

```json
{
  "mcpServers": {
    "openedx": {
      "type": "http",
      "url": "https://your-lms.example.com/mcp/",
      "headers": {
        "Authorization": "Bearer my-agent-key"
      }
    }
  }
}
```

### Using OAuth (Claude.ai, ChatGPT, Grok)

With `FRISIAN_MCP_OAUTH_ISSUER` set and `frisian_mcp.contrib.oauth` installed, AI clients can self-register and connect via PKCE. Point the client at:

```
https://your-lms.example.com/mcp/
```

The client discovers `/.well-known/oauth-authorization-server` and initiates the authorization code flow automatically.

---

## Plugin App Reference

The `openedx_frisian_mcp/` plugin app included in this repository contains:

| File | Purpose |
|---|---|
| `apps.py` | `AppConfig` with `PluginURLs` registration — required in production |
| `urls.py` | Explicit MCP, OAuth, and well-known URL patterns — required in production |
| `dev_auth.py` | Dev-only fallback: authenticates as service user when no Bearer token present. **Never use in production.** |
| `mcp_dev_urls.py` | Dev-only: overrides `admin/login/` to serve plain HTML instead of React redirect. Not needed in production. |
| `mcp_request_log_middleware.py` | Dev-only: logs Authorization header presence on MCP requests. Used to capture evidence of the Anthropic Bearer-token omission bug. Not needed in production. |

---

## Next Steps

- [Troubleshooting](../../../../troubleshooting/Django/openedx/sumac/troubleshooting.md) — common problems and solutions
- [Installation & Configuration Reference](../../../../Reference/installation-configuration-reference.md) — complete settings reference

---

*Document written: 2026-05-22*
