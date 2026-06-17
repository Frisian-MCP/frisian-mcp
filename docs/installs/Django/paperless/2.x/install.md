# Installing frisian-mcp with Paperless-ngx

**Audience:** Paperless-ngx administrators adding MCP gateway support  
**Platform:** Paperless-ngx · Django 4.2 · Python 3.12+

---

## Overview

frisian-mcp is a Django package that turns your existing Django REST Framework ViewSets into Model Context Protocol (MCP) tools with zero boilerplate. When installed in Paperless-ngx, every API endpoint the application exposes — documents, tags, correspondents, mail rules, workflows, and more — automatically becomes callable by any MCP-compatible AI client.

A default Paperless-ngx installation exposes 131 ViewSet actions across 20 ViewSets. frisian-mcp's dispatch-group system bundles those into 7 topic-level tools so agents see a manageable, navigable surface rather than all operations at once.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Paperless-ngx | 2.x |
| Python | 3.12 or newer |
| Django | 4.2 (bundled with Paperless-ngx 2.x) |
| Django REST Framework | 3.14+ (bundled with Paperless-ngx 2.x) |

No additional infrastructure is required for the basic install. OAuth support requires a shared cache backend (Redis) in multi-worker deployments.

---

## Step 1 — Install the Package

```bash
pip install frisian-mcp
```

For Docker-based deployments, add this to the Dockerfile or entrypoint script that runs before Paperless-ngx starts:

```bash
pip install frisian-mcp
```

---

## Step 2 — Add to INSTALLED_APPS

Paperless-ngx provides a dedicated environment variable for adding third-party apps without touching any source files:

```bash
PAPERLESS_APPS=frisian_mcp
```

Set this in your `paperless.conf` or environment alongside your existing Paperless-ngx environment variables. Paperless-ngx appends the listed apps to `INSTALLED_APPS` automatically at startup.

### Optional: OAuth 2.0 Support

To allow AI clients (Claude.ai, ChatGPT, Grok) to connect using their built-in OAuth flow, also include the OAuth contrib app:

```bash
PAPERLESS_APPS=frisian_mcp,frisian_mcp.contrib.oauth
```

---

## Step 3 — Configure Settings

For a basic install, the `PAPERLESS_APPS` env var is sufficient. For full configuration — dispatch groups, OAuth, authentication classes — create a settings override file and point Paperless-ngx at it.

Create a file named `paperless_frisian_mcp.py` (see the included [production settings template](paperless_frisian_mcp.py)) alongside your Paperless-ngx configuration, then set:

```bash
DJANGO_SETTINGS_MODULE=paperless_frisian_mcp
```

This file imports all of Paperless-ngx's base settings and adds only the frisian-mcp configuration on top — no Paperless-ngx source files are modified.

### Minimum configuration

```python
# paperless_frisian_mcp.py

from paperless.settings import *  # noqa: F401, F403

INSTALLED_APPS.append("frisian_mcp")  # noqa: F405

FRISIAN_MCP_PATH = "mcp"
FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"
```

### Recommended production configuration

See [paperless_frisian_mcp.py](paperless_frisian_mcp.py) for the full production settings template with OAuth, dispatch groups, and reverse proxy support.

---

## Step 4 — Run Migrations

frisian-mcp adds database tables for OAuth clients, tokens, and access tokens. Run migrations after updating `INSTALLED_APPS`:

```bash
python manage.py migrate
```

In a Docker deployment, this typically runs as part of the existing entrypoint script before the server starts.

---

## Step 5 — No URL Wiring Required

frisian-mcp does not modify any Paperless-ngx source files.

No changes to Paperless-ngx's `urls.py`, models, serializers, views, or middleware. The MCP endpoint is registered entirely from within the installed package via Django's `AppConfig.ready()` hook. Paperless-ngx has no knowledge of frisian-mcp beyond seeing it in `INSTALLED_APPS`.

