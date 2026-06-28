# Getting Started

## What You're Looking At

This server is running frisian-mcp — a Django package that turns any Django REST Framework application into an MCP server. The demo surfaces here are live. The Nautobot and Netbox integrations documented in the reference section are real systems that were built and validated by agents connecting to those servers directly via MCP.

If you're an agent reading this: you are already using frisian-mcp. The tool you called to retrieve this document is a dispatcher — a single MCP tool that groups related operations rather than exposing them individually. That is the pattern this package implements.

---

## The Short Version

```bash
pip install frisian-mcp
```

frisian-mcp installs cleanly under `uv` (Astral) too:

```bash
uv add frisian-mcp                 # add to a uv-managed project
uv pip install frisian-mcp         # ad-hoc, system Python
uv sync --frozen                   # lockfile-based reproducible install
```

For `uv` inside Docker, the canonical pattern is to install `uv` in the build stage and use `uv pip install --system` so the runtime keeps using system Python (no venv path surgery):

```dockerfile
FROM python:3.12-slim
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
COPY pyproject.toml uv.lock /app/
WORKDIR /app
RUN uv pip install --system frisian-mcp
```

In `settings.py`:

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp',
]

FRISIAN_MCP_AUTODISCOVER = True  # expose all DRF ViewSets automatically
```

In `urls.py`:

```python
from frisian_mcp.views import McpView

urlpatterns = [
    path('mcp/', McpView.as_view()),
    ...
]
```

Connect your MCP client to `https://your-domain.com/mcp/`. That's the full install for a brownfield app with existing ViewSets.

> **Verify the install before connecting any client.** Run `python manage.py mcp_doctor` after the first deploy and after every config change. It walks eight checks (INSTALLED_APPS, URL mounting, auth wiring, security settings, cache backend, performance hints, OAuth posture, authorize URL reachability) and exits non-zero on errors — ideal as a CI gate. `--security` adds an extended OAuth audit. See [Guide → mcp_doctor](../Guide/mcp-doctor.md).

---

## What Happens on Install

When `FRISIAN_MCP_AUTODISCOVER = True`, frisian-mcp walks your DRF router on startup and registers each ViewSet as an MCP tool. It reads your existing OpenAPI schema — no separate schema definition required.

An agent connecting to your endpoint calls `tools/list` and receives the registered tools. frisian-mcp's dispatcher pattern means the agent sees a small, stable tool list rather than every individual API operation as a separate tool.

For a Django app with 50 ViewSet actions, the difference looks like this:

| Approach | Tools exposed to agent | Schema tokens at connect |
|---|---|---|
| Flat MCP (one tool per action) | 50 | ~15,000–25,000 |
| frisian-mcp dispatcher | 3–8 | ~500–2,000 |

The agent still has access to all 50 operations — it discovers them progressively via `action=help` on each dispatcher as needed.

> **The dispatcher reduction is opt-in, not automatic.** The dispatcher row reflects what you get with [`FRISIAN_MCP_DISPATCH_GROUPS`](../Reference/installation-configuration-reference.md#frisian_mcp_dispatch_groups) configured. With autodiscovery alone (no `FRISIAN_MCP_DISPATCH_GROUPS`), the package early-returns out of dispatcher installation (`src/frisian_mcp/apps.py:573-577`) and the agent sees one flat tool per ViewSet action — you get the Flat MCP row, not the dispatcher row. Plan your group configuration as part of installation, not after.

---

## Auto-Discovery vs. Explicit Registration

### Auto-Discovery

The default mode. Set `FRISIAN_MCP_AUTODISCOVER = True` and all ViewSets are registered automatically. Use `@mcp_ignore` to exclude specific ViewSets or methods you don't want on the MCP surface.

```python
from frisian_mcp.decorators import mcp_ignore

@mcp_ignore
class InternalAdminViewSet(viewsets.ModelViewSet):
    # This ViewSet will not appear in MCP tool listings
    ...
```

Auto-discovery silently registers zero tools under four conditions worth knowing:

- ViewSets not yet resolved at discovery time (check your router registration order)
- All ViewSets decorated with `@mcp_ignore`
- `FRISIAN_MCP_AUTODISCOVER = False`
- Function-based views used instead of ViewSets (auto-discovery only reads ViewSets)

### Explicit Registration

For full control over tool names, schemas, and permission tiers:

```python
from frisian_mcp.decorators import mcp_dispatcher, mcp_action

@mcp_dispatcher(name='orders')
class OrdersDispatcher:

    @mcp_action(description='List orders for the authenticated user')
    def list(self, request, params):
        ...

    @mcp_action(description='Create a new order', write=True)
    def create(self, request, params):
        ...
```

Explicit registration is the right choice when you want to design tool names and descriptions for agent interaction rather than exposing raw API structure.

---

## Authentication

### Static API Key (Simplest)

For development or internal tools, set `FRISIAN_MCP_API_KEYS` in settings:

```python
FRISIAN_MCP_API_KEYS = {
    'your-agent-key': 'read_write',
    'your-readonly-key': 'read',
}
```

Agents include the key as a Bearer token. No additional configuration required.

### Token Auth (contrib.tokens)

For per-agent tokens with database-backed revocation:

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp',
    'frisian_mcp.contrib.tokens',
]
```

Generates `FrisianMcpToken` model with Django admin management. Agents authenticate via `Authorization: Bearer <token>`.

### OAuth 2.0 (contrib.oauth)

For connecting Claude, GPT, and other AI agent clients that expect OAuth:

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp',
    'frisian_mcp.contrib.oauth',
]

FRISIAN_MCP_OAUTH_ISSUER = 'https://your-domain.com'
```

