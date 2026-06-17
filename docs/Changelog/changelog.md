# Changelog

**Category:** reference  
**Slug:** changelog

---

## v1.0 — 2026-06

### Restored: `Meta.mcp_light_key` consumption in the lean write envelope

`_extract_lean_envelope` (`src/frisian_mcp/backends/invocation.py`) now honours
`ViewSet.serializer_class.Meta.mcp_light_key` when building the lean confirmation
envelope on write paths.  Fields listed in `mcp_light_key` appear in the envelope
in addition to the standard `id` / `pk` / `url` / `name` / `display` extraction.

```python
class DeviceSerializer(serializers.ModelSerializer):
    class Meta:
        fields = '__all__'
        mcp_light_key = ['site_slug', 'role']
```

This restores the behaviour documented in the
[Installation & Configuration Reference](../Reference/installation-configuration-reference.md)
and the [Write-Path Response Filtering](../Guide/write-path-response-filtering.md)
guide — both docs already describe the feature; the implementation now matches.

> `mcp_light_key` is a class attribute on the serializer's `Meta`, **not** a
> decorator.  See the cross-reference note in both docs after the example block.

---

## v0.9.0 — 2026-05

The dispatcher release. Adds the `FRISIAN_MCP_DISPATCH_GROUPS` pattern, `FRISIAN_MCP_AUTODISPATCH`, URL auto-registration, and a complete test suite now at 978 passing tests.

### New: FRISIAN_MCP_DISPATCH_GROUPS

The dispatcher pattern is now a first-class package setting. Map group names to resource prefix lists; each group becomes one MCP tool instead of one tool per endpoint.

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    'dcim':     ['device', 'rack', 'interface', 'cable', 'location'],
    'ipam':     ['ipaddress', 'prefix', 'vlan', 'vrf'],
    'circuits': ['circuit', 'provider', 'circuittermination'],
}
```

Token arithmetic validated against a 1,967-tool Django application: 5 dispatcher groups replace 1,967 flat tool schemas. Schema token load drops from ~490,000 tokens to ~2,000–4,000 tokens — a 99%+ reduction. Resources not included in any group remain as flat tools with no breaking change.

Each dispatcher accepts `action=help` to return the full resource/action tree for that group. Tool schemas are loaded lazily, only when the agent is about to use them.

### New: FRISIAN_MCP_AUTODISPATCH

Automatic resource-level dispatching without manual group configuration. Set `FRISIAN_MCP_AUTODISPATCH = True` to group all discovered tools by resource name automatically. One dispatcher per resource rather than one tool per action.

### New: URL auto-registration

`McpView`, OAuth endpoints, and well-known discovery endpoints all register automatically at startup via `AppConfig.ready()`. Host apps no longer need `urls.py` changes for any of these. `FRISIAN_MCP_PATH` setting controls the mount path (default: `mcp/`).

```python
FRISIAN_MCP_PATH = 'api/mcp'  # mounts at /api/mcp/
```

- `FRISIAN_MCP_PROTECTED_PATH` (optional) auto-registers an `IsAuthenticated`-required second `McpView` at the given path — the in-process variant of the open + authenticated pattern from Security.
- `FRISIAN_MCP_EXTRA_PATHS` (optional, list) registers `McpView` at additional same-config paths — use when an MCP client strips the path component.

### New: McpEndpointView as DRF APIView

The MCP endpoint view was converted from a plain Django function view to a DRF `APIView`. This means `FRISIAN_MCP_AUTHENTICATION_CLASSES` and `FRISIAN_MCP_PERMISSION_CLASSES` settings are now evaluated properly. Host apps with non-standard authentication (custom token models, Cognito JWT, etc.) can configure the MCP endpoint independently of `DEFAULT_AUTHENTICATION_CLASSES`.

### New: Zero-tool startup warning

When auto-discovery runs and finds zero tools, frisian-mcp now logs a startup warning with a hint to use `@mcp_tool` manual registration. This prevents silent misconfiguration on projects that use function-based views exclusively.

### New: FRISIAN_MCP_TOOLS_LIST_CACHE_TTL

At large ViewSet surfaces (1,000+ tools), `tools/list` generation is expensive to recompute on every call. This setting caches the result. Recommended for any host app with a large ViewSet surface.

```python
FRISIAN_MCP_TOOLS_LIST_CACHE_TTL = 300  # seconds
```

### Bug fixes

**DRFSyncDiscovery permission_classes** — discovered tools were inheriting host ViewSet `permission_classes`, causing MCP calls to fail if the ViewSet required specific permissions. Tools now have `permission_classes=()` by default; the permission tier system handles access gating independently.

**SyncInvocation accepted_media_type** — synthetic DRF requests constructed during MCP dispatch were missing `accepted_renderer` and `accepted_media_type`. This caused failures on ViewSets that inspect content negotiation. Fixed via `DefaultContentNegotiation` on request construction.

**urls.W002 warning** — inner URL pattern `r'^/?$'` corrected to `r'^$'`, eliminating the Django system check warning on startup.

**URL construction separator bug** — `f"{issuer}{mcp_path}"` where `mcp_path` had no leading slash produced malformed URLs (e.g. `https://host.comapi/mcp`). Fixed in both `OAuthClientAdmin.connector_mcp_url()` and `OAuthProtectedResourceView`. Now uses `f"{issuer.rstrip('/')}/{mcp_path.lstrip('/')}"`.

