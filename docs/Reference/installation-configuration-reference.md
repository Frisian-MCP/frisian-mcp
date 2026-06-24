# Installation & Configuration Reference

**Category:** reference  
**Slug:** installation-configuration-reference  
**Audience:** Developers integrating frisian-mcp into a Django project

---

## Requirements

- Python 3.10+
- Django 5.x
- Django REST Framework 3.x
- PostgreSQL (recommended) or SQLite for development

frisian-mcp has no required dependencies beyond Django and DRF. Optional contrib modules add their own dependencies (see below).

---

## Installation

```bash
pip install frisian-mcp
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp',
    # Optional contrib modules:
    # 'frisian_mcp.contrib.tokens',   # per-agent token auth
    # 'frisian_mcp.contrib.oauth',    # full OAuth 2.0
]
```

Run migrations:

```bash
python manage.py migrate
```

Mount the MCP endpoint in `urls.py`:

```python
from frisian_mcp.views import McpView

urlpatterns = [
    path('mcp/', McpView.as_view()),
    ...
]
```

That's the complete install. If your Django app already has DRF ViewSets registered to a router, they are now accessible via MCP at `/mcp/`.

---

## Settings Reference

All settings use the `FRISIAN_MCP_` prefix.

### FRISIAN_MCP_PATH

**Type:** `str`  
**Default:** `'mcp'`

The mount path for the primary `McpView`, auto-registered at startup via `AppConfig.ready()`. Host apps do not need to edit `urls.py` for this view — setting `FRISIAN_MCP_PATH` is enough.

```python
FRISIAN_MCP_PATH = 'mcp/public'  # mounts at /mcp/public/
```

---

### FRISIAN_MCP_PROTECTED_PATH

**Type:** `str`  
**Default:** `None` (no second mount)

When set, `AppConfig.ready()` auto-registers a second `McpView` subclass at this path that enforces `IsAuthenticated` and uncaps the effective tier ceiling for authenticated callers. This is the in-process variant of the open + authenticated pattern described in the Security architecture doc — both mounts live in one Django process; no reverse-proxy split is required.

```python
FRISIAN_MCP_PATH = 'mcp/public'
FRISIAN_MCP_PROTECTED_PATH = 'mcp/admin'
```

Pair with `FRISIAN_MCP_MAX_TIER = 'read'` on the primary path to keep that surface anonymous-read-only regardless of any token presented.

---

### FRISIAN_MCP_MAX_TIER

**Type:** `str` — one of `'read'`, `'read_write'`, `'admin'`  
**Default:** `None` (no cap)

Caps the effective tier for every caller hitting the **primary** `McpView` mount, including authenticated callers. When the protected mount is also auto-registered via `FRISIAN_MCP_PROTECTED_PATH`, the protected subclass overrides this cap so authenticated callers on that path see the full tier surface.

```python
FRISIAN_MCP_MAX_TIER = 'read'  # primary path is anonymous-read-only
```

---

### FRISIAN_MCP_PERMISSION_CLASSES

**Type:** `list`  
**Default:** `[]`

DRF permission classes applied at the gateway level on the primary `McpView`. Evaluated by the DRF `APIView` machinery as standard `permission_classes`. Use this when the primary mount needs a permission check (e.g. `IsAuthenticatedOrServiceToken`) in addition to or instead of the tier system.

```python
FRISIAN_MCP_PERMISSION_CLASSES = [
    'frisian_mcp.contrib.tokens.permissions.IsAuthenticatedOrServiceToken',
]
```

---

### FRISIAN_MCP_EXTRA_PATHS

**Type:** `list[str]`  
**Default:** `[]`

Additional mount paths for the same primary `McpView` configuration. Useful when an MCP client strips a path component on its way through a proxy or when you want the same surface reachable at multiple URLs without re-registering.

```python
FRISIAN_MCP_EXTRA_PATHS = ['api/mcp', 'v1/mcp']
```

---

### FRISIAN_MCP_AUTODISCOVER

**Type:** `bool`  
**Default:** `True`

