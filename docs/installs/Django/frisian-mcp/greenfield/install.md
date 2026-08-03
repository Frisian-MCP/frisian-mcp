# Greenfield Install — frisian-mcp

**Audience:** Django developers adding MCP gateway support to a new or existing project  
**Platform:** Django 4.2+ · Django REST Framework 3.14+ · Python 3.11+  
**Package version:** 1.1.x

---

## Overview

frisian-mcp turns your existing Django REST Framework ViewSets into Model Context Protocol (MCP) tools with zero boilerplate. Auto-discovery scans your URL resolver at startup and registers every ViewSet action it finds. AI clients — Claude Code, Claude.ai, ChatGPT, Cursor, and others — connect to a single HTTP endpoint and call those actions as tools.

This guide covers a greenfield Django + DRF project. For platform-specific guides (Nautobot, NetBox, Open edX, Paperless), see the platform install pages.

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.11 or newer |
| Django | 4.2 LTS or 5.x |
| Django REST Framework | 3.14+ |

No additional infrastructure is required for the basic install. Token-based authentication uses Django's database. OAuth 2.0 support requires a shared cache backend (Redis recommended in multi-worker deployments).

---

## Step 1 — Install the Package

```bash
pip install frisian-mcp
```

For projects using `requirements.txt` or `pyproject.toml`:

```text
frisian-mcp>=1.1
```

---

## Step 2 — Add to INSTALLED_APPS

Add `frisian_mcp` to your Django settings:

```python
# settings.py

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "frisian_mcp",
    # ... your apps
]
```

### Optional: Bearer token authentication

To issue Bearer tokens from the Django admin and use them to authenticate MCP clients (Claude Code, Cursor, Windsurf):

```python
INSTALLED_APPS = [
    ...
    "frisian_mcp",
    "frisian_mcp.contrib.tokens",
]
```

### Optional: OAuth 2.0

To allow AI clients with built-in OAuth flows (Claude.ai, ChatGPT, Grok) to connect without a Bearer token:

```python
INSTALLED_APPS = [
    ...
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
]
```

### Optional: Agent connection tracking

To track per-agent session history and restrict individual agents to a subset of tools:

```python
INSTALLED_APPS = [
    ...
    "frisian_mcp",
    "frisian_mcp.contrib.agents",
]
```

---

## Step 3 — Wire the URL

Add the MCP endpoint to your root URLconf. Use `re_path` — not `path` — to avoid a 308 redirect that MCP clients do not follow:

```python
# urls.py

from django.urls import include, re_path

urlpatterns = [
    # ... existing patterns
    re_path(r"^mcp/?", include("frisian_mcp.urls")),
]
```

This mounts the gateway at `/mcp`. Clients connect to `https://your-domain.example/mcp`.

> **Why `re_path`?** MCP clients like Claude.ai and Cursor may strip the trailing slash from the server URL. Django's `APPEND_SLASH` would normally issue a 301 redirect from `/mcp` → `/mcp/`, which those clients don't follow. The `re_path` pattern with an optional trailing slash (`/?`) matches both forms directly, and the package also auto-installs `McpTrailingSlashMiddleware`, which suppresses the `APPEND_SLASH` redirect on the gateway path — so both `/mcp` and `/mcp/` reach the endpoint with no redirect either way.

---

## Step 4 — Run Migrations

frisian-mcp ships database models when the contrib apps are installed:

```bash
python manage.py migrate
```

If you are only using the base package without any contrib apps, this step is a no-op.

---

## Step 5 — Minimal Settings

No settings are required to get auto-discovery running. The defaults are:

| Setting | Default | Effect |
|---------|---------|--------|
| `FRISIAN_MCP_ENABLED` | `True` | Enable/disable the gateway |
| `FRISIAN_MCP_AUTODISCOVER` | `True` | Auto-register ViewSet actions on startup |
| `FRISIAN_MCP_UNAUTHENTICATED_TIER` | `"read"` | Permission tier for unauthenticated requests. Cannot disable anonymous access — see below. |
| `FRISIAN_MCP_SERVER_NAME` | `"frisian-mcp"` | Server name in the `initialize` handshake |

A common minimum configuration:

```python
# settings.py

# Expose read-tier tools to unauthenticated callers.
FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"

# Optional: name your server
FRISIAN_MCP_SERVER_NAME = "my-app-mcp"
```

> ⚠️ `FRISIAN_MCP_UNAUTHENTICATED_TIER` can only *raise* the anonymous tier, never lower it below `read`. Setting it to `"none"` or `None` does **not** require authentication — unrecognised tiers rank equal to `read`, so anonymous callers keep the full read surface. To require authentication for every tool, add a gateway permission class:
>
> ```python
> FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]
> ```

For a full settings reference, see `features/configuration.md`.

---

## Step 6 — Verify

Start the development server and confirm the gateway responds:

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool
```

A successful response looks like:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "users.list",
        "description": "...",
        "inputSchema": { ... }
      }
    ]
  }
}
```

If `tools` is an empty array, auto-discovery found no ViewSet actions. Check that:

- Your ViewSets are registered in the URL resolver (not just defined)
- `FRISIAN_MCP_AUTODISCOVER` is not set to `False`
- No `@mcp_ignore` decorator was applied to all ViewSets

---

## Step 7 — Connect an AI Client

**Claude Code** — add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "my-app": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Or via CLI:

```bash
claude mcp add my-app \
  --transport http \
  --header "Authorization: Bearer <token>" \
  http://localhost:8000/mcp
```

See [connect-agent](../../../../v1.1/Guide/connect-agent.md) for Claude.ai, ChatGPT, and Grok OAuth connection steps.

---

## Auto-Discovery Behaviour

On startup, `AppConfig.ready()` scans the Django URL resolver for DRF ViewSet actions and registers each as an MCP tool named `{resource}.{action}` — for example, `users.list`, `orders.create`, `products.retrieve`.

The following are excluded automatically:

- ViewSets decorated with `@mcp_ignore`
- Individual actions decorated with `@mcp_ignore`
- Actions shadowed by a registered `@mcp_dispatcher` (the dispatcher replaces the flat tool list for that resource)

For large APIs (hundreds of ViewSet actions), use dispatchers to keep `tools/list` manageable. See `features/dispatcher.md`.

---

## Next Steps

| Feature | Guide |
|---------|-------|
| Manual tool registration | `features/mcp-tool.md` |
| Dispatcher pattern (large APIs) | `features/dispatcher.md` |
| Large-response negotiation | `features/mcp-heavy.md` |
| Write-path response filtering | `features/write-path.md` |
| Resources, `@mcp_ignore`, permission tiers, all settings | `features/configuration.md` |

Cross-references to the design rationale behind these features:

- [dispatcher-pattern](../../../../v1.1/Guide/dispatcher-pattern.md)
- [the-token-problem](../../../../v1.1/Guide/the-token-problem.md)
- [read-response-filtering](../../../../v1.1/Guide/read-response-filtering.md)
- [write-path-response-filtering](../../../../v1.1/Guide/write-path-response-filtering.md)
