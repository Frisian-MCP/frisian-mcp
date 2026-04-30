# friese-mcp

**The Django MCP gateway that discovers your API automatically.**

friese-mcp turns your existing Django REST Framework ViewSets into [Model Context Protocol](https://spec.modelcontextprotocol.io/) tools with zero boilerplate. Add the package, include one URL, and every ViewSet action becomes a callable MCP tool — no manual schema writing, no tool registration code, no wiring.

**Version:** 0.1.0 | **License:** Apache 2.0 | **Python:** 3.11+ | **Django:** 5.x

```bash
pip install friese-mcp
```

---

## Why friese-mcp

Most Django MCP integrations require you to write a tool definition for every endpoint you want to expose. With 20 ViewSets and 5 actions each, that's 100 tool definitions to write, keep in sync with your serializers, and update every time a field changes.

friese-mcp takes a different approach: **auto-discovery**.

At startup, friese-mcp walks your URL patterns, finds every DRF ViewSet, and registers each action as an MCP tool — name, description, and input schema derived from your serializer automatically. The tool manifest stays in sync with your API without any extra work.

### The dispatcher pattern: the other thing worth knowing

Auto-discovery is the default path. For teams building purpose-built agent tools — multi-action families that share context — friese-mcp ships the **`@mcp_dispatcher`** pattern.

One class. One MCP tool name. Many actions routed internally.

```python
from friese_mcp import mcp_dispatcher, mcp_action

@mcp_dispatcher(name="tasks", description="Manage project tasks.")
class TasksDispatcher:

    @mcp_action(name="create", description="Create a task.")
    def create(self, request, params):
        task = Task.objects.create(title=params["title"])
        return {"id": task.pk}

    @mcp_action(name="list", description="List tasks by status.")
    def list(self, request, params):
        return {"tasks": list(Task.objects.values("id", "title", "status"))}
```

- One tool in `tools/list` instead of many
- Built-in help mode: call with `action="help"` for a structured action listing
- Per-action JSON Schema validation before the method runs
- Close-match suggestions on unknown action names

This is the pattern for agent-facing APIs where tool count matters and progressive disclosure beats a flat list of 150 tools.

---

## At a glance

| Feature | Details |
|---|---|
| **Auto-discovery** | Walks URL patterns at startup; registers every ViewSet action as an MCP tool |
| **Zero boilerplate** | Name, description, and input schema derived from DRF serializers automatically |
| **`@mcp_dispatcher`** | One tool → many actions; built-in help mode; per-action validation |
| **`@mcp_tool`** | Explicit single-function tool registration for custom logic |
| **`@mcp_resource`** | Expose server-side content via `resources/list` / `resources/read` |
| **Filter introspection** | `SearchFilter`, `OrderingFilter`, `DjangoFilterBackend` → schema properties on `list` |
| **Allowlist / denylist** | `FRIESE_MCP_TOOL_ALLOWLIST` / `FRIESE_MCP_TOOL_DENYLIST` for surgical surface control |
| **OAuth 2.0** | `contrib.oauth` — authorization code (PKCE) + client credentials; permission-tier tool filtering |
| **Static tokens** | `contrib.tokens` — HMAC-hashed Bearer tokens for internal agents |
| **Per-agent scoping** | `contrib.agents` — per-credential tool allowlists for multi-agent deployments |
| **Tool middleware** | `FRIESE_MCP_TOOL_MIDDLEWARE` — audit logging, rate limiting, heartbeats |
| **Rate limiting** | `RateLimitMiddleware` — built-in sliding-window, no Redis required |
| **Pluggable backends** | Custom discovery and invocation backends via dotted-path settings |
| **SSE support** | `Accept: text/event-stream` wraps any response in a single SSE event |
| **Cursor pagination** | `FRIESE_MCP_TOOLS_PAGE_SIZE` for large tool manifests |
| **MCP `2025-03-26`** | Streamable HTTP; `ping`, `initialize`, `tools/list`, `tools/call`, `resources/list` |

---

## Quickstart

Install and add to `INSTALLED_APPS`:

```bash
pip install friese-mcp
```

```python
# settings.py
INSTALLED_APPS = [
    ...
    "friese_mcp",
]
```

Include the gateway URL:

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    ...
    path("mcp/", include("friese_mcp.urls")),
]
```

That's it. With auto-discovery enabled (the default), every DRF ViewSet in your URL tree is now an MCP tool.

```python
# myapp/views.py — nothing changes here
class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
```

After startup, the gateway exposes:

| Tool | Description |
|---|---|
| `users.list` | List User objects |
| `users.retrieve` | Retrieve a User object by ID |
| `users.create` | Create a new User object |
| `users.update` | Replace a User object by ID |
| `users.partial_update` | Partially update a User object by ID |
| `users.destroy` | Delete a User object by ID |

Connect an MCP client in one command:

```bash
python manage.py mcp_config --client claude-code --token mytoken123
```

```json
{
  "mcpServers": {
    "friese-mcp": {
      "type": "http",
      "url": "http://localhost:8000/mcp/",
      "headers": { "Authorization": "Bearer mytoken123" }
    }
  }
}
```

---

## Architecture overview

```
MCP Client (Claude, Cursor, GPT, …)
       │  JSON-RPC 2.0 over HTTP POST
       ▼
┌──────────────────────────────────────────────────┐
│  McpEndpointView  (DRF APIView)                   │
│  ├─ Authentication  (FRIESE_MCP_AUTHENTICATION_CLASSES) │
│  ├─ Permissions     (FRIESE_MCP_PERMISSION_CLASSES)     │
│  └─ Method dispatch                              │
│       ├─ initialize / initialized / ping / help  │
│       ├─ tools/list  ──────────────── ToolRegistry │
│       ├─ tools/call  ── ToolMiddleware ── Registry │
│       ├─ resources/list ───────── ResourceRegistry │
│       └─ resources/read ───────── ResourceRegistry │
└──────────────────────────────────────────────────┘
       │
┌──────────────────┐   ┌─────────────────────────┐
│  ToolRegistry    │   │  Auto-discovery          │
│  (module-level   │◄──│  (DRFSyncDiscovery)      │
│   singleton)     │   │  Walks URL patterns at   │
│                  │   │  AppConfig.ready()       │
└──────────────────┘   └─────────────────────────┘
       │
┌──────────────────────────────────────────────────┐
│  InvocationBackend  (SyncInvocation by default)  │
│  Builds synthetic DRF Request → calls ViewSet    │
│  action → returns ToolResult                     │
└──────────────────────────────────────────────────┘
```

**Key design points:**

- **Separation of discovery and invocation.** Two pluggable backends. Override either independently: use a custom discovery backend for Nautobot's app registry; use a custom invocation backend for Celery-delegated or async execution.
- **Registry is the source of truth.** `@mcp_tool`, `@mcp_dispatcher`, and auto-discovery all write to the same `tool_registry` singleton. `tools/list` reads from it directly.
- **Tool errors are `isError: true`, not JSON-RPC errors.** Permission denials, validation errors, and handler exceptions return `isError: true` inside a normal HTTP 200 response — the JSON-RPC session stays alive for the agent to inspect and retry.
- **Two enforcement points.** Gateway-level permissions gate the entire `/mcp/` surface (`FRIESE_MCP_PERMISSION_CLASSES`). Tool-level permissions gate individual `tools/call` invocations via `ToolRegistry.dispatch()`.

---

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Connecting MCP clients](#connecting-mcp-clients)
- [Quickstart](#quickstart)
- [Settings reference](#settings-reference)
- [Reverse proxy configuration](#reverse-proxy-configuration)
- [Authentication and permissions](#authentication-and-permissions)
- [Built-in authentication](#built-in-authentication)
  - [contrib.tokens — static Bearer tokens](#contribtokens--static-bearer-tokens)
  - [contrib.oauth — OAuth 2.0](#contriboauth--oauth-20)
  - [contrib.agents — per-agent tool allowlists](#contribagents--per-agent-tool-allowlists)
- [Auto-discovery](#auto-discovery)
- [Decorators](#decorators)
  - [@mcp_tool](#mcp_tool)
  - [@mcp_ignore](#mcp_ignore)
  - [@mcp_dispatcher and @mcp_action](#mcp_dispatcher-and-mcp_action)
  - [@mcp_resource](#mcp_resource)
- [Tool call middleware](#tool-call-middleware)
  - [RateLimitMiddleware](#ratelimitmiddleware)
- [ToolRegistry API](#toolregistry-api)
- [MCP gateway endpoint](#mcp-gateway-endpoint)
  - [SSE support](#sse-support)
  - [Session ID header](#session-id-header)
  - [tools/list cursor pagination](#toolslist-cursor-pagination)
- [Pluggable backend architecture](#pluggable-backend-architecture)
- [Known limitations and design decisions](#known-limitations-and-design-decisions)
- [Troubleshooting](#troubleshooting)

---

## Requirements

- Python 3.11+
- Django 5.x
- Django REST Framework 3.14+

## Installation

```
pip install friese-mcp
```

Add `"friese_mcp"` to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "friese_mcp",
]
```

Include the gateway URL in your root URLconf:

```python
from django.urls import include, path

urlpatterns = [
    ...
    path("mcp/", include("friese_mcp.urls")),
]
```

The gateway is now reachable at `POST /mcp/`.

---

## Connecting MCP clients

Run the built-in management command to generate a ready-to-paste `mcpServers` config block:

```bash
python manage.py mcp_config [--client {claude-code,cursor,claude-desktop,generic}] [--url URL] [--token VALUE] [--name KEY]
```

**URL resolution order:** `--url` flag → `FRIESE_MCP_BASE_URL` setting → `http://localhost:8000/mcp/`

**Server name resolution:** `--name` flag → `FRIESE_MCP_SERVER_NAME` setting → `"friese-mcp"`

### Output schema by client

| `--client` | Output shape |
|---|---|
| `claude-code` | `{"type": "http", "url": ..., ["headers": {...}]}` |
| `cursor` | `{"type": "http", "url": ..., ["headers": {...}]}` |
| `claude-desktop` | `{"url": ..., ["headers": {...}]}` |
| `generic` (default) | `{"url": ..., "transport": "http", ["headers": {...}]}` |

`headers` is only present when `--token` is supplied.

### Examples

**Generic (no auth):**
```bash
python manage.py mcp_config
```
```json
{
  "mcpServers": {
    "friese-mcp": {
      "url": "http://localhost:8000/mcp/",
      "transport": "http"
    }
  }
}
```

**Claude Code / Cursor with a Bearer token:**
```bash
python manage.py mcp_config --client claude-code --token mytoken123
```
```json
{
  "mcpServers": {
    "friese-mcp": {
      "type": "http",
      "url": "http://localhost:8000/mcp/",
      "headers": {
        "Authorization": "Bearer mytoken123"
      }
    }
  }
}
```

**Claude Desktop (production URL, custom server name):**
```bash
python manage.py mcp_config --client claude-desktop --url https://api.example.com/mcp/ --name my-product
```
```json
{
  "mcpServers": {
    "my-product": {
      "url": "https://api.example.com/mcp/"
    }
  }
}
```

