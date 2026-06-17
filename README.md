# frisian-mcp

**The Django MCP gateway that discovers your API automatically.**

frisian-mcp turns your existing Django REST Framework ViewSets into [Model Context Protocol](https://spec.modelcontextprotocol.io/) tools with zero boilerplate. Add the package, include one URL, and every ViewSet action becomes a callable MCP tool — name, description, and input schema derived from your serializers automatically.

**Designed for token-efficient agent workflows.** A 50-action Django app loads in 500–2,000 tokens of `tools/list` schema instead of the 15,000–25,000 conventional flat MCP would emit; a 60-device bulk-write response is 24 tokens instead of ~10,800 of full echo. Same surface, two orders of magnitude less context burned before the agent has done any reasoning. Full numbers in [Token efficiency](#token-efficiency).

**Version:** 1.0.11 | **License:** Apache 2.0 | **Python:** 3.11+ | **Django:** 5.x

```bash
pip install frisian-mcp
```

**Project site:** <https://frisian-mcp.com/>

A live MCP server is hosted at `https://frisian-mcp.com/` for hands-on evaluation — point any MCP-compatible client at it to see the dispatcher pattern and lean envelope behavior against a real surface. The same site serves the project documentation through an MCP-consumable dispatcher, so coding agents (Claude Code, Codex, Gemini CLI, etc.) can connect to it directly and consume installation, configuration, and decorator reference material as part of their working context while integrating frisian-mcp into your own project.

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

## Token efficiency

The dispatcher pattern and the lean write envelope exist for one reason: agent context windows are finite, and the conventional MCP shape (one tool per ViewSet action; full serialized echo on every write) burns through that budget before the agent has done anything useful.

Measured numbers from real integrations:

| Scenario | Default MCP shape | frisian-mcp | Reduction |
|---|---|---|---|
| `tools/list` for a 50-action Django app | ~15,000–25,000 tokens | 500–2,000 tokens with `FRISIAN_MCP_DISPATCH_GROUPS` | ~95% |
| `tools/list` for a Nautobot 3.x deployment | 1,737 flat tools | 15 dispatcher tools | tool surface reduced ~99% |
| 60-device bulk-create response | ~10,798 tokens (full echo) | 24 tokens (lean envelope) | 99.8% |
| 200-device bulk-create response | ~36,000 tokens | 24 tokens (constant) | 99.9% |

The bulk-write savings are constant regardless of batch size — the lean envelope is fixed-shape and the full response is reachable via the continuation token without re-running the write. The dispatcher reduction is opt-in through `FRISIAN_MCP_DISPATCH_GROUPS` (autodiscovery alone gives the conventional flat shape).

See [docs/Guide/the-token-problem.md](docs/Guide/the-token-problem.md), [docs/Guide/dispatcher-pattern.md](docs/Guide/dispatcher-pattern.md), and [docs/Guide/write-path-response-filtering.md](docs/Guide/write-path-response-filtering.md) for the design rationale and full measurements.

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

## Authentication and security

frisian-mcp delegates authentication to DRF — any DRF authentication class works out of the box via `FRISIAN_MCP_AUTHENTICATION_CLASSES`. Three ready-to-use contrib modules cover the most common cases:

| Module | What it provides |
|---|---|
| `frisian_mcp.contrib.tokens` | HMAC-hashed static Bearer tokens for internal agents and service accounts |
| `frisian_mcp.contrib.oauth` | Full OAuth 2.0 — authorization code (PKCE) + client credentials; redirect URI allowlist |
| `frisian_mcp.contrib.agents` | Per-credential tool allowlists; connections fail-closed when the credential is deactivated |

Gateway-level access is controlled by `FRISIAN_MCP_PERMISSION_CLASSES`. Tool-level access is controlled by permission tiers (`read` / `write` / `admin`) mapped via `FRISIAN_MCP_TOKEN_TIER_MAP`. Use `FRISIAN_MCP_MAX_TIER` to cap all callers on an endpoint regardless of their credential tier.

### Hardened-by-default posture (1.0.x)

The defaults are oriented toward production safety rather than walk-up convenience:

- **Token and client-secret storage uses HMAC-SHA256 digests** (`FRISIAN_MCP_HMAC_KEY`). A leaked database row cannot be replayed directly — the raw secret is only ever shown once at creation time.
- **OAuth dynamic client registration is closed by default** (`FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False`, `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=False`, `FRISIAN_MCP_OAUTH_AUTO_APPROVE=False`). The operator pre-registers every OAuth client via the Django admin; anonymous walk-up registration is not possible without an explicit opt-in.
- **The PKCE default permission tier is `read`.** Mis-configurations cannot accidentally hand out write or admin scopes on first connect.
- **Permission-aware discovery** (`FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY=True`) rebuilds dispatcher action enums per-request — a read-tier token sees only `list` / `retrieve` actions, write and admin actions are hidden from `tools/list` rather than just blocked at execution.
- **`.well-known` discovery metadata is gated** by `FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY`. With it set to `False`, the OAuth metadata endpoints return parseable JSON 404s so discovery-first MCP clients fall back to their configured Bearer instead of being routed into a dead-end OAuth cascade.
- **Authenticator chain ordering is no longer load-bearing** for correctness — both `FrisianMcpTokenAuthentication` and `OAuthTokenAuthentication` return `None` on lookup-miss so either order works. Tokens-first is the recommended convention for the WWW-Authenticate challenge shape (see [docs/Getting Started/getting-started.md](docs/Getting%20Started/getting-started.md#using-tokens-and-oauth-together)).
- **SSE keepalive structure is documented**, with a one-time runtime warning when the package detects it is running under a synchronous WSGI worker (which cannot scale SSE without starving the worker pool). The recommended deployment is an ASGI worker class (`uvicorn.workers.UvicornWorker` or `uvicorn` directly).

See [docs/Security/security.md](docs/Security/security.md) for the full threat model and recommended deployment patterns, and [docs/Reference/installation-configuration-reference.md](docs/Reference/installation-configuration-reference.md) for the complete settings reference.

---

## Full documentation

Full settings reference, auth configuration, decorator API, middleware, pluggable backends, security guide, and troubleshooting are in [`docs/`](docs/).

For browsable docs, a live MCP server, and the agent-consumable docs dispatcher (point your coding agent at it directly), visit <https://frisian-mcp.com/>.