When `True`, frisian-mcp walks your DRF router at startup and registers all ViewSets as MCP tools automatically. Set to `False` if you want to use explicit registration only.

```python
FRISIAN_MCP_AUTODISCOVER = True
```

**Auto-discovery produces zero tools when:**

- ViewSets are not yet resolved at discovery time — verify your router registration runs before frisian-mcp's app ready signal
- All discovered ViewSets are decorated with `@mcp_ignore`
- Only function-based views are in use (auto-discovery reads ViewSets only)

---

### FRISIAN_MCP_DISPATCH_GROUPS

**Type:** `dict[str, list[str]]`  
**Default:** unset

Mapping `{group_name: [resource_prefix, ...]}` that collapses a set of flat auto-discovered tools into a single group dispatcher tool. Without this setting, dispatcher installation early-returns (`src/frisian_mcp/apps.py:573-577`) and the agent sees one flat tool per ViewSet action — the dispatcher reduction is opt-in, not automatic.

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    "catalog": ["item", "category", "supplier"],
    "stock":   ["stock_level", "stock_movement"],
}
```

**How prefix matching works.** Member-tool selection is `startswith` based (`apps.py:550`): a configured prefix `"purchase_order"` matches `purchase_order_list` AND `purchase_order_line_list` because both start with `purchase_order` followed by the tool-name separator. Use this when you want one group to bundle a related family of resources.

**Prefixes must match the leading segment of registered tool names.** The exact form depends on your DRF router configuration:

- **DRF default basename** (router doesn't specify `basename=`): DRF derives the basename from `Model._meta.object_name.lower()` — e.g. a `StockMovement` model produces basename `stockmovement` and tool names like `stockmovement_list`. Configure `"stockmovement"` (no underscore).
- **Explicit router basename** (you registered with e.g. `router.register('stock-movement', ...)`): the package converts hyphens to underscores at discovery time (`backends/discovery.py:367`) so the tool prefix becomes `stock_movement`. Configure `"stock_movement"` (with underscore).
- **Custom basename**: whatever you passed — e.g. `register(..., basename='widget')` produces `widget_list`. Configure `"widget"`.

**Misconfigured groups warn at startup.** A group whose configured prefixes match zero tools logs a `WARNING` and prints a `[frisian-mcp] WARNING` line with "Did you mean:" suggestions derived from the actually-registered resource names (`apps.py:600-635`). The group is silently dropped — its flat tools remain visible in `tools/list`. If you see a `0 matching tools` warning, the most common cause is configuring camelcase-stripped prefixes (`stockmovement`) for a build that uses kebab-case router slugs (which become `stock_movement` after the hyphen→underscore conversion), or vice versa. Match the suggestion the warning prints rather than guessing.

```text
[frisian-mcp] WARNING: dispatch group 'stock' has 0 matching tools — its flat tools will
remain visible in tools/list and may crowd out other dispatchers. Hint: use
Model._meta.object_name.lower(). See log.
```

---

### FRISIAN_MCP_API_KEYS

**Type:** `dict`  
**Default:** `{}` (no static keys; all callers treated as unauthenticated)

Maps API key strings to permission tiers. The simplest auth configuration for development and internal tools.

```python
FRISIAN_MCP_API_KEYS = {
    'agent-read-write-key': 'read_write',
    'agent-readonly-key': 'read',
    'admin-key': 'admin',
}
```

Agents pass the key as `Authorization: Bearer <key>`. No database setup required.

---

### FRISIAN_MCP_UNAUTHENTICATED_TIER

**Type:** `str`  
**Default:** `'read'`

The maximum permission tier for callers who provide no credentials. Set to `None` to require authentication for all tool access.

```python
# Public read access (default)
FRISIAN_MCP_UNAUTHENTICATED_TIER = 'read'