When `--client claude-desktop` is used, the command also prints the config file path to stderr:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/claude/claude_desktop_config.json`

> **Security note:** `--token` embeds the Bearer value in the output JSON. Treat the command output as sensitive when a real token is used — do not commit it to version control.

**Multi-server setups:** Use `--name` to produce entries with distinct keys, then merge the `mcpServers` objects manually.

---

## Settings reference

All settings are read from Django's `settings` module at runtime. Every setting has a safe default so no configuration is required for a standard DRF project.

### `FRIESE_MCP_ENABLED`

**Type:** `bool` | **Default:** `True`

Master on/off switch. When `False`, the gateway endpoint returns HTTP 503 and auto-discovery is skipped entirely.

```python
FRIESE_MCP_ENABLED = False  # disable in staging
```

### `FRIESE_MCP_AUTODISCOVER`

**Type:** `bool` | **Default:** `True`

Controls whether ViewSet auto-discovery runs at `AppConfig.ready()`. Set to `False` when you want to register all tools manually via `@mcp_tool` and do not want the URL tree scanned.

```python
FRIESE_MCP_AUTODISCOVER = False
```

### `FRIESE_MCP_DISCOVERY_BACKENDS`

**Type:** `list[str]` (dotted Python import paths) | **Default:** absent (uses `FRIESE_MCP_DISCOVERY_BACKEND`, then `DRFSyncDiscovery`)

List of discovery backend classes to run at startup. Results are merged in order — later backends win on tool name clashes. Use this when you need to pull tools from multiple sources (e.g. standard DRF ViewSets plus a Nautobot app registry).

```python
FRIESE_MCP_DISCOVERY_BACKENDS = [
    "friese_mcp.backends.discovery.DRFSyncDiscovery",
    "myapp.backends.NautobotDiscovery",
]
```

> **Requires `FRIESE_MCP_AUTODISCOVER = True` (the default).** When `FRIESE_MCP_AUTODISCOVER` is `False`, startup discovery is skipped entirely and this setting is never read — no tools will be auto-discovered regardless of what backends are configured.

Each class must subclass `friese_mcp.backends.BaseDiscoveryBackend`.

### `FRIESE_MCP_DISCOVERY_BACKEND`

**Type:** `str` (dotted Python import path) | **Default:** `"friese_mcp.backends.discovery.DRFSyncDiscovery"`

Single discovery backend class. Use `FRIESE_MCP_DISCOVERY_BACKENDS` (plural) instead when you need multiple backends. This setting is ignored when `FRIESE_MCP_DISCOVERY_BACKENDS` is present.

```python
FRIESE_MCP_DISCOVERY_BACKEND = "myapp.backends.NautobotDiscovery"
```

> **Requires `FRIESE_MCP_AUTODISCOVER = True` (the default).** Setting `FRIESE_MCP_AUTODISCOVER = False` short-circuits startup before backends are consulted — this setting is silently ignored and no tools are auto-discovered.

The referenced class must subclass `friese_mcp.backends.BaseDiscoveryBackend`.

### `FRIESE_MCP_INVOCATION_BACKEND`

**Type:** `str` (dotted Python import path) | **Default:** `"friese_mcp.backends.invocation.SyncInvocation"`

The invocation backend class used to dispatch `tools/call` requests. Override for async execution, Celery delegation, or tenant-scoped contexts.

```python
FRIESE_MCP_INVOCATION_BACKEND = "myapp.backends.AsyncInvocation"
```

The referenced class must subclass `friese_mcp.backends.BaseInvocationBackend`.

### `FRIESE_MCP_AUTHENTICATION_CLASSES`

**Type:** `list[str | type]` | **Default:** DRF's `DEFAULT_AUTHENTICATION_CLASSES`

Authentication classes applied to every request reaching the MCP gateway endpoint. Each entry may be a dotted-path string or a class object. When absent, DRF's `DEFAULT_AUTHENTICATION_CLASSES` is used unchanged.

```python
FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "rest_framework_simplejwt.authentication.JWTAuthentication",
    "rest_framework.authentication.SessionAuthentication",
]
```

Use this to attach a token type (e.g. MCPToken, API key) specifically to the MCP surface without affecting the rest of your API.

### `FRIESE_MCP_PERMISSION_CLASSES`

**Type:** `list[str | type]` | **Default:** `[]` (no gateway-level permission check)

Permission classes applied to every request reaching the MCP gateway endpoint before any method handler or tool is invoked. Defaults to `[]` for backwards compatibility — host apps that already gate `/mcp/` at the infrastructure level are unaffected.

Each entry may be a dotted-path string or a class object.

```python
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

> **Note:** `FRIESE_MCP_PERMISSION_CLASSES` gates the entire MCP endpoint (all methods). Individual tools still enforce their own `permission_classes` via `ToolRegistry.dispatch()` regardless of this setting.

### `FRIESE_MCP_SERVER_NAME`

**Type:** `str` | **Default:** `"friese-mcp"`

The `serverInfo.name` field returned in the `initialize` handshake response. `serverInfo.version` is read automatically from the installed package metadata (`importlib.metadata`) and cannot be overridden via settings.

```python
FRIESE_MCP_SERVER_NAME = "my-product-mcp"
```

### `FRIESE_MCP_TOOL_ALLOWLIST`

**Type:** `list[str]` | **Default:** absent (all tools visible)

When present, only the tool names in this list are registered at startup. All other auto-discovered tools are dropped before reaching the registry. Names are exact matches (e.g. `"users.destroy"`).

```python
FRIESE_MCP_TOOL_ALLOWLIST = [
    "users.list",
    "users.retrieve",
    "workouts.create",
]
```

Use this to expose a minimal, stable tool surface for production AI agents without modifying your ViewSets.

### `FRIESE_MCP_TOOL_DENYLIST`

**Type:** `list[str]` | **Default:** absent (no tools suppressed)

Tool names in this list are dropped at startup. Applied after the allowlist, so denylisting an allowlisted name still removes it.

```python
FRIESE_MCP_TOOL_DENYLIST = [
    "users.destroy",
    "admin.delete_all",
]
```

### `FRIESE_MCP_NORMALIZE_INPUT_CASE`

**Type:** `bool` | **Default:** `False`

When `True`, incoming `tools/call` argument keys are normalised from camelCase to snake_case before dispatch. Useful when the calling agent (e.g. a GPT plugin) sends `userId` instead of `user_id`.

```python
FRIESE_MCP_NORMALIZE_INPUT_CASE = True
```

### `FRIESE_MCP_EXPOSE_ERRORS`

**Type:** `bool` | **Default:** `settings.DEBUG`

Controls whether the raw exception message is included in `isError: true` tool responses for unhandled exceptions.

| Value | Error text returned to caller |
|---|---|
| `True` (or absent with `DEBUG = True`) | `str(exc)` — full exception message |
| `False` (or absent with `DEBUG = False`) | `"Internal tool error"` — safe generic message |

```python
# Always expose errors (e.g. a fully internal deployment)
FRIESE_MCP_EXPOSE_ERRORS = True

# Always suppress errors (e.g. public-facing API)
FRIESE_MCP_EXPOSE_ERRORS = False
```

When the setting is absent, it inherits `settings.DEBUG`: errors are verbose in development and suppressed in production by default. Full error details are always logged server-side via `logger.exception` regardless of this setting.

### `FRIESE_MCP_BASE_URL`

**Type:** `str` | **Default:** absent

Base URL used by `python manage.py mcp_config` when generating the `mcpServers` JSON block. When absent, the command falls back to `http://localhost:8000/mcp/`. This setting does not affect the gateway endpoint itself — set it so generated configs point at your production URL without requiring `--url` on every invocation.

```python
FRIESE_MCP_BASE_URL = "https://api.example.com/mcp/"
```

### `FRIESE_MCP_TRUSTED_PROXY_COUNT`

**Type:** `int` | **Default:** `0`

Number of trusted reverse proxies in front of the Django application. When `> 0`, friese-mcp reads `X-Forwarded-Proto` and `X-Forwarded-Host` for URL construction (OAuth well-known metadata, `WWW-Authenticate` resource URL) and pulls the real client IP from `X-Forwarded-For` when `RateLimitMiddleware` is configured with `key='ip'`.

Set this to the number of proxy hops that add a trusted `X-Forwarded-For` entry (typically `1` for a single nginx/Caddy/ALB in front of Django).

```python
FRIESE_MCP_TRUSTED_PROXY_COUNT = 1  # one nginx/Caddy/ALB in front of Django
```

