# friese-mcp

Django MCP gateway with runtime introspection and permission-aware tool scoping.

friese-mcp exposes your existing Django REST Framework ViewSets as [Model Context Protocol](https://spec.modelcontextprotocol.io/) tools over a single JSON-RPC 2.0 HTTP endpoint. Zero boilerplate for standard CRUD resources; explicit overrides where you need them.

**Version:** 0.1.0 | **License:** Apache 2.0 | **Owner:** TriFriese LLC

---

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Settings reference](#settings-reference)
- [Auto-discovery](#auto-discovery)
- [Decorators](#decorators)
- [ToolRegistry API](#toolregistry-api)
- [MCP gateway endpoint](#mcp-gateway-endpoint)
- [Pluggable backend architecture](#pluggable-backend-architecture)
- [Known limitations and design decisions](#known-limitations-and-design-decisions)

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

## Quickstart

With auto-discovery enabled (the default), friese-mcp scans your URL patterns at startup and registers every DRF ViewSet action as an MCP tool. No additional code required.

```python
# myapp/views.py
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]

class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
```

After startup, the following tools are registered automatically:

| Tool name | Description |
|---|---|
| `users.list` | List User objects |
| `users.retrieve` | Retrieve a User object by ID |
| `users.create` | Create a new User object |
| `users.update` | Replace a User object by ID |
| `users.partial_update` | Partially update a User object by ID |
| `users.destroy` | Delete a User object by ID |

Send a `tools/list` request to inspect the live tool manifest:

```
POST /mcp/
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
```

Call a tool:

```
POST /mcp/
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "users.list",
    "arguments": {}
  }
}
```

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

### `FRIESE_MCP_DISCOVERY_BACKEND`

**Type:** `str` (dotted Python import path) | **Default:** `"friese_mcp.backends.discovery.DRFSyncDiscovery"`

The discovery backend class loaded at startup. Override to use a custom scanner (e.g. for Nautobot's app registry, or async ViewSets).

```python
FRIESE_MCP_DISCOVERY_BACKEND = "myapp.backends.NautobotDiscovery"
```

The referenced class must subclass `friese_mcp.backends.BaseDiscoveryBackend`.

### `FRIESE_MCP_INVOCATION_BACKEND`

**Type:** `str` (dotted Python import path) | **Default:** `"friese_mcp.backends.invocation.SyncInvocation"`

The invocation backend class used to dispatch `tools/call` requests. Override for async execution, Celery delegation, or tenant-scoped contexts.

```python
FRIESE_MCP_INVOCATION_BACKEND = "myapp.backends.AsyncInvocation"
```

The referenced class must subclass `friese_mcp.backends.BaseInvocationBackend`.

### `FRIESE_MCP_SERVER_NAME`

**Type:** `str` | **Default:** `"friese-mcp"`

The `serverInfo.name` field returned in the `initialize` handshake response.

```python
FRIESE_MCP_SERVER_NAME = "my-product-mcp"
```

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

### Tool naming

Tool names follow the pattern `{resource}.{action}`, where:

- **resource** — the last non-empty literal segment of the URL path, with hyphens converted to underscores and URL parameter placeholders (`<pk>`, `(?P<pk>...)`) stripped. Examples: `/api/v1/users/` → `users`, `/api/orders/<pk>/` → `orders`.
- **action** — the DRF ViewSet action name: `list`, `retrieve`, `create`, `update`, `partial_update`, `destroy`, or any custom action name.

> **Note:** The resource name is derived from the URL path, not the ViewSet class name. A custom action at `/api/users/export/` produces the tool name `export.export` (last path segment), not `users.export`. Register such tools explicitly with `@mcp_tool` if you need a cleaner name.

### Input schema derivation

`DRFSyncDiscovery.get_input_schema()` builds a JSON Schema (draft-07) for each action:

- **Detail actions** (`retrieve`, `update`, `partial_update`, `destroy`): always includes an `"id"` property of type `integer`. `id` is required for all detail actions except `partial_update`.
- **Write actions** (`create`, `update`, `partial_update`): instantiates the ViewSet's serializer via `get_serializer_class()` and maps each non-read-only field to a JSON Schema type. Required serializer fields become required schema properties.
- **Read actions** (`list`, `retrieve`): no body schema; arguments are passed as query parameters.
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
**Protocol:** JSON-RPC 2.0 over HTTP POST
**Content-Type:** `application/json`
**CSRF:** exempt (machine-to-machine endpoint)

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
    "protocolVersion": "2024-11-05",
    "clientInfo": {"name": "my-client", "version": "1.0"}
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "serverInfo": {"name": "friese-mcp", "version": "0.1.0"},
    "capabilities": {"tools": {}, "resources": {}}
  }
}
```

#### `initialized`

Client confirmation notification. Send after `initialize`. Returns an empty result.

#### `tools/list`

Enumerate all registered MCP tools. See [Auth and tools/list](#auth-and-toolslist) for the auth model.

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

Returns an empty list in v1. Resources are not implemented.

#### `resources/read`

Returns `METHOD_NOT_FOUND` in v1.

### HTTP-level behaviour

| Condition | HTTP status | JSON-RPC error code |
|---|---|---|
| Non-POST request | 405 | `-32600` (Invalid Request) |
| `FRIESE_MCP_ENABLED = False` | 503 | `-32603` (Internal Error) |
| All other responses | 200 | See error codes below |

### JSON-RPC error codes

| Code | Name | When |
|---|---|---|
| `-32700` | Parse error | Request body is not valid JSON |
| `-32600` | Invalid Request | Missing/wrong `jsonrpc` field, `method` is not a string, or non-POST HTTP method |
| `-32601` | Method Not Found | Unrecognised method name, or `resources/read` in v1 |
| `-32602` | Invalid Params | Missing `name` in `tools/call`, `arguments` not an object, unknown tool name, permission denied, or input schema validation failure |
| `-32603` | Internal Error | Gateway disabled (`FRIESE_MCP_ENABLED = False`) |

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
- Uses `rest_framework.test.APIRequestFactory` to construct the inner request. This is a production use of a test utility — it is the only clean way to build a synthetic DRF request without a live server, and is intentional.
- **Not suitable for async ViewSets.** Use a custom `BaseInvocationBackend` pointed at `FRIESE_MCP_INVOCATION_BACKEND` for async or Celery-delegated invocation.

### `RequestContext`

Optional carrier for application-specific context. Available for use in custom backends.

| Field | Type | Description |
|---|---|---|
| `request` | `HttpRequest` | The raw Django HTTP request from the MCP gateway. |
| `user` | `AbstractBaseUser \| AnonymousUser` | The authenticated user. |
| `tenant` | `Any` | Optional tenant object (e.g. for multi-tenant apps). |
| `extras` | `dict` | Arbitrary key/value context from host-app middleware. |

---

## Known limitations and design decisions

### Auth and `tools/list`

`tools/list` returns the full tool manifest (names, descriptions, input schemas) to any caller without performing authentication or permission checks. This is intentional.

**Rationale:** friese-mcp does not own authentication or authorisation. The host application is responsible for placing auth-gating in front of the MCP endpoint at the infrastructure level — API gateway, reverse proxy, Django middleware, or DRF authentication classes applied to the URL include. Adding permission filtering inside `tools/list` would pull the package into auth ownership that is explicitly out of v1 scope.

**Recommended pattern:** Protect the entire `/mcp/` URL prefix with authentication middleware or an API gateway rule. All MCP traffic — including `tools/list` — passes through that gate.

### Object-level permissions not enforced

`ToolRegistry.dispatch()` calls `has_permission(request, None)` for each permission class but does not call `has_object_permission()`. In standard DRF, object-level permissions are evaluated after the target object is fetched; friese-mcp's permission check runs *before* ViewSet invocation, at which point no object exists yet.

Host applications using object-level permission classes (e.g. `IsOwnerOrReadOnly`) should be aware that `has_object_permission()` will not be invoked by friese-mcp v1. This gap is documented for v2.

### `tools/call` errors do not expose exception details

When a tool raises an unhandled exception, `tools/call` returns `{"isError": true, "content": [{"type": "text", "text": "{\"error\": \"Internal tool error\"}"}]}`. The raw exception message is intentionally suppressed to prevent leaking internal details (DB column names, file paths, model field names). Full error information is available in the server log.

### No request body size limit

`json.loads(request.body)` has no `Content-Length` guard. Host-app infrastructure (nginx, gunicorn, load balancer) should enforce request body size limits.

### CSRF and session authentication

The MCP endpoint is `@csrf_exempt`. If a host app uses session-cookie authentication, browser-based CSRF attacks against the MCP endpoint become possible. MCP clients should use token authentication (Bearer / API key), not session cookies.

### No SSE / streaming in v1

The gateway is HTTP POST + JSON response only. Server-Sent Events (SSE) and streaming responses are out of scope for v1.

### No rate limiting

Rate limiting is the host application's concern and is not provided by friese-mcp. Apply rate limiting at the API gateway, reverse proxy, or Django middleware layer.

### OAuth 2.0 auth layer

An OAuth 2.0 / token issuance layer is deferred to v2. v1 inherits whatever authentication the host app attaches to the incoming HTTP request.