Implements the full OAuth 2.0 client_credentials grant with RFC 8414 well-known metadata and RFC 7591 dynamic client registration. Claude and GPT connect without special handling once configured.

### Using tokens AND OAuth together

When you install both `contrib.tokens` and `contrib.oauth`, configure `FRISIAN_MCP_AUTHENTICATION_CLASSES` to declare both authenticators. **Always list the token classes BEFORE the OAuth class** when both are present:

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",  # optional
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

**Why tokens-first matters.** The first authenticator in the chain emits the `WWW-Authenticate` challenge on 401 responses. Tokens-first emits a bare `Bearer` challenge, which static-token MCP clients (Claude Code, Codex, Gemini CLI) accept and use to send their configured Bearer in the next request. OAuth-first emits `Bearer realm="...", resource_metadata="..."`, which discovery-first clients interpret as a directive to probe `.well-known/` and run the OAuth discovery cascade — fine if every client is an OAuth client, but a footgun the moment you add a static-token coding agent (which is when most teams hit this for the first time, the hard way). As of frisian-mcp 1.0.11 both classes return `None` on lookup-miss so the order is no longer load-bearing for *correctness*, but it remains load-bearing for the challenge-shape reason above. **Put tokens before OAuth.**

---

## Permission Tiers

frisian-mcp uses three tiers to gate tool visibility and execution:

| Tier | Who sees it | How to set |
|---|---|---|
| `read` | Unauthenticated callers | Default for all auto-discovered ViewSets |
| `read_write` | Authenticated callers | `write=True` on `@mcp_action` |
| `admin` | Admin users only | `admin=True` on `@mcp_action` |

The permission tier system is silent — an agent never receives a "permission denied" error for a tool it can't see. Tools above the caller's tier simply don't appear in `tools/list`. This prevents agents from burning context on retry loops against operations they'll never be able to call.

---

## The @mcp_heavy Decorator

For tools that return large result sets, `@mcp_heavy` registers an explicit MCP tool that enforces a probe-then-fetch protocol — the first call returns a preview + continuation token; the second call returns the full data, a summary, a page, or a filtered subset depending on the requested mode.

`@mcp_heavy` is an **explicit tool registration** decorator (sibling of `@mcp_tool`, `@mcp_dispatcher`, `@mcp_action`). It requires `name`, `description`, and `input_schema` arguments, and the decorated callable must have a `(arguments, request)` signature — it is **not** a bare wrapper for a DRF `ModelViewSet` method:

```python
from frisian_mcp.decorators import mcp_heavy

@mcp_heavy(
    name="devices.search",
    description="Search devices and return a probe envelope with pagination metadata.",
    input_schema={
        "type": "object",
        "properties": {
            "site": {"type": "string"},
            "role": {"type": "string"},
        },
    },
)
def search_devices(arguments, request):
    qs = Device.objects.filter(**arguments)
    return DeviceSerializer(qs, many=True).data
```