See [Reverse proxy configuration](#reverse-proxy-configuration) for a full example.

> **Startup validation:** `friese_mcp.contrib.oauth` validates this setting at startup. If the value is not a non-negative integer, Django raises `ImproperlyConfigured` before the server accepts any requests. Booleans are explicitly rejected even though `bool` is a subclass of `int` in Python — set the actual count, not `True`.

### `FRIESE_MCP_HMAC_KEY`

**Type:** `str` | **Default:** absent (falls back to `SECRET_KEY`)

Independent HMAC key used to hash `FrieseMcpToken.token` and `OAuthClient.client_secret`. When set, token validity is decoupled from Django's `SECRET_KEY` — rotating `SECRET_KEY` no longer invalidates existing tokens.

```python
FRIESE_MCP_HMAC_KEY = env("FRIESE_MCP_HMAC_KEY")  # read from environment, never hardcode
```

When absent and `DEBUG = False`, friese-mcp logs a startup warning recommending you set this.

> **Key rotation warning:** Changing `FRIESE_MCP_HMAC_KEY` (or `SECRET_KEY` when this is unset) invalidates every existing `FrieseMcpToken` and `OAuthClient.client_secret` instantly. Treat this key like a password pepper — set it once in production and do not rotate without first regenerating all credentials.

### `FRIESE_MCP_TOOLS_LIST_CACHE_TTL`

**Type:** `int | None` | **Default:** `None` (caching disabled)

Caches the full `tools/list` manifest in Django's cache backend for the given number of seconds. When `None` (the default), every `tools/list` request rebuilds the manifest from the registry.

```python
FRIESE_MCP_TOOLS_LIST_CACHE_TTL = 60  # cache for 60 seconds
```

**When to enable:** if `tools/list` appears in your slow-request logs, enable caching. Deployments with 80+ tools see the most benefit, since schema serialisation time grows with tool count.

**Per-agent filtering interaction:** when `contrib.agents` is active and the authenticated agent has an `allowed_tools` allowlist, the cache is automatically skipped for that request. A filtered view is never written to the shared cache, so one agent's restricted manifest cannot poison the result for other callers.

**Cache invalidation:** the cache expires naturally after `FRIESE_MCP_TOOLS_LIST_CACHE_TTL` seconds. If you register tools at runtime (e.g. in a management command or test fixture), the new tools will not appear in `tools/list` until the TTL expires — unless you explicitly invalidate the cache:

```python
from friese_mcp import invalidate_tools_list_cache

invalidate_tools_list_cache()  # next tools/list request rebuilds from registry
```

Call `invalidate_tools_list_cache()` immediately after any runtime registration change to make the new manifest visible without waiting for the TTL.

### `FRIESE_MCP_UNAUTHENTICATED_TIER`

**Type:** `str | None` | **Default:** `"read"`

Controls which tool tier is visible to unauthenticated callers (i.e. when `request.auth is None`). The default `"read"` means only read-tagged tools appear in `tools/list` for unauthenticated requests — admin-tagged tools are not enumerable without a token.

```python
# Expose all tools to unauthenticated callers (open demo surface)
FRIESE_MCP_UNAUTHENTICATED_TIER = "admin"

# Expose only read-tier tools (default — prevents admin tool name enumeration)
FRIESE_MCP_UNAUTHENTICATED_TIER = "read"
```

Set to `None` to disable tier filtering for unauthenticated requests entirely (equivalent to `"admin"` but more explicit).

> **Security note:** The default `"read"` prevents unauthenticated callers from enumerating admin-tagged tool names. Even with `FRIESE_MCP_UNAUTHENTICATED_TIER = "admin"`, unauthenticated callers can only *see* tools in `tools/list` — `tools/call` still enforces `permission_classes` on each tool. Tier filtering is a visibility control, not an execution guard.

---

## Reverse proxy configuration

In production, Django typically runs behind a reverse proxy (nginx, Caddy, AWS ALB). By default, `request.build_absolute_uri('/')` returns the internal hostname and scheme, which causes two problems:

1. **OAuth well-known metadata** — the `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource` endpoints embed the issuer URL. With the internal hostname, MCP clients that perform OAuth auto-discovery will build token endpoint URLs that don't resolve.
2. **Rate limiting by IP** — `RateLimitMiddleware` with `key='ip'` reads `REMOTE_ADDR`, which is the proxy's IP when behind a proxy. All clients collapse into a single bucket.

### Option A — Set `FRIESE_MCP_TRUSTED_PROXY_COUNT` (dynamic, per-request)

Tell friese-mcp how many proxies are in the chain. It will read `X-Forwarded-Proto` and `X-Forwarded-Host` for URL construction, and extract the real client IP from `X-Forwarded-For`:

```python
# settings.py
FRIESE_MCP_TRUSTED_PROXY_COUNT = 1  # one nginx/Caddy/ALB in front of Django
```

Make sure your proxy sets these headers. Example nginx configuration:

```nginx
location /mcp/ {
    proxy_pass         http://django:8000;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   X-Forwarded-Host  $host;
}
```

### Option B — Set `FRIESE_MCP_OAUTH_ISSUER` (explicit, recommended for production)

For maximum predictability, set the issuer URL directly. friese-mcp uses this value without inspecting request headers:

```python
# settings.py
FRIESE_MCP_OAUTH_ISSUER = "https://api.example.com"
```

When `FRIESE_MCP_OAUTH_ISSUER` is unset and `DEBUG = False`, friese-mcp logs a startup warning recommending you set it.

### Recommended production settings

```python
# settings.py
FRIESE_MCP_OAUTH_ISSUER = "https://api.example.com"   # explicit — no header inspection
FRIESE_MCP_TRUSTED_PROXY_COUNT = 1                     # real client IP for rate limiting
FRIESE_MCP_BASE_URL = "https://api.example.com/mcp/"  # mcp_config command output
```

---

## Authentication and permissions

`McpEndpointView` extends DRF's `APIView`. This means authentication and permission enforcement happen at the DRF layer, before any method handler or tool is invoked.

### Gateway-level vs tool-level enforcement

friese-mcp has two independent permission enforcement points:

| Level | What it controls | How to configure |
|---|---|---|
| **Gateway** | Access to the entire `/mcp/` endpoint (all methods: ping, initialize, tools/list, tools/call, …) | `FRIESE_MCP_PERMISSION_CLASSES` |
| **Tool** | Access to a specific tool within `tools/call` | `permission_classes` on the ViewSet or `@mcp_tool` |

A request denied at gateway level receives a DRF 403 response before it reaches the JSON-RPC handler. A request denied at tool level receives an `isError: true` tool-level content response — not a JSON-RPC protocol error code. This keeps the JSON-RPC session alive so the agent can inspect the error and retry or call a different tool.

### Permission tiers — tools/list filtering

friese-mcp supports a three-tier permission model that controls which tools appear in `tools/list` for a given token. This lets you expose a minimal read-only surface to public callers while keeping write and admin tools hidden until a token grants access.

| Tier | Value | What it sees |
|---|---|---|
| Read | `"read"` | Tools tagged `read` only |
| Read-Write | `"read_write"` | Tools tagged `read` and `read_write` |
| Admin | `"admin"` | All tools |

**How tools are tagged:**

- `@mcp_tool(write=True)` → `"read_write"` tier
- `@mcp_tool(admin=True)` → `"admin"` tier
- `@mcp_tool(...)` (no kwargs) → `"read"` tier (default)
- `@mcp_action(write=True)` and `@mcp_heavy(write=True)` follow the same pattern
- ViewSet auto-discovery: GET actions → `"read"`, POST/PUT/PATCH/DELETE → `"read_write"`

**How tier is determined at request time:**

- Authenticated with `FrieseMcpToken` or `OAuthAccessToken`: tier is `token.permission`
- Authenticated but token lacks a `.permission` attribute: conservative fallback to `"read"`
- Unauthenticated (`request.auth is None`): `FRIESE_MCP_UNAUTHENTICATED_TIER` (default `"read"`)

**Dispatcher tools are always visible:** `@mcp_dispatcher` tools always appear in `tools/list` regardless of their action tiers — they are navigation entry points. Permission enforcement for write/admin actions within a dispatcher happens at `tools/call` time (the dispatcher returns a permission error, not a `tools/list` absence).

**Tier filtering and caching:** `FRIESE_MCP_TOOLS_LIST_CACHE_TTL` caches per-tier. A read-tier cache entry does not pollute the admin-tier result.

### Example: JWT-gated MCP surface

```python
# settings.py
FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "rest_framework_simplejwt.authentication.JWTAuthentication",
]
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

All MCP traffic now requires a valid JWT. Tools still enforce their own per-tool `permission_classes` on top of this.

### Example: separate auth for MCP and REST API

```python
# settings.py

# Standard REST API uses session auth
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ]
}

# MCP surface uses a custom token — doesn't affect the REST API
FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "myapp.authentication.MCPTokenAuthentication",
]
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

### BYOA — Bring Your Own Auth

The two contrib modules (`contrib.tokens`, `contrib.oauth`) are strictly optional conveniences. If your project already has auth infrastructure — Cognito JWTs, API keys, a custom token model, OAuth via `python-oauth2` — skip contrib entirely and plug your own class into `FRIESE_MCP_AUTHENTICATION_CLASSES`:

```python
FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "myapp.authentication.MCPTokenAuthentication",  # your own class
]
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

Any DRF `BaseAuthentication` subclass works. The [Built-in authentication](#built-in-authentication) section below documents what contrib provides if you want it.

---

## Built-in authentication

friese-mcp ships two opt-in auth modules under `friese_mcp.contrib`. Both are strict opt-ins — add them to `INSTALLED_APPS` only if you want to use them. Projects with existing auth (custom tokens, OAuth, API keys) skip contrib entirely and plug their own classes into `FRIESE_MCP_AUTHENTICATION_CLASSES`.

### `contrib.tokens` — static Bearer tokens

The simplest auth option. Create a token per client in Django admin, distribute it, and done. No expiry, no handshake — good for internal agents and scripted automation.

#### Setup

```python
# settings.py
INSTALLED_APPS = [
    ...
    "friese_mcp",
    "friese_mcp.contrib.tokens",
]

FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication",
]
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

```bash
python manage.py migrate
```

No URL configuration required — `contrib.tokens` only provides models and an authentication class.

#### Creating tokens

Tokens are generated automatically on first save. The raw Bearer value is exposed **once** as `instance.plaintext_token` on the freshly-created object — it is not stored in the database and cannot be retrieved later.

**Shell:**
```python
from friese_mcp.contrib.tokens.models import FrieseMcpToken

# Token linked to a user
token = FrieseMcpToken.objects.create(name="claude-agent", user=my_user)

# Service token — no user
token = FrieseMcpToken.objects.create(name="ci-pipeline")

print(token.plaintext_token)  # raw Bearer value — save this now, it cannot be retrieved later
```

**Django admin:** Navigate to **Friese MCP Tokens → Add**. Fill in a `name`, leave `token` blank, and save. The raw token is **not** displayed in the admin — use the shell method above when you need to capture it at creation time.

#### Using tokens

Include the token as a Bearer header:

```
POST /mcp/
Authorization: Bearer <token>
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
```

#### `FrieseMcpToken` model

| Field | Type | Description |
|---|---|---|
| `token` | `CharField(64)` | HMAC-SHA256 of the raw Bearer token keyed by `FRIESE_MCP_HMAC_KEY`. The raw value is exposed once as `instance.plaintext_token` immediately after creation and is never stored. |
| `name` | `CharField(200)` | Human-readable label (e.g. `"claude-agent"`). |
| `is_active` | `BooleanField` | Set to `False` to revoke. Inactive tokens are rejected. |
| `permission` | `CharField` | Permission tier: `"read"`, `"read_write"` (default), or `"admin"`. Controls which tool tier this token can see in `tools/list`. |
| `user` | `ForeignKey(AUTH_USER_MODEL, null=True)` | Optional user. `None` for service tokens. |
| `created_at` | `DateTimeField` | Auto-set on creation. |
| `last_used_at` | `DateTimeField(null=True)` | Updated on each successful authentication (queryset update, no signals). |

> **Pre-v1 tokens:** If your project created tokens before upgrading to v1.0 (when the field stored the raw value), those tokens will no longer authenticate. Delete and recreate them — this is a one-time migration step for early adopters.

> **`SECRET_KEY` rotation risk:** Token HMACs are keyed by `FRIESE_MCP_HMAC_KEY` when set, or `SECRET_KEY` when not. If `FRIESE_MCP_HMAC_KEY` is absent and you rotate `SECRET_KEY` (e.g. after a security incident), every `FrieseMcpToken` is permanently invalidated — all agents lose access instantly with no warning. **Recommended:** set `FRIESE_MCP_HMAC_KEY` to an independent secret in production so that `SECRET_KEY` rotation does not affect token validity.

#### `FrieseMcpTokenAuthentication`

Reads `Authorization: Bearer <token>`. Returns `(user, token)` on success, where `user` is the associated Django user or `AnonymousUser` for service tokens. Raises `AuthenticationFailed` on invalid or inactive tokens. Returns `None` (passes to next authenticator) when the `Authorization` header is absent or uses a different scheme.

> **Service tokens and `IsAuthenticated`:** A service token with no linked user sets `request.user` to `AnonymousUser`. `AnonymousUser.is_authenticated` is `False`, so `IsAuthenticated` will deny the request. Either link service tokens to a user, or use a custom permission class that allows `AnonymousUser`.

---

### `contrib.oauth` — OAuth 2.0

Full OAuth 2.0 for AI agent clients (Claude, GPT, Cursor, etc.). Supports two grant types:

- **Authorization code + PKCE (RFC 7636)** — the standard flow for AI clients that connect via OAuth in a browser (e.g. Claude.ai Add Connector). The client is redirected to `/oauth/authorize/`, receives a one-time code, and exchanges it for a Bearer token using a PKCE code verifier.
- **Client credentials (RFC 6749 §4.4)** — headless M2M flow. Clients exchange `client_id` + `client_secret` directly for a token. No browser redirect.

Both grant types issue `OAuthAccessToken` records with a configurable lifetime. Includes RFC 8414 authorization server metadata and MCP-spec protected resource metadata for automatic client discovery.

#### Setup

```python
# settings.py
INSTALLED_APPS = [
    ...
    "friese_mcp",
    "friese_mcp.contrib.oauth",
]

FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# Optional — defaults shown
FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 3600   # 1 hour
FRIESE_MCP_OAUTH_REGISTRATION_OPEN = False      # disable dynamic client registration
FRIESE_MCP_OAUTH_AUTO_APPROVE = True            # skip consent screen; set False to show it
# FRIESE_MCP_OAUTH_AUTHORIZE_URL = ""           # override authorization_endpoint in well-known
```

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("oauth/", include("friese_mcp.contrib.oauth.urls")),
    path(".well-known/", include("friese_mcp.contrib.oauth.wellknown_urls")),
    path("mcp/", include("friese_mcp.urls")),
]
```

```bash
python manage.py migrate
```

#### Managing OAuth clients

`client_id` and `client_secret` are auto-generated on first save. `client_id` is a public identifier and is readable at any time. `client_secret` is stored as an HMAC-SHA256 hash — the raw value is exposed **once** as `instance.plaintext_client_secret` on the freshly-created object and cannot be recovered later.

**Shell:**
```python
from friese_mcp.contrib.oauth.models import OAuthClient