**camelCase/snake_case schema asymmetry** — field name transformation across read and write schemas is now consistent. Previously, serializers using camelCase field names produced inconsistent parameter names between tool input schemas and response schemas.

**Generic error on invalid UUID** — DRF `ValidationError` and `DoesNotExist` exceptions in tool handlers now surface as structured JSON-RPC error responses rather than `"Internal tool error"`. Agents receive actionable error messages.

**PyPI namespace `pyproject.toml` build backend** — switched from hatchling to setuptools to eliminate Docker install fragility and the macOS Python 3.13 `UF_HIDDEN` editable install issue.

### Tests

978 tests passing. Coverage expanded across dispatcher pattern, permission tier enforcement, and multi-app integration scenarios.

---

## v0.4.1 — 2026-04

Security patch sprint. All three items address findings from a formal code review of the v0.4.0 codebase.

### Security fixes

**SEC-1 — XFF injectable header fix** — `_get_base_url()` was reading `X-Forwarded-Proto` and `X-Forwarded-Host` from the first (attacker-injectable) position in the header list. Now reads from the last position (set by the nearest trusted proxy), matching the behavior of `_resolve_key()` in rate-limit middleware. `FRISIAN_MCP_TRUSTED_PROXY_COUNT` validation added in `OAuthConfig.ready()` — rejects bool, string, and negative int values with `ImproperlyConfigured`.

**SEC-2 — OAuthAccessToken `__str__` token masking** — `OAuthAccessToken.__str__` was exposing the first 8 plaintext characters of the token in Django admin list views and log output. Now returns `token[:4]****`. `IsAuthenticatedOrServiceToken.has_permission()` now checks `auth.is_active` — inactive tokens are denied even if they are the correct type.

**SEC-3 — FRISIAN_MCP_HMAC_KEY decoupling** — HMAC key for `FrisianMcpToken` and `OAuthClient.client_secret` now reads `FRISIAN_MCP_HMAC_KEY` first, falling back to `SECRET_KEY`. Production startup warnings added to `TokensConfig.ready()` and `OAuthConfig.ready()` when `FRISIAN_MCP_HMAC_KEY` is unset. Decouples token validity from Django `SECRET_KEY` rotation — rotating `SECRET_KEY` no longer silently invalidates all issued tokens.

```python
FRISIAN_MCP_HMAC_KEY = env('FRISIAN_MCP_HMAC_KEY')  # treat like a password pepper
```

### Tests

584 tests passing.

---

## v0.4.0 — 2026-04

Security hardening and integration completeness sprint. Addresses all findings from a formal code review of the v0.3.0 codebase before integration testing.

### New: HMAC-SHA256 hashed secret storage (AUTH-4)

`FrisianMcpToken.token` and `OAuthClient.client_secret` are no longer stored verbatim. Both are now stored as HMAC-SHA256 hashes. The raw value is exposed exactly once at creation time via `.plaintext_token` / `.plaintext_client_secret` instance attributes; it is absent after any subsequent database load.

```python
token = FrisianMcpToken.objects.create(user=user)
print(token.plaintext_token)  # only opportunity to retrieve the raw value
```

**Migration note:** Pre-v0.4.0 tokens are permanently invalidated on deploy. All tokens must be regenerated.

### New: Reverse proxy support (INFRA-1)

`FRISIAN_MCP_TRUSTED_PROXY_COUNT` setting (default `0`) enables correct OAuth issuer URL construction and rate-limit IP key resolution behind reverse proxies. When set, frisian-mcp reads the n-th-from-last `X-Forwarded-For` entry rather than the first (attacker-injectable) position.

```python
FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1  # one proxy in front of gunicorn
```

Startup warning added when `FRISIAN_MCP_OAUTH_ISSUER` is unset and `DEBUG=False`.

### New: WWW-Authenticate resource_metadata (DISCOVERY-1)

Both `OAuthTokenAuthentication` and `FrisianMcpTokenAuthentication` now return `resource_metadata=` in the `WWW-Authenticate` header on 401 responses. This is the discovery hook the MCP spec uses — without it, MCP clients cannot auto-trigger the OAuth flow on first connection.