# Require auth for everything
FRISIAN_MCP_UNAUTHENTICATED_TIER = None
```

---

### FRISIAN_MCP_SERVER_NAME

**Type:** `str`  
**Default:** `'frisian-mcp'`

The server name returned in the MCP `initialize` response. Agents use this to identify which server they're connected to.

```python
FRISIAN_MCP_SERVER_NAME = 'my-app-mcp'
```

---

### FRISIAN_MCP_EXPOSE_ERRORS

**Type:** `bool`  
**Default:** `False`

When `False`, exceptions in tool handlers return a generic error message. When `True`, the full exception message is returned. Useful for development; leave `False` in production to avoid leaking internal detail.

```python
FRISIAN_MCP_EXPOSE_ERRORS = True  # development only
```

---

### FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD

**Type:** `int` (bytes)  
**Default:** system default (see package source)

Response size threshold above which frisian-mcp automatically applies `@mcp_heavy` pagination behavior, even on ViewSets that are not explicitly decorated. Prevents large responses from exhausting agent context windows without requiring manual decoration of every ViewSet.

```python
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 50000  # bytes
```

---

### FRISIAN_MCP_AUTHENTICATION_CLASSES

**Type:** `list`  
**Default:** Uses DRF's `DEFAULT_AUTHENTICATION_CLASSES`

Override the authentication backends used for MCP requests specifically, without changing your DRF defaults.

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    'frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication',
    'frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication',
]
```

> **Chain ordering rule.** When using static tokens and OAuth together, **always list `FrisianMcpTokenAuthentication` (and / or `FrisianMcpApiKeyAuthentication`) BEFORE `OAuthTokenAuthentication`**. The first authenticator in the chain emits the WWW-Authenticate challenge on 401 responses. Tokens-first emits a bare `Bearer` challenge so static-token MCP clients (Claude Code, Codex, Gemini CLI) fall back cleanly to their configured Bearer. OAuth-first emits `Bearer realm="...", resource_metadata="..."`, which nudges discovery-first clients into the OAuth cascade — fine if every client is an OAuth client, but a footgun the moment you add a static-token coding agent.

---

### FRISIAN_MCP_OAUTH_ISSUER

**Type:** `str`  
**Required when using `contrib.oauth`**

The base URL of your OAuth issuer. Used to construct well-known metadata endpoints (`/.well-known/oauth-authorization-server`) and validate tokens.

```python
FRISIAN_MCP_OAUTH_ISSUER = 'https://your-domain.com'
```

---

### FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY

**Type:** `bool`  
**Default:** `False`

When `True`, `tools/list` is filtered per-request so each caller sees only the tools their identity is permitted to use, based on `user.get_all_permissions()`. Tools outside the caller's permission set are omitted entirely — they do not appear at any tier.

Default off. Enabling this setting introduces no migrations and does not change behavior for unauthenticated or tier-only callers unless the authentication backend is configured to resolve identities to real Django users.

```python
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
```

> **Security note:** This setting controls tool *visibility*, not *execution* enforcement. REST calls execute as the resolved `request.user`, which is governed by `FRISIAN_MCP_SERVICE_ACCOUNT_USER` (anonymous callers) or the OAuth user resolution settings (OAuth callers). See the [Security Guidance](../Guide/permission-aware-discovery-security.md) for deployment requirements.

See [Permission-Aware Discovery](../Guide/permission-aware-discovery.md) for the full guide.

---

### FRISIAN_MCP_PERMISSION_ADAPTER

**Type:** `str` (dotted import path)  
**Default:** `"frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"`

Dotted import path to the permission adapter class used when `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` is `True`. The adapter must implement the `PermissionAdapter` protocol: `get_capabilities(user) -> frozenset[str]` and `is_unrestricted(user) -> bool`.

```python
# Default: standard Django ModelBackend
FRISIAN_MCP_PERMISSION_ADAPTER = (
    "frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter"
)

# For backends using EXEMPT_VIEW_PERMISSIONS semantics
FRISIAN_MCP_PERMISSION_ADAPTER = (
    "frisian_mcp.contrib.permissions.exempt_view_adapter.ExemptViewPermissionAdapter"
)
```

---

### FRISIAN_MCP_OAUTH_SERVICE_USER

**Type:** `str` (Django username)  
**Default:** `None`

The username of the Django user that OAuth-authenticated requests resolve to for permission checking and execution. Required when `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` is `True` and `frisian_mcp.contrib.oauth` is installed, unless all `OAuthClient` records have a per-client user configured in the admin.