client = OAuthClient.objects.create(name="claude-agent")
print(client.client_id)                 # 32-hex-char public identifier
print(client.plaintext_client_secret)   # raw secret — save this now, it cannot be retrieved later
```

**Django admin:** Navigate to **OAuth Clients → Add**. Set a name and permission level, then save. `client_id` is visible in the detail page; the raw `client_secret` is **not** — use the shell method above to capture it at creation time.

#### Authorization code flow (AI client connect — PKCE)

Used by AI clients that connect via OAuth in a browser, such as the Claude.ai "Add Connector" flow. PKCE (RFC 7636) is required — S256 only. No client secret is used for this grant.

**Step 1 — Redirect user to `/oauth/authorize/`:**

```
GET /oauth/authorize/
  ?response_type=code
  &client_id=<client_id>
  &redirect_uri=https://your-client.example.com/callback
  &code_challenge=<base64url(sha256(code_verifier))>
  &code_challenge_method=S256
  &state=<random-csrf-token>
```

If `FRIESE_MCP_OAUTH_AUTO_APPROVE = True` (the default), the server redirects immediately with a one-time code:

```
https://your-client.example.com/callback?code=<code>&state=<state>
```

If `FRIESE_MCP_OAUTH_AUTO_APPROVE = False`, a consent page is rendered first (`friese_mcp/oauth/authorize.html`, overridable). The user clicks Allow or Deny; Allow redirects with the code.

**Step 2 — Exchange the code for a token:**

```
POST /oauth/token/
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&code=<code>&redirect_uri=<same-as-step-1>&client_id=<id>&code_verifier=<verifier>
```

Response:

```json
{
  "access_token": "<64-hex-char token>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "mcp:read"
}
```

The scope string reflects the client's permission tier: `mcp:read`, `mcp:write`, or `mcp:admin`. Authorization codes are single-use and expire after 300 seconds.

**Step 3 — Use the token:** same as the client credentials flow below.

> **Production cache requirement:** Authorization codes are stored in Django's cache backend (300 s TTL). The default `LocMemCache` is per-process — in a multi-worker gunicorn deployment, a code written by worker A will not be found by worker B, causing intermittent `invalid_grant` errors. Set a shared cache backend (Redis, Memcached) in production. `OAuthConfig.ready()` logs a startup warning when `LocMemCache` is detected with `DEBUG = False`.

#### Client credentials flow (headless M2M)

**Step 1 — Exchange credentials for a token:**

```
POST /oauth/token/
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<id>&client_secret=<secret>
```

JSON body is also accepted:

```json
{
  "grant_type": "client_credentials",
  "client_id": "<id>",
  "client_secret": "<secret>"
}
```

Response:

```json
{
  "access_token": "<64-hex-char token>",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "mcp:read"
}
```

**Step 2 — Use the token:**

```
POST /mcp/
Authorization: Bearer <access_token>
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
```

**Step 3 — Refresh when expired:** Repeat step 1 to get a new token. There is no refresh token in the `client_credentials` grant — re-authenticate with the client credentials.

#### HTTP endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/oauth/authorize/` | Authorization code endpoint (RFC 6749 §4.1 + PKCE). Redirects with code on success. |
| `POST` | `/oauth/authorize/` | Consent form submission (only when `FRIESE_MCP_OAUTH_AUTO_APPROVE = False`). |
| `POST` | `/oauth/token/` | Issue an access token. Supports `authorization_code` and `client_credentials` grants. |
| `POST` | `/oauth/register/` | Dynamic client registration (RFC 7591). Disabled unless `FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True`. |
| `GET` | `/.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414). Includes `authorization_endpoint`. |
| `GET` | `/.well-known/oauth-protected-resource` | Protected resource metadata (MCP spec). |

#### Token endpoint errors

| `error` | HTTP | Cause |
|---|---|---|
| `unsupported_grant_type` | 400 | `grant_type` is not `client_credentials` or `authorization_code` |
| `invalid_request` | 400 | Required parameter missing |
| `invalid_client` | 401 | Credentials not found, or client is inactive |
| `invalid_grant` | 400 | Authorization code not found, expired, already used, or PKCE mismatch |

#### Dynamic client registration

When `FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True`, clients can self-register:

```
POST /oauth/register/
Content-Type: application/json

{"client_name": "my-agent", "scope": "mcp"}
```

Response (`201`):
```json
{
  "client_id": "<32-hex>",
  "client_secret": "<64-hex>",
  "client_name": "my-agent",
  "scope": "mcp"
}
```

> **Security note:** Dynamic registration is disabled by default. Enable it only in controlled environments where you want to allow clients to self-register. Any caller with network access can create a client.

#### Well-known discovery

MCP clients that support OAuth discovery can auto-configure by fetching:

- `GET /.well-known/oauth-protected-resource` — returns the MCP resource URL and authorization server base URL.
- `GET /.well-known/oauth-authorization-server` — returns the token endpoint, supported grant types, and (if enabled) the registration endpoint.

Use `FRIESE_MCP_OAUTH_ISSUER` to set an explicit base URL when the server is behind a reverse proxy:

```python
FRIESE_MCP_OAUTH_ISSUER = "https://api.example.com"
```

#### `OAuthTokenAuthentication`

Reads `Authorization: Bearer <token>`. Looks up the token in `OAuthAccessToken`, checks expiry and client active status. Returns `(OAuthServicePrincipal(), access_token)` on success. Raises `AuthenticationFailed` on invalid, expired, or inactive-client tokens. Returns `None` when the header is absent.

`OAuthServicePrincipal` is a minimal principal object with `is_authenticated = True` and no linked Django user. This means `rest_framework.permissions.IsAuthenticated` works correctly — the MCP client is authenticated as a service, not as a user account.

#### `OAuthClient` model

| Field | Type | Description |
|---|---|---|
| `client_id` | `CharField(32)` | Auto-generated 32-hex-char public identifier. Readable at any time. |
| `client_secret` | `CharField(64)` | HMAC-SHA256 of the raw client secret keyed by `FRIESE_MCP_HMAC_KEY`. The raw value is exposed once as `instance.plaintext_client_secret` immediately after creation and is never stored. |
| `name` | `CharField(200)` | Human-readable label. |
| `is_active` | `BooleanField` | Set to `False` to revoke all token issuance. Existing tokens are also rejected. |
| `permission` | `CharField` | Permission tier: `"read"`, `"read_write"` (default), or `"admin"`. Determines which tool tier the issued access token can see. |
| `created_at` | `DateTimeField` | Auto-set on creation. |

> **Pre-v1 clients:** If your project created `OAuthClient` records before upgrading to v1.0 (when `client_secret` stored the raw value), those clients will no longer be able to authenticate. Delete and recreate them — this is a one-time migration step for early adopters.

> **`SECRET_KEY` rotation risk:** `OAuthClient.client_secret` HMACs are keyed by `FRIESE_MCP_HMAC_KEY` when set, or `SECRET_KEY` when not. Rotating `SECRET_KEY` without first setting `FRIESE_MCP_HMAC_KEY` permanently invalidates every OAuth client secret — all agents must re-authenticate with new credentials. **Recommended:** set `FRIESE_MCP_HMAC_KEY` to an independent secret in production.

#### `OAuthAccessToken` model

| Field | Type | Description |
|---|---|---|
| `token` | `CharField(64)` | Auto-generated 64-hex-char Bearer token. |
| `client` | `ForeignKey(OAuthClient)` | Issuing client. Cascade-deletes with client. |
| `expires_at` | `DateTimeField` | Defaults to `now() + FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS`. |
| `permission` | `CharField` | Permission tier inherited from `client.permission` at issuance. Controls tool visibility in `tools/list`. |
| `created_at` | `DateTimeField` | Auto-set on creation. |

`is_expired()` method returns `True` if `now() >= expires_at`.

> **Token cleanup:** Expired tokens are not automatically deleted. Add a management command or scheduled task (e.g. Celery beat) to periodically run `OAuthAccessToken.objects.filter(expires_at__lt=now()).delete()`.

#### contrib.oauth settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS` | `int` | `3600` | Access token lifetime in seconds. |
| `FRIESE_MCP_OAUTH_REGISTRATION_OPEN` | `bool` | `False` | Enable RFC 7591 dynamic client registration. |
| `FRIESE_MCP_OAUTH_ISSUER` | `str` | auto-detected | Base URL used in well-known metadata. Set explicitly behind a reverse proxy. |
| `FRIESE_MCP_OAUTH_TOKEN_PATH` | `str` | `"/oauth/token/"` | Token endpoint path in well-known metadata. |
| `FRIESE_MCP_OAUTH_REGISTER_PATH` | `str` | `"/oauth/register/"` | Registration endpoint path in well-known metadata. |
| `FRIESE_MCP_PATH` | `str` | `"/mcp/"` | MCP gateway path used in protected-resource metadata. |
| `FRIESE_MCP_OAUTH_AUTO_APPROVE` | `bool` | `True` | Skip consent screen on `GET /oauth/authorize/`. When `False`, renders `friese_mcp/oauth/authorize.html` — overridable via Django's template override mechanism. |
| `FRIESE_MCP_OAUTH_AUTHORIZE_URL` | `str` | `""` | Override the `authorization_endpoint` advertised in `/.well-known/oauth-authorization-server`. Use when your IdP or SSO provider hosts the authorize endpoint. When empty, the package-provided `/oauth/authorize/` URL is used. |

#### Using both contrib modules together

List `OAuthTokenAuthentication` first so OAuth tokens are tried before static tokens:

```python
FRIESE_MCP_AUTHENTICATION_CLASSES = [
    "friese_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
    "friese_mcp.contrib.tokens.authentication.FrieseMcpTokenAuthentication",
]
FRIESE_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

DRF tries each authenticator in order and uses the first that succeeds.

---

### `contrib.agents` — per-agent tool allowlists

An optional app that lets you register named AI agent profiles in Django admin and restrict each agent to a specific subset of MCP tools. Useful when multiple agents (Claude Code, Cursor, GPT) share the same MCP server but should see different tool surfaces.

#### Setup

Requires `contrib.tokens` and/or `contrib.oauth` — `AgentConnection` links to either credential type.

```python
# settings.py
INSTALLED_APPS = [
    ...
    "friese_mcp",
    "friese_mcp.contrib.tokens",    # and/or contrib.oauth
    "friese_mcp.contrib.agents",
]
```

```bash
python manage.py migrate
```

No URL configuration required.

#### Creating agent connections

**Django admin:** Open `/admin/`, navigate to **Agent Connections**, and click **Add**. Set a name (e.g. `"Claude Code — production"`), choose the agent type, link a `FrieseMcpToken` or `OAuthClient`, and optionally fill in `allowed_tools`.

**Shell:**
```python
from friese_mcp.contrib.tokens.models import FrieseMcpToken
from friese_mcp.contrib.agents.models import AgentConnection