### New: mcp_config client variants (CONFIG-1)

`mcp_config` management command now supports `--client`, `--token`, and `--name` arguments:

```bash
python manage.py mcp_config --client claude-code --token my-agent-key
python manage.py mcp_config --client claude-desktop --url https://your-domain.com/api/mcp/
```

Supported clients: `claude-code`, `cursor`, `claude-desktop`, `generic` (default, backwards-compatible).

### New: AgentConnection XOR constraint (DATA-1)

`AgentConnection` now enforces that at most one credential FK (token or OAuth client) may be set at a time, via both a DB-level `CheckConstraint` and a `clean()` method for admin-form validation. `_get_agent_connection()` now uses explicit `.order_by('-created_at')` for deterministic results when multiple connections exist.

### New: OAuthAccessToken.last_used_at (PARITY-1)

`OAuthAccessToken` now has a `last_used_at` field stamped on every successful authentication, matching the behavior of `FrisianMcpToken`. Consistent observability across both auth paths.

### New: IsAuthenticatedOrServiceToken permission class (UX-1)

New `frisian_mcp.contrib.tokens.permissions.IsAuthenticatedOrServiceToken` permission class. Grants access when `request.user.is_authenticated` OR `request.auth` is a `FrisianMcpToken` instance. Resolves the common footgun where service tokens (no linked user) are rejected by `IsAuthenticated` despite having a valid token.

```python
from frisian_mcp.contrib.tokens.permissions import IsAuthenticatedOrServiceToken

FRISIAN_MCP_PERMISSION_CLASSES = [
    'frisian_mcp.contrib.tokens.permissions.IsAuthenticatedOrServiceToken',
]
```

### Tests

584 tests passing. Up from 526 at v0.3.0.

---

## v0.3.0 — 2026-04

Integration completeness sprint. Adds imperative tool registration, pluggable discovery backends, SSE support, rate limiter backend abstraction, dynamic resource providers, and URI template extraction.

### New: frisian_mcp.register() imperative API

For host apps that cannot use `@mcp_tool` (function-based views, dynamic tool surfaces), tools can now be registered programmatically:

```python
import frisian_mcp

frisian_mcp.register(
    name='send_notification',
    description='Send a push notification to a user',
    input_schema={
        'type': 'object',
        'properties': {
            'user_id': {'type': 'integer'},
            'message': {'type': 'string'},
        },
        'required': ['user_id', 'message'],
    },
    handler=send_notification_handler,
)
```

### New: FRISIAN_MCP_DISCOVERY_BACKENDS

`FRISIAN_MCP_DISCOVERY_BACKENDS` setting accepts a list of dotted-path backend classes, merging results from each. `DRFSyncDiscovery` becomes one backend alongside any custom backends the host app provides. Backwards-compatible with the singular `FRISIAN_MCP_DISCOVERY_BACKEND` setting.

```python
FRISIAN_MCP_DISCOVERY_BACKENDS = [
    'frisian_mcp.backends.DRFSyncDiscovery',
    'myapp.mcp.CustomDiscoveryBackend',
]
```

### New: Dynamic resource provider

`ResourceRegistry.register_provider(list_fn, read_fn=None)` — registers a callable invoked at request time. `list_fn` result merges into `resources/list`; `read_fn` is tried as a fallback after a static URI miss. Enables dynamic resource surfaces that cannot be enumerated at startup.

### New: GET SSE support

`McpView` now handles `GET` requests with an empty `StreamingHttpResponse` and `Content-Type: text/event-stream`, per the MCP 2025-03-26 Streamable HTTP specification. Previously returned 405.

### New: Pluggable RateLimitMiddleware backend

`RateLimitMiddleware` now supports a `FRISIAN_MCP_RATE_LIMIT["backend"]` setting accepting a dotted path to a custom backend class. The default in-process `InMemoryRateLimitBackend` remains unchanged. Custom backends (Redis, memcached, etc.) implement `AbstractRateLimitBackend`.

### New: resources/read URI template extraction

`read_resource()` now supports RFC 6570-style template variable extraction. Variables are forwarded to the handler as a third argument when the handler accepts three or more positional parameters, preserving backwards compatibility with existing two-argument handlers.

### Tests

526 tests passing. Up from 175 at v0.2.0.

---

## v0.2.0 — 2026-04

Auth release. Adds full OAuth 2.0 for Claude, GPT, and other MCP clients, and per-agent token authentication via contrib modules.

### New: contrib.tokens