When set, OAuth callers execute as this user — both discovery filtering and REST invocations use this user's permissions.

```python
FRISIAN_MCP_OAUTH_SERVICE_USER = "mcp_service_account"
```

> **Warning:** Do not set this to a superuser or admin account in production. The execution identity determines what OAuth callers can actually do via the REST layer. Use a minimum-privilege account whose permissions match the desired tool surface.

---

### FRISIAN_MCP_SERVICE_ACCOUNT_USER

**Type:** `str` (Django username)  
**Default:** `None`

The username of the Django user that **anonymous** (unauthenticated) MCP requests execute as. When set, anonymous callers satisfy host-app `IsAuthenticated` checks and the specified user's credentials are used for all REST invocations on the anonymous path.

```python
FRISIAN_MCP_SERVICE_ACCOUNT_USER = "mcp_readonly_service"
```

> **Warning:** Setting this to an admin or superuser account grants every anonymous caller full admin execution rights at the REST layer, regardless of `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` or tier settings. Restrict this to isolated or air-gapped networks. For shared or production instances, use a minimum-privilege non-admin account. See [Permission-Aware Discovery — Security Guidance](../Guide/permission-aware-discovery-security.md).

---

## Management Commands

frisian-mcp ships three Django management commands. Run with `python manage.py <command>` (or `nautobot-server <command>`, `docker exec <container> python manage.py <command>`, etc., depending on host).

| Command | Purpose | Guide |
|---|---|---|
| `mcp_doctor` | Audit the host's frisian-mcp integration end-to-end. Default pass runs eight checks (INSTALLED_APPS, URL mounting, auth wiring, security settings, cache backend, performance hints, OAuth registration posture, authorize URL reachability). `--security` adds six OAuth-specific security checks. Exits non-zero on errors. CI-pipeline usable. | [Guide → mcp_doctor](../Guide/mcp-doctor.md) |
| `mcp_config` | Generate a client config JSON snippet for connecting an MCP client to this gateway. `--client <name>` emits the format expected by a specific client; `--token <value>` embeds an auth header; `--url`/`--name` override the server URL and key. | (inline; see `mcp_config --help`) |
| `mcp_hash_api_key` | Compute the HMAC-SHA256 digest of a raw API key for use in `FRISIAN_MCP_API_KEYS`. Keys are stored as digests, not raw values, so a leaked settings file does not directly expose usable credentials. | (inline; see `mcp_hash_api_key --help`) |

Run `mcp_doctor` after every install, after every config change, and as the first diagnostic step on any unexpected behaviour — most integration issues surface as a single `⚠` or `✗` line in the doctor output.

---

## Decorator Reference

### @mcp_ignore

Excludes a ViewSet or individual method from MCP auto-discovery.

```python
from frisian_mcp.decorators import mcp_ignore

# Exclude entire ViewSet
@mcp_ignore
class InternalViewSet(viewsets.ModelViewSet):
    ...

# Exclude a specific action
class UserViewSet(viewsets.ModelViewSet):

    @mcp_ignore
    def admin_reset(self, request, pk=None):
        ...
```

Use this for UI-oriented endpoints, admin actions, or any surface not intended for agent consumption. Decorated ViewSets and methods are completely invisible in `tools/list` — they do not appear at any permission tier.

---

### @mcp_heavy

Explicit MCP tool registration that enforces a probe-then-fetch protocol. The first call returns a preview, total size, available modes (`summary` / `paginated` / `filtered` / `full`), and a continuation token; the second call returns the requested mode against the cached result.

`@mcp_heavy` is a sibling of `@mcp_tool` / `@mcp_dispatcher` / `@mcp_action`. It requires `name`, `description`, and `input_schema` arguments, and the decorated callable must have a `(arguments, request)` signature — it is **not** a bare wrapper for a DRF `ModelViewSet` method. Applying it bare on a ViewSet method raises `TypeError: mcp_heavy() missing 2 required positional arguments` at import.

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