token = FrieseMcpToken.objects.create(name="claude-agent")
AgentConnection.objects.create(
    name="Claude Code — production",
    agent_type="claude-code",
    token=token,
    allowed_tools=["users.list", "workouts.create"],
)
```

#### How it works

When a request arrives at `tools/list` or `tools/call`, the gateway calls `_get_agent_connection(request)`:

1. If `friese_mcp.contrib.agents` is not installed → no filtering, all tools visible.
2. If `request.auth` is a `FrieseMcpToken` → look up the first active `AgentConnection` linked via `token`.
3. If `request.auth` is an `OAuthAccessToken` → look up the first active `AgentConnection` linked via `oauth_client`.
4. No matching connection → no filtering, all tools visible.

When a matching connection is found and `allowed_tools` is a non-null list:

- `tools/list` returns only the tools in `allowed_tools`.
- `tools/call` rejects calls to tools not in `allowed_tools` with `isError: true`.
- `last_seen_at` is stamped on the connection record on each successful `tools/call`.

Setting `allowed_tools` to `null` (the Django admin default) disables per-agent filtering for that connection — the agent sees all registered tools.

#### `AgentConnection` model

| Field | Type | Description |
|---|---|---|
| `name` | `CharField(200)` | Human-readable label (e.g. `"Claude Code — production"`). |
| `agent_type` | `CharField(50)` | Agent type: `claude-code`, `cursor`, `gpt`, `github-copilot`, `generic`. |
| `is_active` | `BooleanField` | Set to `False` to disable per-agent filtering for this entry without deleting it. |
| `allowed_tools` | `JSONField(null=True)` | JSON array of permitted tool names. `null` → unrestricted (all tools visible). |
| `token` | `ForeignKey(FrieseMcpToken, null=True)` | Linked static Bearer token credential. |
| `oauth_client` | `ForeignKey(OAuthClient, null=True)` | Linked OAuth 2.0 client credential. |
| `last_seen_at` | `DateTimeField(null=True)` | Updated on each `tools/call` from this agent. |
| `notes` | `TextField` | Optional free-text notes (owner, purpose, rotation schedule). |
| `created_at` | `DateTimeField` | Auto-set on creation. |

> **`is_active` behaviour:** When `is_active = False`, the `AgentConnection` is ignored by the gateway entirely. The linked credential remains valid and the agent can still call any tool — the per-agent filtering is simply not applied.

---

## Auto-discovery

When `FRIESE_MCP_ENABLED` and `FRIESE_MCP_AUTODISCOVER` are both `True`, `FrieseMcpConfig.ready()` runs the following sequence:

1. Instantiates the configured discovery backend (`DRFSyncDiscovery` by default).
2. Calls `backend.discover_tools()`, which walks the entire Django URL resolver tree recursively.
3. For each `URLPattern` whose callback has a `cls` attribute inheriting from `ViewSetMixin`:
   - Skips the class if it carries `_mcp_ignore = True` (set by `@mcp_ignore`).
   - Reads the `actions` mapping (`{http_method: action_name}`) from the bound view.
   - Derives a resource name from the URL path (see [Tool naming](#tool-naming)).
   - Derives an input schema from the serializer (see [Input schema derivation](#input-schema-derivation)).
   - Inherits `permission_classes` from the ViewSet class verbatim.
4. Each discovered `ToolDefinition` is registered in the global `tool_registry`.
5. Logs `friese_mcp: auto-discovery registered N tools` at INFO level.

Each `(ViewSet class, action)` pair is registered at most once. When the same ViewSet appears at multiple URL patterns (e.g. list route `/users/` and detail route `/users/<pk>/`), duplicate `(cls, action_name)` pairs are deduplicated via a `seen` set.

### Surface area control per ViewSet

Use `mcp_include_actions` and `mcp_exclude_actions` to control which actions are registered for a specific ViewSet without touching `@mcp_ignore` or settings-level lists:

```python
class UserViewSet(ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer

    # Only these two actions become MCP tools — all others are suppressed.
    mcp_include_actions = ["list", "retrieve"]

class AuditLogViewSet(ReadOnlyModelViewSet):
    queryset = AuditLog.objects.all()

    # All actions except destroy are registered.
    mcp_exclude_actions = ["destroy"]
```

- `mcp_include_actions` — explicit allowlist for this ViewSet. Only the listed action names are registered. All others are silently skipped.
- `mcp_exclude_actions` — denylist for this ViewSet. Listed action names are skipped; all others are registered.
- Both may be present; `mcp_include_actions` is applied first (an action not in the include list is never registered even if it is not in the exclude list).
- Neither attribute is inherited — they must be declared on the concrete ViewSet class.

### Tool naming

Tool names follow the pattern `{resource}.{action}`, where:

- **resource** — the last non-empty literal segment of the URL path, with hyphens converted to underscores and URL parameter placeholders (`<pk>`, `(?P<pk>...)`) stripped. Examples: `/api/v1/users/` → `users`, `/api/orders/<pk>/` → `orders`.
- **action** — the DRF ViewSet action name: `list`, `retrieve`, `create`, `update`, `partial_update`, `destroy`, or any custom action name.

> **Note:** The resource name is derived from the URL path, not the ViewSet class name. A custom action at `/api/users/export/` produces the tool name `export.export` (last path segment), not `users.export`. Register such tools explicitly with `@mcp_tool` if you need a cleaner name.

### Input schema derivation

`DRFSyncDiscovery.get_input_schema()` builds a JSON Schema (draft-07) for each action:

- **Detail actions** (`retrieve`, `update`, `partial_update`, `destroy`): always includes an `"id"` property. `id` accepts either an integer or a string (`anyOf: [{type: integer}, {type: string}]`) so that UUID-keyed models work without schema validation errors. `id` is required for all detail actions except `partial_update`.
- **Write actions** (`create`, `update`, `partial_update`): instantiates the ViewSet's serializer via `get_serializer_class()` and maps each non-read-only field to a JSON Schema type. Required serializer fields become required schema properties.
- **List actions** with filter backends: introspects `filter_backends` on the ViewSet and adds query-parameter properties for `SearchFilter` (`search`), `OrderingFilter` (`ordering` with optional enum of valid field names), and `DjangoFilterBackend` (`filterset_fields` or `filterset_class.base_filters`). See [Filter and search parameters](#filter-and-search-parameters) below.
- **Custom GET `@action` methods**: extracts typed parameters from the method signature using `inspect.signature()` + `typing.get_type_hints()`. Parameters with defaults are optional; those without are required. Parameters named `self`, `request`, `pk`, `format`, `args`, or `kwargs` are skipped.
- **Fallback**: `{"type": "object"}` when serializer introspection fails (no `get_serializer_class`, read-only ViewSet, serializer requires an active request, etc.).

DRF field → JSON Schema type mapping:

| DRF field types | JSON type |
|---|---|
| `CharField`, `EmailField`, `URLField`, `SlugField`, `RegexField`, `UUIDField`, `FilePathField`, `IPAddressField`, `DateField`, `DateTimeField`, `TimeField`, `DurationField` | `string` |
| `IntegerField`, `SmallIntegerField`, `BigIntegerField` | `integer` |
| `FloatField`, `DecimalField` | `number` |
| `BooleanField`, `NullBooleanField` | `boolean` |
| `ListField` | `array` |
| `DictField`, `JSONField` | `object` |
| All others | `string` (fallback) |

### Filter and search parameters

When a ViewSet declares `filter_backends`, auto-discovery adds the corresponding query-parameter properties to the `list` action schema:

```python
from rest_framework.filters import OrderingFilter, SearchFilter
from django_filters.rest_framework import DjangoFilterBackend

class UserViewSet(ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    filter_backends = [SearchFilter, OrderingFilter, DjangoFilterBackend]
    search_fields = ["username", "email"]
    ordering_fields = ["username", "created_at"]
    filterset_fields = ["is_active", "role"]
```

This produces a `users.list` schema that includes `search`, `ordering` (with `enum` values `["username", "-username", "created_at", "-created_at"]`), `is_active`, and `role` as optional string parameters.

The three supported backends:

| Backend class | Parameter added | Notes |
|---|---|---|
| `SearchFilter` | `search: string` | Full-text search term |
| `OrderingFilter` | `ordering: string` | Comma-separated field names. Prefix `-` for descending. Includes `enum` when `ordering_fields` is not `"__all__"`. |
| `DjangoFilterBackend` | One entry per filterset field | Reads `filterset_fields` (list or dict) then falls back to `filterset_class.base_filters`. django-filter is an optional dependency; detected by class name. |

Custom filter backends beyond these three are not introspected. Add them via `@mcp_tool` if you need richer schemas.

### Custom `@action` GET schema example

Typed parameters on a custom GET action are automatically surfaced as schema properties:

```python
from rest_framework.decorators import action
from rest_framework.response import Response

class ReportViewSet(ModelViewSet):
    ...

    @action(detail=False, methods=["get"])
    def summary(self, request, format: str = "json", limit: int = 100):
        """Return a summary report."""
        ...
```

This produces a `reports.summary` tool with optional `format` (string) and `limit` (integer) properties. Parameters without annotations fall back to type `string`.

> **Tip:** For complex custom actions, use `@mcp_tool` to declare an explicit schema. Auto-derived schemas from signatures are a convenience for simple read-only actions.

---

## Decorators

### `@mcp_tool`

Explicitly register any callable as an MCP tool. The decorated function is registered as a side effect and returned unchanged.

```python
from django.http import HttpRequest
from friese_mcp import mcp_tool
from rest_framework.permissions import IsAuthenticated

@mcp_tool(
    name="orders.cancel",
    description="Cancel an order by ID.",
    input_schema={
        "type": "object",
        "properties": {"order_id": {"type": "integer"}},
        "required": ["order_id"],
    },
    permission_classes=[IsAuthenticated],
)
def cancel_order(arguments: dict, request: HttpRequest) -> dict:
    order = Order.objects.get(pk=arguments["order_id"])
    order.cancel()
    return {"cancelled": order.pk}
```

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Unique MCP tool name. Overwrites any existing registration with the same name. |
| `description` | `str` | Yes | Human-readable description shown in `tools/list`. |
| `input_schema` | `dict` | Yes | JSON Schema (draft-07) for argument validation. |
| `permission_classes` | `list[type[BasePermission]]` | No | DRF permission classes. Pass `None` or `[]` for unrestricted access. |
| `write` | `bool` | No | Set `True` to assign `permission_tier="read_write"`. The tool is hidden from `tools/list` for read-only tokens. Default `False`. |
| `admin` | `bool` | No | Set `True` to assign `permission_tier="admin"`. The tool is only visible to admin-tier tokens. Takes precedence over `write`. Default `False`. |

The decorated callable must have the signature `(arguments: dict, request: HttpRequest) -> Any` and return a JSON-serialisable value.

### `@mcp_ignore`

Exclude a ViewSet class or individual action method from auto-discovery. Has no effect on tools registered via `@mcp_tool`.

```python
from friese_mcp import mcp_ignore

# Exclude an entire ViewSet
@mcp_ignore
class InternalViewSet(ModelViewSet):
    ...

# Exclude a single action
class UserViewSet(ModelViewSet):
    @mcp_ignore
    def private_action(self, request):
        ...
```

`@mcp_ignore` sets `_mcp_ignore = True` on the target object. The discovery backend checks this attribute before registering each ViewSet or action.

### `@mcp_dispatcher` and `@mcp_action`

Register a class as a single MCP **dispatcher tool** — one tool name that routes to multiple named actions. Use `@mcp_dispatcher` when you have a family of related operations that share context (e.g. tasks, rooms, projects): it reduces tool count and enables progressive disclosure via built-in help-mode.

**When to use `@mcp_dispatcher` vs `@mcp_tool`:**

| | `@mcp_tool` | `@mcp_dispatcher` |
|---|---|---|
| Structure | One callable, one tool | One class, one tool, many actions |
| Best for | Standalone, independent operations | Related operations sharing context or a namespace |
| Tool count | One tool per function | One tool for the whole family |
| Help mode | None | Built-in — call without `action` or with `action="help"` |

```python
from django.http import HttpRequest
from friese_mcp import mcp_dispatcher, mcp_action

@mcp_dispatcher(name="tasks", description="Manage project tasks.")
class TasksDispatcher:

    @mcp_action(
        name="create",
        description="Create a new task.",
        params={"title": "Task title", "priority": "Integer 1–5 (default 3)"},
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["title"],
        },
    )
    def create(self, request: HttpRequest, params: dict) -> dict:
        task = Task.objects.create(
            title=params["title"],
            priority=params.get("priority", 3),
            created_by=request.user,
        )
        return {"id": task.pk, "title": task.title}

    @mcp_action(
        name="list",
        description="List tasks, optionally filtered by status.",
        params={"status": "Filter by status: open, closed, all (default all)"},
    )
    def list(self, request: HttpRequest, params: dict) -> dict:
        qs = Task.objects.all()
        if params.get("status") in ("open", "closed"):
            qs = qs.filter(status=params["status"])
        return {"tasks": list(qs.values("id", "title", "status"))}

    @mcp_action(
        name="get",
        description="Retrieve a single task by ID.",
        params={"id": "Task ID"},
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    )
    def get(self, request: HttpRequest, params: dict) -> dict:
        task = Task.objects.get(pk=params["id"])
        return {"id": task.pk, "title": task.title, "status": task.status}
```

**`@mcp_dispatcher(name, description)`** — class decorator. Scans the class for `@mcp_action` methods, instantiates the class once at decoration time, and registers it as a single MCP tool with a compact `inputSchema`. The class instance is reused across calls; `request` is passed per-call.

**`@mcp_action(name, description, params=None, input_schema=None, write=False, admin=False)`** — method decorator. Marks a method as a dispatchable action. Does not alter the method's behaviour.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Action name used as the `action` argument value (e.g. `"create"`). |
| `description` | `str` | Yes | Human-readable description shown in help-mode responses. |
| `params` | `dict[str, str]` | No | Mapping of param name → human-readable hint. Shown in help-mode. |
| `input_schema` | `dict` | No | JSON Schema (draft-07) for server-side validation of `params` before the method is called. |
| `write` | `bool` | No | Set `True` to assign `permission_tier="read_write"` to this action. Default `False`. |
| `admin` | `bool` | No | Set `True` to assign `permission_tier="admin"` to this action. Takes precedence over `write`. Default `False`. |

> **Dispatcher tools are always visible:** The parent `@mcp_dispatcher` tool always appears in `tools/list` for all callers regardless of its actions' tiers — it is a navigation entry point. Per-action tier enforcement fires at `tools/call` time: if a token's tier is insufficient for an action, the dispatcher returns a permission error response rather than hiding the tool from `tools/list`.

Action method signature:

```python
def action_name(self, request: HttpRequest, params: dict) -> dict:
    ...
```

`params` is the raw dict from the `params` key of the incoming `tools/call` arguments (defaulting to `{}`).

#### Help mode

Call the dispatcher tool without an `action` argument, or with `action="help"`, to receive a structured listing of all available actions:

```json
// Request
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {"name": "tasks", "arguments": {"action": "help"}}
}

