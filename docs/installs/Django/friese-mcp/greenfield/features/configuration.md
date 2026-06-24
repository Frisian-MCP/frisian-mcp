# Feature: @mcp_resource, @mcp_ignore, Permission Tiers & Settings Reference

**Audience:** Developers configuring frisian-mcp in production  
**Package version:** 1.0.x

---

## @mcp_resource

Register a callable as an MCP resource — a named, URI-addressable piece of content that agents can read via `resources/list` and `resources/read`.

### Signature

```python
from frisian_mcp import mcp_resource

@mcp_resource(
    uri_template="config://app/{key}",
    name="App Config",
    description="Read application configuration values.",
    mime_type="application/json",
)
def read_config(uri: str, request: HttpRequest) -> str:
    key = uri.split("/")[-1]
    value = get_config(key)
    return json.dumps({"key": key, "value": value})
```

The decorated function must accept `(uri: str, request: HttpRequest)` and return the resource content as a string.

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `uri_template` | `str` | Yes | — | Resource URI. Supports `{variable}` placeholders (RFC 6570 Level 1). |
| `name` | `str` | Yes | — | Human-readable name shown in `resources/list`. |
| `description` | `str` | No | `""` | Optional description shown in `resources/list`. |
| `mime_type` | `str` | No | `"text/plain"` | MIME type of the returned content. |

### URI template matching

URI templates use Level-1 RFC 6570 placeholders. The match is exact on literal segments; `{variable}` captures one path segment:

```text
config://app/{key}        matches  config://app/debug_mode
documents://reports/{id}  matches  documents://reports/42
```

---

## @mcp_ignore

Exclude a ViewSet class or individual action from auto-discovery. Use when you have ViewSets that should remain private and not be exposed to MCP clients.

### Applied to a class

```python
from frisian_mcp import mcp_ignore

@mcp_ignore
class InternalAuditViewSet(ModelViewSet):
    """This ViewSet is excluded from MCP auto-discovery."""
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
```

### Applied to a method

```python
from frisian_mcp import mcp_ignore

class UserViewSet(ModelViewSet):

    @mcp_ignore
    def set_password(self, request, pk=None):
        """Excluded from MCP — password changes must go through the web UI."""
        ...

    def list(self, request):
        """This action IS discovered and registered."""
        ...
```

`@mcp_ignore` sets `_mcp_ignore = True` on the target. Auto-discovery checks this attribute before registering. It does not affect DRF routing — the action remains available via the REST API.

---

## Permission tiers

frisian-mcp uses three permission tiers to control which tools are visible and callable for a given request.

| Tier | When applied | Visible and callable to |
|------|-------------|------------------------|
| `read` | Default for all tools/dispatchers | All callers, including unauthenticated (if `frisian_MCP_UNAUTHENTICATED_TIER="read"`) |
| `read_write` | `write=True` on `@mcp_tool`, `@mcp_heavy`, or `@mcp_action` | Callers with `read_write` or `admin` tokens |
| `admin` | `admin=True` on the decorator | Callers with `admin` tokens only |

### Unauthenticated callers

The effective tier for an unauthenticated request is controlled by `frisian_MCP_UNAUTHENTICATED_TIER`:

```python
# settings.py

# Allow unauthenticated callers to see and call read-tier tools.
frisian_MCP_UNAUTHENTICATED_TIER = "read"

# Require authentication for all tools.
frisian_MCP_UNAUTHENTICATED_TIER = "none"
```

Setting this to `"none"` makes `tools/list` return an empty array for unauthenticated callers, effectively hiding the entire tool surface.

### Token tiers (contrib.tokens)

When `frisian_mcp.contrib.tokens` is installed, tokens carry a `permission` attribute that maps to a tier. Manage tokens in the Django admin under **frisian MCP → Tokens**.

### Gateway-level auth

Use `frisian_MCP_AUTHENTICATION_CLASSES` and `frisian_MCP_PERMISSION_CLASSES` to gate the entire MCP surface:

```python
# settings.py

# Require JWT authentication at the gateway level
frisian_MCP_AUTHENTICATION_CLASSES = [
    "rest_framework_simplejwt.authentication.JWTAuthentication",
]
frisian_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

When absent, `frisian_MCP_AUTHENTICATION_CLASSES` falls back to DRF's `DEFAULT_AUTHENTICATION_CLASSES`. `frisian_MCP_PERMISSION_CLASSES` defaults to `[]` (no gateway-level permission check) so that tool-level permissions handle access control.

---

## Settings Reference

All settings are optional. Defaults are shown.

### Core

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_ENABLED` | `True` | Set `False` to disable the MCP gateway entirely (returns 503 to all requests). |
| `frisian_MCP_AUTODISCOVER` | `True` | Set `False` to disable DRF ViewSet auto-discovery. Use when registering all tools manually with `@mcp_tool`. |
| `frisian_MCP_SERVER_NAME` | `"frisian-mcp"` | Server name returned in the `initialize` handshake `serverInfo.name` field. |
| `frisian_MCP_SESSION_ID_HEADER` | `True` | When `True`, adds `Mcp-Session-Id` header to `initialize` responses. |
| `frisian_MCP_EXPOSE_ERRORS` | `settings.DEBUG` | When `True`, unhandled tool exceptions return the exception message to the agent. Set `False` in production. |

### Authentication & permissions

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_AUTHENTICATION_CLASSES` | DRF default | List of dotted-path strings or class objects. Gateway-level authentication. |
| `frisian_MCP_PERMISSION_CLASSES` | `[]` | List of dotted-path strings or class objects. Gateway-level permission check. |
| `frisian_MCP_UNAUTHENTICATED_TIER` | `"read"` | Permission tier for unauthenticated requests. Set `"none"` to require auth for all tools. |

### Tool filtering

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_TOOL_ALLOWLIST` | `None` | List of exact tool names to allow. All other discovered tools are dropped. Applied before denylist. |
| `frisian_MCP_TOOL_DENYLIST` | `None` | List of exact tool names to suppress. Applied after allowlist. |

Example — expose only specific tools:

```python
# settings.py

frisian_MCP_TOOL_ALLOWLIST = [
    "orders.list",
    "orders.retrieve",
    "products.list",
]
```

### `tools/list` performance

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_TOOLS_LIST_CACHE_TTL` | `None` | Integer seconds. When set, caches `tools/list` responses per permission tier. Set `None` to disable caching. |
| `frisian_MCP_TOOLS_PAGE_SIZE` | `None` | Integer. When set, paginates `tools/list` responses using an opaque base64url cursor. Clients advance pages via `nextCursor`. |

### Large responses

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_HEAVY_PAGE_SIZE` | `20` | Default page size for `@mcp_heavy` tools in `paginated` mode. |
| `frisian_MCP_AUTO_NEGOTIATE_THRESHOLD` | `None` | Integer byte count. When set, auto-wraps any tool response exceeding this size in a probe envelope, even tools not decorated with `@mcp_heavy`. Secondary backstop — prefer `@mcp_heavy` for explicit control. |

### Middleware

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_TOOL_MIDDLEWARE` | `[]` | List of dotted-path strings for MCP tool middleware classes. Middleware receives `(request, tool_name, arguments, call_next)` and must return the result. Applied in declaration order (first = outermost). |

Example middleware:

```python
# myapp/mcp_middleware.py

import logging
import time

logger = logging.getLogger(__name__)

class TimingMiddleware:
    def __call__(self, request, tool_name, arguments, call_next):
        start = time.monotonic()
        result = call_next(request, tool_name, arguments)
        elapsed = time.monotonic() - start
        logger.info("tool=%s elapsed=%.3fs", tool_name, elapsed)
        return result
```

```python
# settings.py

frisian_MCP_TOOL_MIDDLEWARE = [
    "myapp.mcp_middleware.TimingMiddleware",
]
```

### Advanced discovery

| Setting | Default | Description |
|---------|---------|-------------|
| `frisian_MCP_DISCOVERY_BACKEND` | DRF sync discovery | Dotted-path string. Override the ViewSet discovery backend. |
| `frisian_MCP_INVOCATION_BACKEND` | Sync invocation | Dotted-path string. Override the tool invocation backend. |

---

## See also

- `install.md` — greenfield install and URL wiring
- `features/mcp-tool.md` — manual tool registration
- `features/dispatcher.md` — dispatcher pattern
- `features/mcp-heavy.md` — large-response negotiation
- `features/write-path.md` — write-path response filtering