**For auto-discovered ViewSets**, set [`FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD`](#frisian_mcp_auto_negotiate_threshold) instead — any auto-discovered tool whose response exceeds the byte threshold is auto-wrapped in the same probe envelope without a per-ViewSet code change.

The agent is not prevented from paginating — it receives the metadata it needs to make that decision. `@mcp_heavy` ensures the context window is not pre-filled with data the agent may never use.

---

### @mcp_light

Write-path response filtering. All create, update, and destroy tools return a lean confirmation envelope by default rather than echoing the full serialized object. Applied automatically at the package level — no decorator is required on the ViewSet.

**Default lean envelope shapes:**

Single-object create or update:

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

Bulk create or update (when supported by the underlying ViewSet):

```json
{
  "accepted": 60,
  "failed": 0,
  "status_code": 201,
  "data_size": 43190,
  "continuation_token": "<token>"
}
```

> **Note:** Bulk create is a passthrough — frisian-mcp does not add bulk support to ViewSets that don't already implement it. The `accepted`/`failed` envelope only appears when the host ViewSet's DRF implementation handles a list body on the create endpoint. If the underlying ViewSet does not support bulk create, a standard single-object create is all that is available.

Delete:

```json
{
  "id": "abc123",
  "deleted": true,
  "status_code": 204
}
```

Read and list operations are unaffected. The `verify` parameter is a no-op on read tools.

**`verify=True` — per-call full-object override:**

The `verify` parameter is injected automatically into every write tool's inputSchema. Passing `verify=True` on a specific call returns the full serialized object directly — no caching, no second call:

```json
{
  "resource": "device",
  "action": "create",
  "params": { "name": "edge-01", "site": "hq-1" },
  "verify": true
}
```

**Continuation token — retrieve full object without re-executing the write:**

The `continuation_token` in the lean envelope reuses the `@mcp_heavy` cache infrastructure. Pass it to the heavy-fetch path with `mode=full` to retrieve the complete serialized object. The write is not re-run.

**`mcp_light_key` — custom lean envelope fields:**

`mcp_light_key` is a class attribute on the serializer's `Meta` — **not** a decorator, despite the `@mcp_*` family naming. To include specific serializer fields in the lean envelope beyond the standard `id` / `url` / `name` / `display` extraction, declare it directly in `Meta`:

```python
class DeviceSerializer(serializers.ModelSerializer):
    site_slug = serializers.SlugRelatedField(
        source='site', slug_field='slug', read_only=True
    )

    class Meta:
        fields = '__all__'
        mcp_light_key = ['site_slug', 'role']
```

Fields listed in `mcp_light_key` appear in every lean envelope for that serializer, in addition to the standard identifying fields.

**Lean field extraction order:** `id` / `pk` → `url` → `name` / `display` → `mcp_light_key` annotated fields → `status_code`, `data_size`, `continuation_token` (always present).

**Precedence:** If a tool carries both `@mcp_heavy` and write semantics, `@mcp_heavy` probe behavior takes precedence.

---

### @mcp_dispatcher and @mcp_action

For explicit tool registration with full control over names, descriptions, and permission tiers:

```python
from frisian_mcp.decorators import mcp_dispatcher, mcp_action

@mcp_dispatcher(name='inventory')
class InventoryDispatcher:

    @mcp_action(
        description='List all items in inventory with optional filters',
    )
    def list(self, request, params):
        category = params.get('category')
        ...
        return Response(data)

    @mcp_action(
        description='Create a new inventory item',
        write=True  # requires authenticated caller at read_write tier or above
    )
    def create(self, request, params):
        ...

    @mcp_action(
        description='Purge all inventory records',
        admin=True  # requires admin tier
    )
    def purge(self, request, params):
        ...
```

When agents call `tools/list`, they see one tool: `inventory`. Calling `inventory` with `action=help` returns the full action tree with parameter schemas. This is the dispatcher pattern: one tool, discoverable depth.

---

## Auth Module Setup

### contrib.tokens — Per-Agent Token Auth

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp.contrib.tokens',
]
```

```bash
python manage.py migrate
```

Creates the `FrisianMcpToken` model. Tokens are managed via Django admin. Each token is associated with a user and inherits that user's Django permissions.

No additional settings required. Add `FrisianMcpTokenAuthentication` to `FRISIAN_MCP_AUTHENTICATION_CLASSES` if you want it to run alongside other auth backends.

---

### contrib.oauth — Full OAuth 2.0

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp.contrib.oauth',
]

FRISIAN_MCP_OAUTH_ISSUER = 'https://your-domain.com'
```