// Response
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "content": [{
      "type": "text",
      "text": "{\"help\": true, \"dispatcher\": \"tasks\", \"actions\": [{\"name\": \"create\", \"description\": \"Create a new task.\", \"params\": {\"title\": \"Task title\", \"priority\": \"Integer 1-5 (default 3)\"}, \"input_schema\": {...}}, ...]}"
    }],
    "isError": false
  }
}
```

If the caller sends an unrecognised action name, the dispatcher returns a `LookupError` with a close-match suggestion (e.g. `"Unknown action 'creat'. Did you mean: 'create'?"`), surfaced as `isError: true`.

#### Generated `inputSchema`

`@mcp_dispatcher` generates a compact schema automatically — no hand-written JSON Schema needed at the tool level:

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": ["create", "list", "get"],
      "description": "Operation to perform. Omit or use 'help' to list all available actions and their required parameters."
    },
    "params": {
      "type": "object",
      "additionalProperties": true,
      "description": "Parameters for the chosen action. See help for details."
    }
  }
}
```

Per-action parameter schemas live in `@mcp_action(input_schema=...)` and are applied server-side, not in the top-level tool schema. This keeps `tools/list` compact while still enforcing per-action constraints at call time.

#### Server-side validation

When `input_schema` is set on an `@mcp_action`, the `params` dict is validated against that schema before the method is called. Validation failure returns `isError: true` without invoking the method:

```json
{
  "content": [{"type": "text", "text": "{\"error\": \"Invalid params for action 'create': 'title' is a required property\"}"}],
  "isError": true
}
```

#### Export

`mcp_dispatcher` and `mcp_action` are exported from `friese_mcp` directly:

```python
from friese_mcp import mcp_dispatcher, mcp_action
```

#### Dispatcher precedence over auto-discovery

When a `@mcp_dispatcher` is registered for a resource name, friese-mcp automatically suppresses any auto-discovered ViewSet tools whose resource name conflicts. The dispatcher declaration is authoritative for its resource's MCP surface — no `@mcp_ignore` needed on the underlying ViewSet.

**Why:** Writing `@mcp_dispatcher("exercises")` is an explicit declaration that you own the `exercises` resource. If auto-discovery also emits `exercise.list`, `exercise.create`, etc. from the underlying ViewSet, both sets of names appear in `tools/list` but only the dispatcher routes correctly — the flat names return `-32601`. The auto-suppression eliminates this split-brain at startup rather than at call time.

**Match rule:** Strict prefix match. A dispatcher named `exercises` suppresses any auto-discovered tool whose resource prefix is `exercises` (exact) or `exercise` (singular — strips one trailing `s`). No fuzzy matching beyond that.

| Auto-discovered tool | Dispatcher | Suppressed? |
|---|---|---|
| `exercise.list` | `exercises` | Yes — singular match |
| `exercises.custom` | `exercises` | Yes — exact match |
| `programs.list` | `exercises` | No — different resource |
| `users.list` | `users` | Yes — exact match |

**Suppression logging:** When a tool is suppressed, friese-mcp logs at INFO:

```
friese_mcp: suppressing auto-discovered tool 'exercise.list' — shadowed by dispatcher 'exercises'
```

Check your logs if expected auto-discovered tools go missing — a misspelled dispatcher name would silently suppress the wrong resource without this signal.

**Three scenarios:**

1. **Auto-discovery only** (no `@mcp_dispatcher` registered) — nothing suppressed, unchanged behaviour. All ViewSet tools appear in `tools/list` as usual.

2. **Dispatchers only** (`FRIESE_MCP_AUTODISCOVER = False`, or no matching ViewSets) — nothing to suppress. Dispatchers register and appear in `tools/list` normally.

3. **Mixed** (dispatchers for some resources, auto-discovery for others) — only the shadowed ViewSet tools are suppressed. Other ViewSets register normally alongside the dispatchers.

**Custom handlers and fresh projects:** Projects that use custom tool handlers (not DRF ViewSets) will find that auto-discovery produces no tools for those handlers — suppression never fires. Projects with only dispatchers or only auto-discovery are also unaffected.

**Intentional-both edge case:** If you genuinely need both a `@mcp_dispatcher("exercises")` and flat auto-discovered `exercise.*` tools in `tools/list`, the suppression will remove the flat tools. This is intentional — advertising both would recreate the split-brain bug. Use distinct resource names if you need both surfaces.

### `@mcp_resource`

Expose server-side content as an MCP resource via `resources/list` and `resources/read`. Resources are ideal for static or semi-static content that agents should read rather than invoke — configuration files, schema definitions, domain reference data.

```python
from django.http import HttpRequest
from friese_mcp import mcp_resource

@mcp_resource(
    uri_template="rag://products/{product_id}/spec",
    name="Product spec",
    description="Technical specification for a product, as plain text.",
    mime_type="text/plain",
)
def product_spec(uri: str, request: HttpRequest) -> str:
    # uri is the concrete URI from the resources/read request
    product_id = uri.split("/")[-1]
    product = Product.objects.get(pk=product_id)
    return product.spec_text
```

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `uri_template` | `str` | Yes | Resource URI. May include `{variable}` placeholders — the decorated function receives the full concrete URI and is responsible for parsing any variables. |
| `name` | `str` | Yes | Human-readable name shown in `resources/list`. |
| `description` | `str` | No | Optional description shown in `resources/list`. |
| `mime_type` | `str` | No | MIME type of the returned content. Defaults to `"text/plain"`. |

The decorated function must have the signature `(uri: str, request: HttpRequest) -> str` and return the resource contents as a string.

`@mcp_resource` is exported from `friese_mcp` directly:

```python
from friese_mcp import mcp_resource
```

#### `resources/list` response

```json
{
  "jsonrpc": "2.0", "id": 2,
  "result": {
    "resources": [
      {
        "uri": "rag://products/{product_id}/spec",
        "name": "Product spec",
        "description": "Technical specification for a product, as plain text.",
        "mimeType": "text/plain"
      }
    ]
  }
}
```

#### `resources/read` request

```json
{
  "jsonrpc": "2.0", "id": 3, "method": "resources/read",
  "params": {"uri": "rag://products/42/spec"}
}
```

The URI in the `resources/read` request is matched by exact lookup against registered `uri_template` values. If no handler matches, the gateway returns `-32601 METHOD_NOT_FOUND`.

---

## Tool call middleware

`FRIESE_MCP_TOOL_MIDDLEWARE` is a list of dotted-path class strings that are instantiated at startup and wrapped around every `tools/call` dispatch. Use middleware for cross-cutting concerns: audit logging, heartbeat stamping, tenant checks, per-call observability.

```python
# settings.py
FRIESE_MCP_TOOL_MIDDLEWARE = [
    "myapp.mcp.AuditLogMiddleware",
    "myapp.mcp.WorkerHeartbeatMiddleware",
]
```

Middleware runs in declaration order — the first entry is outermost (called first on the way in, last on the way out).

### Middleware class contract

Each middleware is a plain class. friese-mcp instantiates it once at startup (via `load_middleware()`) and calls it as a callable on every `tools/call`:

```python
class AuditLogMiddleware:
    def __call__(self, request, tool_name: str, arguments: dict, call_next):
        # Before the tool call:
        import logging
        logging.getLogger("audit").info("tool_call tool=%s user=%s", tool_name, request.user)

        result = call_next(request, tool_name, arguments)

        # After the tool call:
        logging.getLogger("audit").info("tool_done  tool=%s", tool_name)
        return result
```

**Parameters received by `__call__`:**

| Parameter | Type | Description |
|---|---|---|
| `request` | `HttpRequest` | The current DRF request, including `request.user` and `request.auth`. |
| `tool_name` | `str` | Name of the tool being called (e.g. `"users.list"`). |
| `arguments` | `dict` | Raw arguments dict from the `tools/call` payload. |
| `call_next` | `callable` | The next middleware in the chain (or the tool itself). Call it as `call_next(request, tool_name, arguments)`. |

The middleware must return the result of `call_next(...)` (or a replacement value). Raising an exception aborts the chain — `PermissionError` is converted to `isError: true`; other exceptions bubble up as internal errors.

### `RateLimitMiddleware`

A built-in rate limiter that ships in `friese_mcp.contrib.middleware`. Uses an in-process sliding-window counter — no Redis or external dependency required. Configuration is read from `FRIESE_MCP_RATE_LIMIT`.