**For auto-discovered ViewSets** — which is what the rest of this guide is about — you usually don't need to decorate anything. Set `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` instead: any auto-discovered tool whose response exceeds the byte threshold is auto-wrapped in the same probe envelope, with no per-ViewSet code change required:

```python
# settings.py
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 50_000  # bytes
```

An agent calling a heavy tool — explicit or threshold-wrapped — receives the preview, total size, available modes, and a continuation token. The agent decides whether to paginate, filter, or pull the full payload. The context window is not pre-filled with records the agent may not need.

Measured impact on a 65-device Nautobot instance: 23% token reduction on the list call. At 500 devices: 90% reduction. At 2,000 devices: 97% reduction. At production scale, an un-paginated device list would exhaust the context window before the agent could do any actual work.

---

## Write-Path Token Efficiency (@mcp_light)

Write operations — create, update, delete — return a lean confirmation envelope by default rather than echoing the full serialized object back. No decorator or configuration is required; this is the package-level default for all write tools.

**Single-object create or update:**

```json
{
  "id": "abc123",
  "url": "https://example.com/api/device/abc123/",
  "name": "edge-01",
  "status_code": 201,
  "data_size": 3840,
  "continuation_token": "<token>"
}
```

**Bulk create or update:**

```json
{
  "accepted": 60,
  "failed": 0,
  "status_code": 201,
  "data_size": 43190,
  "continuation_token": "<token>"
}
```

For a 60-device bulk create, the lean envelope is approximately 24 tokens (95 bytes) versus ~10,798 tokens for the full echo — a 99.8% reduction. The saving scales with bulk size; the lean envelope is a fixed-size structure.

**When you need the full object:** pass `verify=True` on the write call to receive the complete serialized response inline. Or use the `continuation_token` to retrieve it via the heavy-fetch path — the write is not re-executed.

**Custom fields in the lean envelope:** annotate specific serializer fields with `mcp_light_key` in the serializer's `Meta` to ensure they appear in every lean envelope for that serializer.

See [Write-Path Response Filtering](../Guide/write-path-response-filtering.md) for the complete guide including `Meta.mcp_light_key` usage (it's a serializer Meta attribute, not a decorator — despite the `@mcp_*` family naming) and the continuation token retrieval pattern.

---

## Connecting MCP Clients

### Claude.ai

Settings → Integrations → Add MCP Server → enter your endpoint URL. Claude will call `tools/list` and discover your tools on the next message.

### Claude Code

```json
{
  "mcpServers": {
    "your-app": {
      "url": "https://your-domain.com/mcp/",
      "headers": {
        "Authorization": "Bearer your-api-key"
      }
    }
  }
}
```

### Cursor, Windsurf, and other coding agents

Same pattern — endpoint URL plus auth header. The `mcp_config` management command (v1 candidate) will output ready-to-paste `mcpServers` JSON for the most common clients.

---

## Per-Identity Tool Surface Filtering (Optional)

By default, every caller of a given permission tier sees the same tool surface. `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` filters `tools/list` so each caller sees only the tools their specific identity is permitted to use. An agent whose account holds only DNS read permissions receives a `tools/list` containing only DNS read tools — tools for other systems do not appear.

```python
# settings.py
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
```

This is an optional feature — default off, no migration impact. It is useful when you want to give different agents scoped views of your API without exposing the full surface to every caller.

See [Permission-Aware Discovery](../Guide/permission-aware-discovery.md) for configuration, adapter options, and startup checks, and [Permission-Aware Discovery — Security Guidance](../Guide/permission-aware-discovery-security.md) for production deployment requirements.

---

## What to Explore Next

- **Installation & Configuration Reference** — complete settings reference, decorator documentation, and auth module setup
- **The Token Problem at MCP Scale** — the quantitative case for the dispatcher pattern
- **Security-First MCP Architecture** — recommended deployment patterns for production MCP surfaces
- **Integration walkthroughs** — real build sessions against Nautobot and NetBox, including token efficiency measurements and agent behavior at scale
- **Test Cases** — Real integration sessions across network automation, document management, and other production systems.  View the **API Validation** for raw comparisons.
- **Changelog** — full version history from v0.1.0 to v1.0.12

---