```bash
python manage.py migrate
```

Mounts automatically:

- `/.well-known/oauth-authorization-server` — RFC 8414 metadata
- `/mcp/oauth/token/` — token endpoint (client_credentials grant)
- `/mcp/oauth/register/` — RFC 7591 dynamic client registration

Claude.ai, Claude Code, and GPT all support OAuth 2.0 client_credentials. Once `contrib.oauth` is configured, these clients connect without any special handling — they discover the metadata endpoint, register a client, and exchange credentials for a bearer token automatically.

---

## Common Patterns

### Brownfield: Existing Django App

The most common case. You have a Django app with DRF ViewSets. You want to make it agent-accessible without refactoring.

1. Install, add to `INSTALLED_APPS`, mount the endpoint
2. Set `FRISIAN_MCP_AUTODISCOVER = True` (default)
3. Add `@mcp_ignore` to any ViewSets not appropriate for agent consumption (admin panels, UI-specific endpoints)
4. Set `FRISIAN_MCP_API_KEYS` for initial access
5. Connect your MCP client

Your existing permissions, serializers, and URL structure all work as-is. frisian-mcp reads your OpenAPI schema and builds MCP tool definitions from it dynamically.

---

### Greenfield: Agent-First from the Start

When you're building a new application and want agents as first-class users from day one:

1. Design your ViewSets with agent interaction patterns in mind — clear names, consistent parameter shapes, metadata-first responses
2. Use `@mcp_dispatcher` and `@mcp_action` for explicit control over what agents see and how operations are named
3. Apply `@mcp_heavy` to any list endpoint that could return more than a few dozen records
4. Use permission tiers to gate write operations from the start — easier to open up later than to lock down

The distinction between brownfield and greenfield is mostly about tool description quality. Auto-discovered ViewSets get DRF-generated descriptions like "List device objects" — functional but not agent-optimized. Explicit `@mcp_action` descriptions let you write "List network devices filtered by site, role, or status — returns count and pagination metadata" — which is what agents need to select the right tool confidently.

---

### Hybrid: Some Auto-Discovered, Some Explicit

The practical middle ground for most projects. Auto-discover the standard CRUD surfaces, register explicit dispatchers for the operations that benefit from better descriptions or custom behavior.

```python
# settings.py
FRISIAN_MCP_AUTODISCOVER = True  # picks up all standard ViewSets

# A custom dispatcher for a workflow that spans multiple resources
@mcp_dispatcher(name='device_onboarding')
class DeviceOnboardingDispatcher:

    @mcp_action(description='Provision a new device across DCIM, IPAM, and DNS in a single operation', write=True)
    def provision(self, request, params):
        # spans multiple ViewSets internally, returns clean result
        ...
```

---

## Deployment Notes

### Diagnostic logging for token-auth issues

Default Django logging swallows the DEBUG-level messages frisian-mcp's auth backends emit on token-verification failure (expired token, wrong tier, malformed JWT). Without an explicit `LOGGING` config the symptom is "the client suddenly can't auth" with no signal in the logs. Wire two pieces:

**1. Enable DEBUG on the package's auth loggers** so the backends surface their own failures:

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "loggers": {
        "frisian_mcp.contrib.oauth.authentication":  {"handlers": ["console"], "level": "DEBUG"},
        "frisian_mcp.contrib.tokens.authentication": {"handlers": ["console"], "level": "DEBUG"},
        # Add for your own MCP auth middleware below.
        "myapp.mcp_auth":                            {"handlers": ["console"], "level": "INFO"},
    },
}
```

**2. Add a thin middleware that logs `Authorization`-header presence on every MCP request** without ever logging the raw credential. The regression signal you watch for is the line moving from `INFO ... auth=Bearer prefix=...` to `WARNING ... NO Authorization header` — that's the "client stopped sending the bearer" event, visible within seconds of it starting.

```python
# myapp/middleware.py
import logging
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger("myapp.mcp_auth")