```python
# settings.py
FRIESE_MCP_TOOL_MIDDLEWARE = [
    "friese_mcp.contrib.middleware.RateLimitMiddleware",
]

FRIESE_MCP_RATE_LIMIT = {
    "rate": "100/m",    # <count>/<period>: s = second, m = minute, h = hour
    "key": "user_id",   # "user_id" | "tenant_id" | "ip"
}
```

When `FRIESE_MCP_RATE_LIMIT` is absent, `RateLimitMiddleware` is a no-op. The rate limit is per-process — not shared across workers. For multi-process deployments, use a shared backend (Redis, database) in a custom middleware class.

**Key resolution:**

| `key` | Bucket identifier |
|---|---|
| `"user_id"` | `str(request.user.pk)`, or `"anonymous"` for unauthenticated requests. |
| `"tenant_id"` | `str(request.user.tenant_id)` if the attribute exists; falls back to `user_id` resolution. |
| `"ip"` | `request.META["REMOTE_ADDR"]`. |

When the limit is exceeded, `RateLimitMiddleware` raises `PermissionError("Rate limit exceeded")`, which the gateway converts to an `isError: true` tool-level response:

```json
{
  "content": [{"type": "text", "text": "{\"error\": \"Rate limit exceeded\"}"}],
  "isError": true
}
```

---

## ToolRegistry API

`friese_mcp.tool_registry` is a module-level singleton. Import it directly:

```python
from friese_mcp import tool_registry
```

Instantiate `ToolRegistry()` directly only when an isolated registry is needed (e.g. in tests).

### `ToolRegistry.register(name, fn, description, input_schema, permission_classes=None)`

Register a callable as a named MCP tool. Thread-safe. Overwrites any existing registration with the same `name` silently.

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Unique tool name. |
| `fn` | `Callable` | Invoked as `fn(arguments, request)`. |
| `description` | `str` | Human-readable description. |
| `input_schema` | `dict` | JSON Schema (draft-07). |
| `permission_classes` | `list[type[BasePermission]] \| None` | DRF permission classes. `None` or `[]` for unrestricted. |

### `ToolRegistry.list_tools() -> list[dict]`

Return the tool manifest in MCP `tools/list` format. Thread-safe. Returns all registered tools regardless of the caller's identity (see [Auth and tools/list](#auth-and-toolslist)).

Each entry:

```json
{
  "name": "users.list",
  "description": "List User objects",
  "inputSchema": { "type": "object", "properties": {} }
}
```

### `ToolRegistry.dispatch(request, name, arguments) -> Any`

Validate, authorise, and invoke a registered tool. Thread-safe. Steps:

1. Look up the tool by `name` — raises `ToolNotFoundError` if absent.
2. Validate `arguments` against `input_schema` — raises `ToolInputError` on failure.
3. Evaluate each `permission_class` in declaration order — raises `PermissionError` on first denial.
4. Call `fn(arguments, request)` and return the result.

**Exceptions:**

| Exception | Base | Raised when |
|---|---|---|
| `ToolNotFoundError` | `LookupError` | No tool with the given `name` is registered. |
| `ToolInputError` | `ValueError` | Arguments fail JSON Schema validation. |
| `PermissionError` | built-in | A permission class denies access. |

---

## MCP gateway endpoint

**URL:** configured by the host app — default `POST /mcp/`
**Protocol:** JSON-RPC 2.0 / MCP `2025-03-26` (Streamable HTTP) over HTTP POST
**Content-Type:** `application/json`
**CSRF:** exempt — `McpEndpointView` extends DRF `APIView`, which bypasses Django's CSRF middleware

All requests and responses follow [JSON-RPC 2.0](https://www.jsonrpc.org/specification). The endpoint handles all MCP traffic through a single URL.

### Supported methods

#### `ping`

Liveness check. Returns an empty result object.

```json
// Request
{"jsonrpc": "2.0", "id": 1, "method": "ping"}

// Response
{"jsonrpc": "2.0", "id": 1, "result": {}}
```

#### `initialize`

MCP protocol handshake. Call once before issuing other requests.

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-03-26",
    "clientInfo": {"name": "my-client", "version": "1.0"}
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-03-26",
    "serverInfo": {"name": "friese-mcp", "version": "0.1.0"},
    "capabilities": {"tools": {}, "resources": {}}
  }
}
```

The server always responds with its own `protocolVersion` (`2025-03-26`) regardless of what the client sends. `serverInfo.name` is controlled by `FRIESE_MCP_SERVER_NAME`. `serverInfo.version` is read from the installed package metadata via `importlib.metadata` and falls back to `"unknown"` when the package is not installed via a standard distribution (e.g. a bare source checkout with no `pip install`).

#### `initialized`

Client confirmation notification. Send after `initialize`.

Per the MCP Streamable HTTP spec (2025-03-26), `initialized` is a **notification** — a JSON-RPC message with no `"id"` field. The server returns HTTP 202 Accepted with an empty body (see [Notifications](#notifications) below).

```
POST /mcp/
Content-Type: application/json

{"jsonrpc": "2.0", "method": "initialized"}

→ HTTP 202 (empty body)
```

#### `tools/list`

Enumerate registered MCP tools. When `contrib.agents` is installed and the caller's credential is linked to an active `AgentConnection` with a non-null `allowed_tools` list, only those tools are returned. See [Auth and tools/list](#auth-and-toolslist) for the full auth model.

```json
// Request
{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

// Response
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "users.list",
        "description": "List User objects",
        "inputSchema": {"type": "object", "properties": {}}
      }
    ]
  }
}
```

#### `tools/call`

Invoke a registered tool.

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "users.retrieve",
    "arguments": {"id": 42}
  }
}

// Success response
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"id\": 42, \"username\": \"alice\"}"}],
    "isError": false
  }
}

// Error response (tool execution failed)
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"error\": \"Internal tool error\"}"}],
    "isError": true
  }
}
```

The raw exception message is never returned to the caller. Full error details are logged server-side via `logger.exception`.

#### `resources/list`

Returns all resources registered via `@mcp_resource`. Returns an empty list if no resources are registered.

#### `resources/read`

Dispatches to the matching `@mcp_resource` handler by exact URI lookup. Returns `-32602 Invalid Params` if no handler is registered for the requested URI. See the [`@mcp_resource` section](#mcp_resource) for registration details.

#### `help`

Returns server metadata and usage hints. Designed for AI agents that need to self-orient without out-of-band documentation.

```json
// Request
{"jsonrpc": "2.0", "id": 5, "method": "help"}

// Response
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "server": "friese-mcp",
    "protocolVersion": "2025-03-26",
    "methods": ["initialize", "initialized", "tools/list", "tools/call", "resources/list", "ping", "help"],
    "hints": {
      "discovery": "Call tools/list to enumerate available tools and their inputSchema.",
      "invocation": "Call tools/call with {name, arguments} to invoke a tool.",
      "errors": "Tool errors return isError=true with content[0].text as JSON. Check the 'error' key for the message and 'detail' for field-level hints.",
      "unknown_tool": "If tools/call returns -32601, the tool name is unrecognised. Re-run tools/list for the correct name — suggestions are included in the error data field."
    }
  }
}
```

### Notifications

Per the MCP Streamable HTTP spec (2025-03-26), a JSON-RPC **notification** is a message with no `"id"` key (distinct from `"id": null`). When the server receives a notification it MUST return HTTP 202 Accepted with an empty body — no JSON-RPC response body.

```
POST /mcp/
Content-Type: application/json

{"jsonrpc": "2.0", "method": "initialized"}

→ HTTP/1.1 202 Accepted
   (empty body)
```

All notifications are handled this way regardless of method name. Only `initialized` is logged at INFO level; other notifications are logged at DEBUG.

### Error handling in `tools/call`

Tool-level errors are returned as `isError: true` content blocks inside a JSON-RPC **success** response (HTTP 200, no `error` key). This keeps the JSON-RPC session alive and lets the agent inspect the error without the session terminating.

| Error condition | `isError` content |
|---|---|
| Permission denied | `{"error": "<permission message>"}` |
| DRF `ValidationError` (field errors) | `{"error": "Validation failed", "detail": {"field": ["message"]}}` |
| DRF `ValidationError` (non-field) | `{"error": "<joined messages>"}` |
| Django `ValidationError` | `{"error": "<joined messages>"}` |
| `ValueError` from tool handler | `{"error": "<message>"}` |
| Unhandled exception | `{"error": "Internal tool error"}` (details in server log) |

Unknown tool names return a JSON-RPC `-32601 METHOD_NOT_FOUND` error with close-match suggestions in the `data` field:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32601,
    "message": "Unknown tool",
    "data": "No tool named 'user.lis'. Did you mean: users.list, users.list_active? Call tools/list to refresh your available tools — the server manifest may have changed."
  }
}
```

### SSE support

friese-mcp supports Server-Sent Events on the same `/mcp/` endpoint. Send `Accept: text/event-stream` and every JSON-RPC response is wrapped in a single SSE event:

```
POST /mcp/
Accept: text/event-stream
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

→ HTTP/1.1 200 OK
   Content-Type: text/event-stream
   Cache-Control: no-cache

   data: {"jsonrpc":"2.0","id":1,"result":{"tools":[...]}}

   (stream closes)