This means frisian-mcp is upgrade-safe. When Paperless-ngx releases a new version, frisian-mcp re-discovers the updated ViewSet tree on first request. No migration of integration code required.

The gateway will be available at:

```
https://your-paperless.example.com/mcp/
```

> **How it works:** frisian-mcp inserts its URL pattern at position 0 of the root URL resolver when `AppConfig.ready()` fires. This is idempotent — subsequent process restarts do not create duplicate entries. If you prefer explicit control, you can add `path("mcp/", include("frisian_mcp.urls"))` to your URL configuration and the auto-registration logic will detect it and skip.

---

## Step 6 — Verify Startup

Start Paperless-ngx normally. On the first incoming request, frisian-mcp scans the URL tree and registers all discovered tools. Look for these lines in the server output:

```
[frisian-mcp] registered 131 tools at /mcp/
[frisian-mcp] 7 dispatch group(s) bundling 131 tools
```

If you see `registered 0 tools`, verify that `FRISIAN_MCP_AUTODISCOVER` is not set to `False` and that `frisian_mcp` appears in `INSTALLED_APPS` before the first request is served.

---

## Step 7 — Configure Dispatch Groups (Recommended)

Paperless-ngx exposes 131 ViewSet actions across 20 ViewSets. Dispatch groups bundle these into 7 topic-level tools, reducing the context an AI client loads on connection from ~33,000 tokens to ~2,000 tokens.

Add `FRISIAN_MCP_DISPATCH_GROUPS` to your settings. The keys are the tool names agents will call; the values are lists of DRF ViewSet basenames.

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    # Core document management
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
```

An agent calling `documents` with `action="help"` receives a structured listing of every resource and action within that group, enabling progressive discovery without context exhaustion.

> **Basename tip:** Dispatch group basenames must match DRF's ViewSet basename — always `Model._meta.object_name.lower()`. For example, Paperless-ngx's `Document` model has basename `document`. If a group registers with 0 members, frisian-mcp logs a warning with suggestions.

---

## Step 8 — Connect an MCP Client

### Using a Paperless-ngx API Token

Generate a token in the Paperless-ngx UI under **Settings → API Tokens**, then connect your MCP client:

```json
{
  "mcpServers": {
    "paperless": {
      "type": "http",
      "url": "https://your-paperless.example.com/mcp/",
      "headers": {
        "Authorization": "Bearer <your-paperless-token>"
      }
    }
  }
}
```

### Using a frisian-mcp Static API Key

For internal agents or scripts, configure a static key in your settings:

```python
FRISIAN_MCP_API_KEYS = {
    "my-agent-key": "read_write",
    "readonly-agent": "read",
}
```

Connect with:

```json
{
  "mcpServers": {
    "paperless": {
      "type": "http",
      "url": "https://your-paperless.example.com/mcp/",
      "headers": {
        "Authorization": "Bearer my-agent-key"
      }
    }
  }
}
```

### Using OAuth (Claude.ai, ChatGPT, Grok)

With `frisian_mcp.contrib.oauth` installed and `FRISIAN_MCP_OAUTH_ISSUER` set, AI clients that support OAuth can self-register and connect automatically. Point the client at:

```
https://your-paperless.example.com/mcp/
```

The client discovers the `/.well-known/oauth-authorization-server` metadata and initiates the PKCE authorization code flow. No manual token management is required.

> **Note:** During integration testing, an intermittent issue was identified in Anthropic's MCP client where the OAuth Bearer token is not forwarded on `tools/call` requests in some sessions. This is an Anthropic platform issue, not a frisian-mcp or Paperless-ngx issue. See the integration report for reproduction details and workaround guidance.

---

## Next Steps

- [Troubleshooting](../../../../troubleshooting/Django/paperless/2.x/troubleshooting.md) — common problems and solutions
- [Installation & Configuration Reference](../../../../Reference/installation-configuration-reference.md) — complete settings reference

---

*Document written: 2026-05-22*