MCP_PATH_PREFIXES = ("/mcp/",)  # adjust to your FRISIAN_MCP_PATH

class MCPAuthLoggingMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if not any(request.path.startswith(p) for p in MCP_PATH_PREFIXES):
            return None
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        client_ip = request.META.get("REMOTE_ADDR", "?")
        ua = request.META.get("HTTP_USER_AGENT", "")[:80]
        if not auth:
            logger.warning("MCP %s %s — NO Authorization header (ip=%s ua=%r)",
                           request.method, request.path, client_ip, ua)
            return None
        scheme, _, credential = auth.partition(" ")
        credential = credential.strip()
        prefix = (credential[:8] + "...") if credential else "(empty)"
        logger.info("MCP %s %s — auth=%s prefix=%s len=%d (ip=%s ua=%r)",
                    request.method, request.path, scheme, prefix,
                    len(credential), client_ip, ua)
        return None
```

Register the middleware in `MIDDLEWARE` ahead of any auth or CSRF middleware so it sees the request before the token is touched. Never log the full credential — `prefix[:8] + len()` is enough detail to correlate against an admin record without exposing the secret in logs that may persist in centralized log aggregation.

---

### SSE keepalive requires an ASGI worker class

frisian-mcp's MCP endpoints stream over SSE. The WSGI keepalive iterator (`src/frisian_mcp/views.py:1688`) calls `time.sleep(min(15.0, remaining))` to hold the connection open, which ties up one sync worker for the lifetime of each MCP client connection. With sync gunicorn workers (`-k sync`, the default), N workers caps you at N concurrent MCP clients — the (N+1)th connection waits, then the worker pool starves.

Use an ASGI worker class so the keepalive runs as `await asyncio.sleep(...)` against the event loop:

```bash
gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker
# or
uvicorn config.asgi:application
```

**Do not** use sync gunicorn workers, uwsgi, or mod_wsgi for production deployments. Bumping `--timeout` to 120s+ delays the symptom (`WORKER TIMEOUT` loops) but does not fix the structural mismatch — the worker pool still starves the moment your MCP client connection count meets your worker count.

---

## Troubleshooting

**Zero tools returned on `tools/list`**  
Check that `FRISIAN_MCP_AUTODISCOVER = True` and your DRF router has ViewSets registered before frisian-mcp's app ready signal fires. If using explicit registration, verify the dispatcher class is imported at startup.

**`WORKER TIMEOUT` loop after MCP client connects**  
Sync gunicorn workers cannot host SSE keepalive — every connection pins one worker until it times out. See [SSE keepalive requires an ASGI worker class](#sse-keepalive-requires-an-asgi-worker-class) above. Switch to `uvicorn.workers.UvicornWorker` (or plain `uvicorn`).

**404 on `/mcp/`**  
Verify the path is included in your root `urls.py`. Both trailing-slash (`/mcp/`) and non-slash (`/mcp`) variants should be tested — Django's `APPEND_SLASH` setting affects which resolves correctly. If running behind a reverse proxy (nginx, Caddy), confirm the proxy is forwarding the `/mcp/` path to gunicorn and not consuming it.

**Authentication errors on write operations**  
Confirm the caller's API key maps to `read_write` or `admin` in `FRISIAN_MCP_API_KEYS`, or that the OAuth token was issued with appropriate scope. Read-tier callers will not see write-tier tools in `tools/list` at all — if the tool is absent rather than returning a 403, the caller is authenticating below the required tier.

**Auto-discovery picks up ViewSets you don't want exposed**  
Add `@mcp_ignore` to the ViewSet class or to specific action methods. For large apps, it can be easier to set `FRISIAN_MCP_AUTODISCOVER = False` and use explicit `@mcp_dispatcher` registration for the surfaces you want to expose.

---