```

The stream is stateless — it closes after delivering the single response. Clients that do not send `Accept: text/event-stream` receive a normal `application/json` response and are unaffected by this change. HTTP 202 notifications are not wrapped in SSE (they return an empty body as before).

### Session ID header

Every `initialize` response includes an `Mcp-Session-Id` header containing a fresh UUID:

```
HTTP/1.1 200 OK
Mcp-Session-Id: 3fa85f64-5717-4562-b3fc-2c963f66afa6
Content-Type: application/json
```

The ID is stateless — friese-mcp generates a new UUID per `initialize` call and does not track it server-side. Clients may use it as a correlation handle for logging. To opt out:

```python
FRIESE_MCP_SESSION_ID_HEADER = False
```

### `tools/list` cursor pagination

By default, `tools/list` returns all registered tools in a single response. For deployments with a large number of tools (80+), enable cursor pagination with:

```python
FRIESE_MCP_TOOLS_PAGE_SIZE = 20
```

**First page — no cursor:**

```json
// Request
{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

// Response (more pages available)
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "tools": [ /* first 20 tools */ ],
    "nextCursor": "MjA="
  }
}
```

**Subsequent pages — pass `cursor`:**

```json
// Request
{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": "MjA="}}

// Response (last page — no nextCursor)
{
  "jsonrpc": "2.0", "id": 2,
  "result": {
    "tools": [ /* remaining tools */ ]
  }
}
```

When `nextCursor` is absent, the client has reached the last page. Cursors are opaque base64url-encoded integer offsets. An invalid cursor returns `-32602 INVALID_PARAMS`.

When `FRIESE_MCP_TOOLS_PAGE_SIZE` is absent (the default), `tools/list` behaves exactly as before — no `cursor` parameter is consumed and no `nextCursor` is returned.

### `tools/list` manifest caching

For deployments where `tools/list` is called frequently, enable manifest caching via `FRIESE_MCP_TOOLS_LIST_CACHE_TTL`. The manifest is stored in Django's configured cache backend and served without touching the registry on cache hits.

Cache is automatically bypassed when `contrib.agents` is filtering tools for the authenticated agent — a filtered view is never written to the shared cache. See [`FRIESE_MCP_TOOLS_LIST_CACHE_TTL`](#friese_mcp_tools_list_cache_ttl) in the settings reference for full details, including the `invalidate_tools_list_cache()` helper for runtime invalidation.

### HTTP-level behaviour

| Condition | HTTP status | JSON-RPC error code |
|---|---|---|
| Non-POST request (except DELETE) | 405 | `-32600` (Invalid Request) |
| DELETE request | 200 | `{}` empty body — stateless no-op |
| `FRIESE_MCP_ENABLED = False` | 503 | `-32603` (Internal Error) |
| All other responses | 200 | See error codes below |

**DELETE no-op:** friese-mcp accepts `DELETE /mcp/` and returns HTTP 200 `{}` with no authentication required. This allows agent clients that send a session-cleanup `DELETE` at the end of a session to do so without error, even though friese-mcp is stateless and holds no session state to clean up.

### JSON-RPC error codes

| Code | Name | When |
|---|---|---|
| `-32700` | Parse error | Request body is not valid JSON |
| `-32600` | Invalid Request | Missing/wrong `jsonrpc` field, `method` is not a string, or non-POST HTTP method |
| `-32601` | Method Not Found | Unrecognised method name, or unknown tool name in `tools/call` |
| `-32602` | Invalid Params | Missing/invalid `name` or `arguments` structure in `tools/call`; argument schema validation failure |
| `-32603` | Internal Error | Gateway disabled (`FRIESE_MCP_ENABLED = False`) |

> **Note on tool errors:** Permission denied, validation errors, and unhandled exceptions inside tool handlers are returned as `isError: true` content blocks (HTTP 200), not JSON-RPC error codes. Only structural call failures (`-32602`) and unknown tool names (`-32601`) use error codes.

---

## Pluggable backend architecture

friese-mcp separates tool *discovery* from tool *invocation* through two abstract base classes. Both are loaded by dotted-path settings at startup.

### `BaseDiscoveryBackend`

```python
from friese_mcp.backends import BaseDiscoveryBackend, ToolDefinition

class MyDiscovery(BaseDiscoveryBackend):
    def discover_tools(self) -> list[ToolDefinition]:
        # Return ToolDefinition instances for each tool to register.
        ...
```

**`discover_tools() -> list[ToolDefinition]`** — required. Return all tools this backend can find.

**`get_input_schema(view_class, action) -> dict`** — optional override. Default returns `{"type": "object"}`. `DRFSyncDiscovery` overrides this to derive schemas from DRF serializers.

### `BaseInvocationBackend`

```python
from friese_mcp.backends import BaseInvocationBackend, ToolDefinition, ToolResult
from django.http import HttpRequest

class MyInvocation(BaseInvocationBackend):
    def invoke(
        self, tool: ToolDefinition, arguments: dict, request: HttpRequest
    ) -> ToolResult:
        # Dispatch the tool and return ToolResult.
        ...
```

**`invoke(tool, arguments, request) -> ToolResult`** — required. Permission enforcement has already been performed by `ToolRegistry.dispatch()` before this method is called.

### `ToolDefinition`

Immutable dataclass produced by discovery backends and consumed by invocation backends.

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Unique MCP tool name (e.g. `"users.list"`). |
| `description` | `str` | Human-readable description. |
| `input_schema` | `dict` | JSON Schema (draft-07). |
| `permission_classes` | `tuple[type[BasePermission], ...]` | DRF permission classes. |
| `source` | `"auto" \| "decorator"` | How this tool was registered. |
| `is_dispatcher` | `bool` | `True` when the tool was registered via `@mcp_dispatcher`. |
| `view_class` | `type \| None` | The ViewSet class (`None` for decorator tools). |
| `action` | `str \| None` | The ViewSet action name (`None` for decorator tools). |

### `ToolResult`

Return value from an invocation backend.

| Field | Type | Description |
|---|---|---|
| `content` | `Any` | JSON-serialisable result value. |
| `is_error` | `bool` | `True` when tool execution failed. Gateway wraps this as `"isError": true`. |

### `SyncInvocation` (default)

Builds a synthetic DRF `Request` from the tool arguments, instantiates the ViewSet, and calls the action method directly in the current thread.

- Works with any standard DRF ViewSet under a synchronous WSGI server (gunicorn, uWSGI).
- The original request's `user` is forwarded to the synthetic inner request so host-app middleware state (JWT payload, tenant scope) remains accessible.
- Constructs the inner `HttpRequest` directly using `django.http.HttpRequest` and `io.BytesIO` — no `django.test` dependency in production code.
- **Not suitable for async ViewSets.** Use a custom `BaseInvocationBackend` pointed at `FRIESE_MCP_INVOCATION_BACKEND` for async or Celery-delegated invocation.

---

## Known limitations and design decisions

### Auth and `tools/list`

Beyond per-agent filtering (see [contrib.agents](#contribagents--per-agent-tool-allowlists)), `tools/list` performs no additional authentication or permission checks. Any caller whose request passes gateway-level auth sees the full tool manifest.

**Rationale:** friese-mcp does not own authentication or authorisation. The host application is responsible for placing auth-gating in front of the MCP endpoint at the infrastructure level — API gateway, reverse proxy, Django middleware, or DRF authentication classes applied to the URL include. Adding per-tool permission filtering inside `tools/list` would pull the package into auth ownership that is explicitly out of v1 scope.

**Recommended pattern:** Protect the entire `/mcp/` URL prefix with authentication middleware or an API gateway rule. All MCP traffic — including `tools/list` — passes through that gate. Use `contrib.agents` when you need per-agent tool visibility scoping beyond that.

### Object-level permissions not enforced

`ToolRegistry.dispatch()` calls `has_permission(request, None)` for each permission class but does not call `has_object_permission()`. In standard DRF, object-level permissions are evaluated after the target object is fetched; friese-mcp's permission check runs *before* ViewSet invocation, at which point no object exists yet.

Host applications using object-level permission classes (e.g. `IsOwnerOrReadOnly`) should be aware that `has_object_permission()` will not be invoked by friese-mcp v1. This gap is documented for v2.

### `tools/call` errors do not expose exception details

When a tool raises an unhandled exception, `tools/call` returns `{"isError": true, "content": [{"type": "text", "text": "{\"error\": \"Internal tool error\"}"}]}`. The raw exception message is intentionally suppressed to prevent leaking internal details (DB column names, file paths, model field names). Full error information is available in the server log.

### No request body size limit

`json.loads(request.body)` has no `Content-Length` guard. Host-app infrastructure (nginx, gunicorn, load balancer) should enforce request body size limits.

### CSRF and session authentication

`McpEndpointView` extends DRF's `APIView`. DRF exempts `APIView` from Django's CSRF middleware by default, so no `@csrf_exempt` decorator is needed. However, if `SessionAuthentication` is included in `FRIESE_MCP_AUTHENTICATION_CLASSES`, DRF will enforce CSRF for session-authenticated requests (standard DRF behaviour). MCP clients should use token authentication (Bearer / API key) rather than session cookies to avoid this complexity.

### Rate limiting

`RateLimitMiddleware` uses an in-process sliding-window counter. In multi-process deployments, each worker maintains its own counter — limits are not shared across workers. For shared rate limiting, use a custom middleware class backed by Redis or a database counter.

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'friese_mcp'` after `pip install -e .` on macOS

> **Scope:** This only affects editable installs (`pip install -e`). A regular `pip install friese-mcp` from PyPI copies files directly into site-packages — no `.pth` file is involved and this bug does not apply.

**Symptom:** `pip install -e ../friese-mcp` completes without error, but `import friese_mcp` raises `ModuleNotFoundError` at runtime.

**Root cause:** Python 3.13 on macOS has a bug where `site.addpackage()` silently skips `.pth` files that carry the `UF_HIDDEN` filesystem flag. Hatchling's editable install writes `__editable__.friese-mcp-0.1.0.pth` to site-packages; if that file gets the hidden flag, Python never processes it and the package is invisible to the interpreter.

This bug affects Homebrew-managed Python 3.13 even after upgrading to 3.13.1 — the upstream fix does not apply to Homebrew builds. `chflags nohidden` also does not reliably stick across reinstalls.

**Recommended fix — `sys.path.insert`**

Add the `src/` directory to `sys.path` before Django loads. This bypasses the `.pth` mechanism entirely and works on all Python 3.13.x builds including Homebrew:

```python
# manage.py (and wsgi.py / asgi.py)
import sys
sys.path.insert(0, "/path/to/friese-mcp/src")

# ... rest of manage.py
```

**Verify the fix:**

```bash
python -c "import friese_mcp; print(friese_mcp.__file__)"
# Expected: /path/to/friese-mcp/src/friese_mcp/__init__.py
```

If you still see `ModuleNotFoundError`, the path in `sys.path.insert` is wrong — check that it points to the `src/` directory that contains the `friese_mcp/` package folder, not the repo root.

**Alternative — pyenv Python 3.11 or 3.12**

Switch to a non-Homebrew Python where editable installs work reliably:

```bash
pyenv install 3.12.9
pyenv local 3.12.9
pip install -e ../friese-mcp
```

### Migrating from a custom MCP implementation — auth model lifecycle

If you are adopting friese-mcp to replace a custom MCP implementation in an existing host app, you may run into a Django model lifecycle problem: your existing MCP auth models (tokens, OAuth clients) live inside the old app. When you want to fully remove the old app from `INSTALLED_APPS`, Django will complain about orphaned migrations — you can't drop the app while its tables still hold data your new setup depends on.

**Symptom:** You want to decommission `myapp.mcp` but `myapp.mcp.models` still contains `MCPToken` or `OAuthClient`. Removing `"myapp.mcp"` from `INSTALLED_APPS` breaks migrations; keeping it means the old app never fully goes away.

**Recommended approach — extract auth models before migrating:**

Before adopting friese-mcp, move your MCP auth models to a small, standalone app that has no dependency on the old MCP tool logic:

```python
# mcp_auth/models.py
# Moved from myapp.mcp — minimal app, no tool logic, just the auth tables

from django.db import models

class MCPToken(models.Model):
    # ... same fields as before
```

```python
# settings.py
INSTALLED_APPS = [
    ...
    "mcp_auth",       # new standalone app — owns the auth tables
    "friese_mcp",     # new MCP gateway
    # "myapp.mcp",    # removed — no longer needed
]
```

Write a Django data migration to copy rows from the old table to the new one, then remove the old app.

**If you use `contrib.tokens` or `contrib.oauth`:** These modules ship their own models (`FrieseMcpToken`, `OAuthClient`, `OAuthAccessToken`) with their own migrations. If you need to preserve existing tokens during migration, write a data migration that copies from your old auth table to the relevant `friese_mcp.contrib.*` table.

**Key principle:** Decouple the model lifecycle from the tool logic before switching. A standalone `mcp_auth` app with no tool dependencies can stay in `INSTALLED_APPS` indefinitely (it's tiny) while you iterate on the MCP gateway, and can be removed in a future cycle once auth is fully migrated.

### Auto-discovery registers 0 tools

If you see a log warning like:

```
WARNING friese_mcp: auto-discovery found 0 tools. If your project uses @api_view FBVs, use @mcp_tool for manual registration.
```

Common causes:
- Your ViewSets are not yet registered in the URL patterns at startup time (e.g. missing `include()` in `ROOT_URLCONF`).
- All ViewSets are decorated with `@mcp_ignore`.
- `FRIESE_MCP_AUTODISCOVER = False` — auto-discovery is disabled. Register tools manually with `@mcp_tool`.
- Your app uses function-based views (`@api_view`) rather than ViewSets — use `@mcp_tool` for those.