Per-agent token authentication without OAuth. `FrisianMcpToken` model managed via Django admin. Agents authenticate with `Authorization: Bearer <token>`. No custom code required from the host app.

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp.contrib.tokens',
]
```

### New: contrib.oauth — Full OAuth 2.0

Claude, GPT, and other AI agent clients connect without special handling. Implements:

- RFC 8414 Authorization Server Metadata (`/.well-known/oauth-authorization-server`)
- RFC 9728 Protected Resource Metadata (`/.well-known/oauth-protected-resource`)
- RFC 7591 Dynamic Client Registration (`/oauth/register/`)
- Authorization Code + PKCE (S256) — required by Claude and GPT
- Full `WWW-Authenticate: Bearer resource_metadata=...` discovery handshake

The discovery chain is automatic. An MCP client hitting an authenticated endpoint receives a 401 with the correct `WWW-Authenticate` header, discovers the metadata endpoints, registers a client, and completes the OAuth flow without any operator intervention beyond the initial settings configuration.

All OAuth endpoints auto-register at startup via `AppConfig.ready()`. No `urls.py` changes required from the host app.

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp.contrib.oauth',
]

FRISIAN_MCP_OAUTH_ISSUER = 'https://your-domain.com'
```

### Tests

175 tests passing. Up from 110 at v0.1.1.

---

## v0.1.1 — 2026-04

Integration fix release. Eight bugs surfaced during live integration testing against a production Django application.

### Bug fixes

**Discovery basename resolution** — `DRFSyncDiscovery` now reads `basename` from `initkwargs` (set by `DefaultRouter`) rather than parsing the URL path. Fixes `^items.list` → `items.list` and duplicate-segment names like `summary.summary` → `items.summary`.

**Schema derivation for non-body actions** — Schema generation now uses `_BODY_ACTIONS` set and `action_mapping` inspection. Destroy and GET custom actions no longer incorrectly pull serializer fields into the input schema.

**partial_update required fields** — `partial_update` schemas no longer mark any body fields as required. All fields are optional for PATCH, consistent with DRF semantics.

**SyncInvocation parser classes** — Synthetic DRF requests constructed during MCP dispatch now include `DEFAULT_PARSER_CLASSES`. Previously, write calls raised `UnsupportedMediaType` on all invocations.

**Custom action HTTP method detection** — `@action` HTTP method now detected via `action_func.mapping` rather than URL pattern inspection, correctly handling all method combinations.

**AnonymousUser fallback** — `request.user` now falls back to `AnonymousUser()` when absent, preventing `AttributeError` in permission checks on unauthenticated requests.

**Exception message leak** — `_handle_tools_call` broad exception handler was returning `str(exc)` in the JSON response body, leaking DB column names, internal file paths, and model field names. Now returns `"Internal tool error"` to the client; full detail logged server-side only.

**ToolInputError error code** — `ToolInputError` in tool handlers now returns `INVALID_PARAMS` (-32602) rather than falling through to the broad exception handler.

### Tests

110 tests passing.

---

## v0.1.0 — 2026-04

Initial release. PyPI namespace registered. Core MCP gateway working.

### Core features

**Auto-discovery** — `FRISIAN_MCP_AUTODISCOVER = True` scans Django URL patterns at startup, finds DRF ViewSets registered with routers, and generates MCP tools from each action. Tool input schemas derived from DRF serializer fields.

**Pluggable backend architecture** — discovery and invocation separated into two independent backend contracts. `DRFSyncDiscovery` (default) handles ViewSet scanning and schema generation. `SyncInvocation` (default) handles tool dispatch. Both are swappable for projects with custom ViewSet base classes, ASGI, or Celery job queues.

**`@mcp_tool` decorator** — explicitly register any function as an MCP tool, bypassing auto-discovery entirely. The correct path for projects using function-based views (`@api_view`) with no ViewSets. Registers directly with the `ToolRegistry` at startup.

**`@mcp_ignore` decorator** — exclude a ViewSet class or individual action method from auto-discovery. Excluded tools are completely invisible in `tools/list` — they do not appear at any permission tier.

**Permission tiers** — three-tier system (`read`, `read_write`, `admin`). Tools above the caller's tier are absent from `tools/list`, not refused on call. Agents never hit permission errors for operations they cannot use.

**`@mcp_heavy` decorator** — pagination-first behavior for list endpoints. Agent receives count and `next` URL with the first page rather than the full result set. Preserves context window at scale.

**MCP endpoint** — JSON-RPC 2.0 over StreamableHTTP at `/mcp/`. Supports `initialize`, `initialized`, `tools/list`, `tools/call`.

### Validated against

- Django application with DRF ViewSets and custom token authentication
- Local Django testbed (110/110 tests, 5 linters clean at release)

---

## Roadmap

**Imminent**
- PyPI public release (v1.0.0)
- AAIF submission — frisian-mcp will be submitted to the Linux Foundation's AI Application Interoperability Framework alongside the MCP ecosystem

---

*Changelog maintained by the frisian-mcp development team.*