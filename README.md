# frisian-mcp

**The Django MCP gateway that discovers your API automatically.**

frisian-mcp turns your existing Django REST Framework ViewSets into [Model Context Protocol](https://spec.modelcontextprotocol.io/) tools with zero boilerplate. Add the package, include one URL, and every ViewSet action becomes a callable MCP tool — name, description, and input schema derived from your serializers automatically.

**Version:** 1.0.11 | **License:** Apache 2.0 | **Python:** 3.11+ | **Django:** 5.x

```bash
pip install frisian-mcp
```

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
| **Allowlist / denylist** | `FRISIAN_MCP_TOOL_ALLOWLIST` / `FRISIAN_MCP_TOOL_DENYLIST` for surgical surface control |
| **Dispatch groups** | `FRISIAN_MCP_DISPATCH_GROUPS` — bundle N tools into 1 dispatcher; `action="help"` for discovery |
| **Deferred discovery** | URL scan fires on first request — captures late-loading plugin ViewSets |
| **OAuth 2.0** | `contrib.oauth` — authorization code (PKCE) + client credentials; HMAC-hashed tokens |
| **Static tokens** | `contrib.tokens` — HMAC-hashed Bearer tokens for internal agents |
| **Per-agent scoping** | `contrib.agents` — per-credential tool allowlists; fail-closed on inactive connections |
| **Permission tiers** | `FRISIAN_MCP_TOKEN_TIER_MAP` / `FRISIAN_MCP_MAX_TIER` — map roles to read/write gates |
| **Host-app scoping** | `SyncInvocation` calls `viewset.initial()` — host RBAC, queryset filtering, and throttles enforced |
| **Tool middleware** | `FRISIAN_MCP_TOOL_MIDDLEWARE` — audit logging, rate limiting, heartbeats |
| **Rate limiting** | `RateLimitMiddleware` — built-in sliding-window, no Redis required |
| **Pluggable backends** | Custom discovery and invocation backends via dotted-path settings |
| **SSE support** | `Accept: text/event-stream` wraps any response in a single SSE event |
| **MCP `2025-03-26`** | Streamable HTTP; `ping`, `initialize`, `tools/list`, `tools/call`, `resources/list` |

---

## Requirements

- Python 3.11+
- Django 5.x
- Django REST Framework 3.14+

---

## Quickstart

**1. Install and add to `INSTALLED_APPS`:**

```python
# settings.py
INSTALLED_APPS = [
    ...
    "frisian_mcp",
]
```

**2. Include the gateway URL:**

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    ...
    path("mcp/", include("frisian_mcp.urls")),
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

After startup, the gateway exposes `users.list`, `users.retrieve`, `users.create`, `users.update`, `users.partial_update`, and `users.destroy` — derived entirely from the ViewSet.

**3. Generate a client config:**

```bash
python manage.py mcp_config --token mytoken123
```

```json
{
  "mcpServers": {
    "frisian-mcp": {
      "url": "http://localhost:8000/mcp/",
      "transport": "http",
      "headers": { "Authorization": "Bearer mytoken123" }
    }
  }
}
```

Use `--client` to emit the format expected by a specific MCP client. Use `--url` and `--name` to override the server URL and key.

---

## Architecture overview

```
MCP Client
       │  JSON-RPC 2.0 over HTTP POST
       ▼
┌──────────────────────────────────────────────────┐
│  McpView  (DRF APIView)                           │
│  ├─ Authentication  (FRISIAN_MCP_AUTHENTICATION_CLASSES) │
│  ├─ Permissions     (FRISIAN_MCP_PERMISSION_CLASSES)     │
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

- **Separation of discovery and invocation.** Two pluggable backends — override either independently.
- **Registry is the source of truth.** `@mcp_tool`, `@mcp_dispatcher`, and auto-discovery all write to the same `tool_registry` singleton.
- **Tool errors are `isError: true`, not JSON-RPC errors.** Permission denials and handler exceptions return `isError: true` inside a normal HTTP 200 response — the session stays alive.
- **Two enforcement points.** Gateway-level permissions gate the entire `/mcp/` surface. Tool-level tiers gate individual `tools/call` invocations.

---

## Dispatcher pattern

For teams building purpose-built agent tools, frisian-mcp ships the **`@mcp_dispatcher`** pattern: one MCP tool name routes to many actions internally.

```python
from frisian_mcp import mcp_dispatcher, mcp_action

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

One tool in `tools/list` instead of many. Call with `action="help"` for a structured listing of available actions. Per-action JSON Schema validation runs before the method.

This is the pattern for agent-facing APIs where tool count matters and progressive disclosure beats a flat list.

For high-volume APIs, `FRISIAN_MCP_DISPATCH_GROUPS` can automatically bundle existing auto-discovered tools into dispatchers with no extra code.

---

## Authentication

frisian-mcp delegates authentication to DRF — any DRF authentication class works out of the box via `FRISIAN_MCP_AUTHENTICATION_CLASSES`. Three ready-to-use contrib modules cover the most common cases:

| Module | What it provides |
|---|---|
| `frisian_mcp.contrib.tokens` | HMAC-hashed static Bearer tokens for internal agents and service accounts |
| `frisian_mcp.contrib.oauth` | Full OAuth 2.0 — authorization code (PKCE) + client credentials; redirect URI allowlist |
| `frisian_mcp.contrib.agents` | Per-credential tool allowlists; connections fail-closed when the credential is deactivated |

Gateway-level access is controlled by `FRISIAN_MCP_PERMISSION_CLASSES`. Tool-level access is controlled by permission tiers (`read` / `write` / `admin`) mapped via `FRISIAN_MCP_TOKEN_TIER_MAP`. Use `FRISIAN_MCP_MAX_TIER` to cap all callers on an endpoint regardless of their credential tier.

---

## Full documentation

Full settings reference, auth configuration, decorator API, middleware, pluggable backends, security guide, and troubleshooting are in [`docs/`](docs/).
