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
- [Authentication and permissions](#authentication-and-permissions)
- [Built-in authentication](#built-in-authentication)
  - [contrib.tokens — static Bearer tokens](#contribtokens--static-bearer-tokens)
  - [contrib.oauth — OAuth 2.0 client credentials](#contriboauth--oauth-20-client-credentials)
  - [contrib.agents — per-agent tool allowlists](#contribagents--per-agent-tool-allowlists)
- [Auto-discovery](#auto-discovery)
- [Decorators](#decorators)
  - [@mcp_tool](#mcp_tool)
  - [@mcp_ignore](#mcp_ignore)
  - [@mcp_dispatcher and @mcp_action](#mcp_dispatcher-and-mcp_action)
- [ToolRegistry API](#toolregistry-api)
- [MCP gateway endpoint](#mcp-gateway-endpoint)
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

The `serverInfo.name` field returned in the `initialize` handshake response.

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

**Django admin:** Open `/admin/`, navigate to **Friese MCP Tokens**, and click **Add**. Set a `name` (e.g. `"claude-agent"`), leave `token` blank (auto-generated), and save. Copy the token value from the detail page.

**Shell:**
```python
from friese_mcp.contrib.tokens.models import FrieseMcpToken

# Token linked to a user
token = FrieseMcpToken.objects.create(name="claude-agent", user=my_user)

# Service token — no user
token = FrieseMcpToken.objects.create(name="ci-pipeline")

print(token.token)  # 64-hex-char secret — copy it now
```

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
| `token` | `CharField(64)` | Auto-generated 64-hex-char secret. Read-only after creation. |
| `name` | `CharField(200)` | Human-readable label (e.g. `"claude-agent"`). |
| `is_active` | `BooleanField` | Set to `False` to revoke. Inactive tokens are rejected. |
| `user` | `ForeignKey(AUTH_USER_MODEL, null=True)` | Optional user. `None` for service tokens. |
| `created_at` | `DateTimeField` | Auto-set on creation. |
| `last_used_at` | `DateTimeField(null=True)` | Updated on each successful authentication (queryset update, no signals). |

#### `FrieseMcpTokenAuthentication`

Reads `Authorization: Bearer <token>`. Returns `(user, token)` on success, where `user` is the associated Django user or `AnonymousUser` for service tokens. Raises `AuthenticationFailed` on invalid or inactive tokens. Returns `None` (passes to next authenticator) when the `Authorization` header is absent or uses a different scheme.

> **Service tokens and `IsAuthenticated`:** A service token with no linked user sets `request.user` to `AnonymousUser`. `AnonymousUser.is_authenticated` is `False`, so `IsAuthenticated` will deny the request. Either link service tokens to a user, or use a custom permission class that allows `AnonymousUser`.

---

### `contrib.oauth` — OAuth 2.0 client credentials

Full OAuth 2.0 `client_credentials` grant for AI agent clients (Claude, GPT, etc.). Clients exchange `client_id` + `client_secret` for a short-lived Bearer token. Tokens expire and must be refreshed. Includes RFC 8414 authorization server metadata and MCP-spec protected resource metadata for automatic client discovery.

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

**Django admin:** Open `/admin/`, navigate to **OAuth Clients**, and click **Add**. Set a name and scope. `client_id` and `client_secret` are auto-generated on save. Copy both values from the detail page.

**Shell:**
```python
from friese_mcp.contrib.oauth.models import OAuthClient

client = OAuthClient.objects.create(name="claude-agent")
print(client.client_id)     # 32-hex-char client identifier
print(client.client_secret) # 64-hex-char secret
```

#### OAuth flow

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
  "scope": "mcp"
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
| `POST` | `/oauth/token/` | Issue an access token (RFC 6749 §4.4). Form-encoded or JSON. |
| `POST` | `/oauth/register/` | Dynamic client registration (RFC 7591). Disabled unless `FRIESE_MCP_OAUTH_REGISTRATION_OPEN = True`. |
| `GET` | `/.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414). |
| `GET` | `/.well-known/oauth-protected-resource` | Protected resource metadata (MCP spec). |

#### Token endpoint errors

| `error` | HTTP | Cause |
|---|---|---|
| `unsupported_grant_type` | 400 | `grant_type` is not `client_credentials` |
| `invalid_request` | 400 | `client_id` or `client_secret` missing |
| `invalid_client` | 401 | Credentials not found, or client is inactive |

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
| `client_id` | `CharField(32)` | Auto-generated 32-hex-char identifier. |
| `client_secret` | `CharField(64)` | Auto-generated 64-hex-char secret. |
| `name` | `CharField(200)` | Human-readable label. |
| `is_active` | `BooleanField` | Set to `False` to revoke all token issuance. Existing tokens are also rejected. |
| `scope` | `CharField(200)` | Space-separated scopes (default `"mcp"`). |
| `created_at` | `DateTimeField` | Auto-set on creation. |

#### `OAuthAccessToken` model

| Field | Type | Description |
|---|---|---|
| `token` | `CharField(64)` | Auto-generated 64-hex-char Bearer token. |
| `client` | `ForeignKey(OAuthClient)` | Issuing client. Cascade-deletes with client. |
| `expires_at` | `DateTimeField` | Defaults to `now() + FRIESE_MCP_OAUTH_TOKEN_EXPIRY_SECONDS`. |
| `scope` | `CharField(200)` | Scopes granted (copied from client at issuance). |
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

**`@mcp_action(name, description, params=None, input_schema=None)`** — method decorator. Marks a method as a dispatchable action. Does not alter the method's behaviour.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Action name used as the `action` argument value (e.g. `"create"`). |
| `description` | `str` | Yes | Human-readable description shown in help-mode responses. |
| `params` | `dict[str, str]` | No | Mapping of param name → human-readable hint. Shown in help-mode. |
| `input_schema` | `dict` | No | JSON Schema (draft-07) for server-side validation of `params` before the method is called. |

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

The server always responds with its own `protocolVersion` (`2025-03-26`) regardless of what the client sends.

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

Returns an empty list in v1. Resources are not implemented.

#### `resources/read`

Returns `METHOD_NOT_FOUND` in v1.

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
    "data": "No tool named 'user.lis'. Did you mean: users.list, users.list_active?"
  }
}
```

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
| `-32601` | Method Not Found | Unrecognised method name, unknown tool name in `tools/call`, or `resources/read` in v1 |
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

### No SSE / streaming in v1

The gateway is HTTP POST + JSON response only. Server-Sent Events (SSE) and streaming responses are out of scope for v1.

### No rate limiting

Rate limiting is the host application's concern and is not provided by friese-mcp. Apply rate limiting at the API gateway, reverse proxy, or Django middleware layer.

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
