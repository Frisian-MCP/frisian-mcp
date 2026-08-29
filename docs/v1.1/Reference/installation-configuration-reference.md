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

### FRISIAN_MCP_ROUTES

**Type:** `dict[str, dict]`  
**Default:** `None` (single-path mounting; see `FRISIAN_MCP_PATH`)  
**Since:** v1.1.0

Mounts read / read-write / admin surfaces on separate paths, each with its own tool allow/deny list and tier ceiling, on a deny-all baseline. A tool denied on a route does not exist there — it is absent from `tools/list` and `tools/call`, not rejected at call time.

```python
FRISIAN_MCP_ROUTES = {
    "default":  {"path": "/mcp",          "highest_tier": "read"},
    "elevated": {"path": "/mcp/elevated", "highest_tier": "read_write", "allow_list": ["*"]},
    "admin":    {"path": "/mcp/ops",      "highest_tier": "admin",      "allow_list": ["*"], "deny_list": ["billing"]},
}
```

**When `FRISIAN_MCP_ROUTES` is set, the routes are the entire gateway surface.** The single-path mount (`FRISIAN_MCP_PATH`), `FRISIAN_MCP_PROTECTED_PATH`, and `FRISIAN_MCP_EXTRA_PATHS` are not mounted — each is logged with a warning if set — so a legacy path cannot re-expose the unfiltered registry beside the deny-carved routes.

See the [Per-Route Permissions guide](../Guide/per-route-permissions.md) for configuration, and [ADR-010](../../ADR/adr-010-per-route-permission-model.md) for the model and its security rationale.

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

When `True`, frisian-mcp walks your DRF router and registers all ViewSets as MCP tools automatically. The scan is scheduled during app-ready but **deferred until the first request**, so it captures ViewSets registered by late-loading plugins; under a long-lived worker this is a one-time cost on the first call rather than at process start. Set to `False` if you want to use explicit registration only.

```python
FRISIAN_MCP_AUTODISCOVER = True
```

**Auto-discovery produces zero tools when:**

- ViewSets are not yet resolved at discovery time — verify your router registration completes before the first request (discovery is deferred to the first request, not run at app-ready)
- All discovered ViewSets are decorated with `@mcp_ignore`
- Only function-based views are in use (auto-discovery reads ViewSets only)

---

### FRISIAN_MCP_DISPATCH_GROUPS

**Type:** `dict[str, list[str]]`  
**Default:** unset

Mapping `{group_name: [resource_prefix, ...]}` that collapses a set of flat auto-discovered tools into a single group dispatcher tool. Without this setting, dispatcher installation early-returns (`src/frisian_mcp/apps.py`) and the agent sees one flat tool per ViewSet action — the dispatcher reduction is opt-in, not automatic.

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    "catalog": ["item", "category", "supplier"],
    "stock":   ["stock_level", "stock_movement"],
}
```

**How prefix matching works.** Member-tool selection is `startswith` based (`apps.py`): a configured prefix `"purchase_order"` matches `purchase_order_list` AND `purchase_order_line_list` because both start with `purchase_order` followed by the tool-name separator. Use this when you want one group to bundle a related family of resources.

**Prefixes must match the leading segment of registered tool names.** The exact form depends on your DRF router configuration:

- **DRF default basename** (router doesn't specify `basename=`): DRF derives the basename from `Model._meta.object_name.lower()` — e.g. a `StockMovement` model produces basename `stockmovement` and tool names like `stockmovement_list`. Configure `"stockmovement"` (no underscore).
- **Explicit router basename** (you registered with e.g. `router.register('stock-movement', ...)`): the package converts hyphens to underscores at discovery time (`backends/discovery.py`) so the tool prefix becomes `stock_movement`. Configure `"stock_movement"` (with underscore).
- **Custom basename**: whatever you passed — e.g. `register(..., basename='widget')` produces `widget_list`. Configure `"widget"`.

**Misconfigured groups warn at startup.** A group whose configured prefixes match zero tools logs a `WARNING` and prints a `[frisian-mcp] WARNING` line with "Did you mean:" suggestions derived from the actually-registered resource names (`apps.py`). The group is silently dropped — its flat tools remain visible in `tools/list`. If you see a `0 matching tools` warning, the most common cause is configuring camelcase-stripped prefixes (`stockmovement`) for a build that uses kebab-case router slugs (which become `stock_movement` after the hyphen→underscore conversion), or vice versa. Match the suggestion the warning prints rather than guessing.

```text
[frisian-mcp] WARNING: dispatch group 'stock' has 0 matching tools — its flat tools will
remain visible in tools/list and may crowd out other dispatchers. Hint: use
Model._meta.object_name.lower(). See log.
```

---

### FRISIAN_MCP_API_KEYS

**Type:** `dict`  
**Default:** `{}` (no static keys; all callers treated as unauthenticated)

Maps API-key **digests** to permission tiers. The simplest auth configuration for development and internal tools — no database setup required.

The dict keys are **HMAC-SHA256 digests (64 hex chars), not the raw keys.** Generate each digest from its raw key with the management command, then paste the digest into settings:

```bash
python manage.py mcp_hash_api_key <raw-key>   # prints the 64-char digest
```

```python
FRISIAN_MCP_API_KEYS = {
    # HMAC-SHA256 digest of the raw key  ->  tier
    # (64 hex chars each; produced by `mcp_hash_api_key`)
    '<64-hex HMAC-SHA256 digest of raw key #1>': 'read_write',
    '<64-hex HMAC-SHA256 digest of raw key #2>': 'read',
    '<64-hex HMAC-SHA256 digest of raw key #3>': 'admin',
}
```

An agent sends the **raw** key as `Authorization: Bearer <raw-key>`; the package HMACs it and looks up the digest. Because only digests live in settings, a leaked settings file does not expose usable credentials.

---

### FRISIAN_MCP_UNAUTHENTICATED_TIER

**Type:** `str`  
**Default:** `'read'`

The maximum permission tier for callers who provide no credentials.

Four cases are distinguished. Two grant access — an absent setting, which uses the compatibility default, and an explicitly configured tier. The other two deny:

| Value | Effect |
|---|---|
| setting absent | `read` — the documented default, so a host that never set it keeps working |
| `'read'` / `'read_write'` / `'admin'` | that tier |
| `None` or `'none'` | denied below `read` — no tool is listed and none can be invoked without credentials |
| any other value | denied below `read`, **and** a startup error is raised naming the setting |

The last row matters: a misspelled tier is a configuration error, not a silent downgrade. It is reported at startup rather than discovered in production.

```python
# Public read access (default)
FRISIAN_MCP_UNAUTHENTICATED_TIER = 'read'

# Require auth for everything
FRISIAN_MCP_UNAUTHENTICATED_TIER = None
```

> **Correction — this setting did not deny access in releases before this one.** Earlier documentation, including two install configurations distributed as production examples, stated that `None` requires authentication for all tools. It did not: an unrecognised tier — which `None` and `'none'` both were — ranked equal to `read`, so anonymous callers kept the full read surface. The setting was present, spelled plausibly, and had no effect. If you deployed against that guidance, treat the read surface of those deployments as having been reachable without credentials, and confirm what was mounted there. The behaviour described in the table above is what this release does.

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
**Default:** `settings.DEBUG`

When unset it tracks `DEBUG`: full exception messages are returned when `DEBUG=True`, generic error messages when `DEBUG=False`. Set it explicitly to `False` to mask errors even in an environment where `DEBUG` might be on. Leave errors masked in production to avoid leaking internal detail.

```python
FRISIAN_MCP_EXPOSE_ERRORS = True  # development only
```

---

### FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD

**Type:** `int` (bytes), or `None` to disable  
**Default:** `25000` (bytes)

Response size threshold for the read-response negotiation backstop. When a successful non-write response exceeds this byte size, frisian-mcp returns a small probe envelope only if the tool's published schema already discloses the continuation call. That includes explicitly registered `@mcp_heavy` tools, class dispatchers, and group dispatchers. It does not include ordinary `@mcp_tool` registrations or plain auto-discovered ViewSet actions. For those non-disclosing tools, an over-threshold response is returned complete: no truncation, no continuation token, and no cache entry pinned.

**The default is active** (`25000` bytes ≈ ~6k `cl100k_base` tokens — above a normal small filtered read, well below a large list page). Any qualifying response over the threshold therefore takes the schema-disclosed negotiation path out of the box; high-cardinality lists are the common trigger.

- **Raise** the value to probe less often (larger responses returned in full); **lower** it to probe sooner.
- Set **`FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = None`** to fully disable the backstop and return every read response in full regardless of size.

This disclosure gate is deliberate. The old universal disclosure branch added 380 tokens to an empty ordinary `@mcp_tool` schema and 382 tokens to an auto-discovered action schema. On a production-shaped registry, removing that always-on schema contribution changed `tools/list` from 5,292 to 2,625 tokens, saving 2,667 tokens (50.4%). `@mcp_heavy` remains the preferred **explicit** mechanism for known-heavy tools that must negotiate instead of returning whole. See the [Read-Response Filtering](../Guide/read-response-filtering.md) guide and [ADR-005](../../ADR/adr-005-read-response-filtering.md).

> **Upgrade note.** Earlier releases shipped this setting as `None` (dormant), so the backstop only fired when an operator set a value. It now defaults to `25000`, so schema-disclosing heavy tools and dispatchers gain probe-first behavior for large responses on upgrade with no config change. To preserve always-full read responses, set it explicitly to `None`.

```python
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 25000  # bytes (this is also the shipped default)
# FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = None  # opt out: always return full read responses
```

---

### FRISIAN_MCP_USAGE_REPORTING

**Type:** `bool` (a recognized string token is also accepted)  
**Default:** `False`

Global default for the opt-in `_usage` token-reporting block attached to
successful dispatcher results. Ships **off**, so responses are byte-identical to a
build without the feature. When `True`, `_usage` is attached to successful
(`isError: false`) results unless a system policy or per-request flag overrides it
(see [`FRISIAN_MCP_USAGE_REPORTING_POLICY`](#frisian_mcp_usage_reporting_policy)).
Token counts use the pinned tiktoken `cl100k_base` encoding when the optional
`frisian-mcp[usage]` extra is installed, and a character-based approximation
otherwise. See the [Token Usage Reporting](../Guide/token-usage-reporting.md)
guide for the full contract and the precedence truth table.

Prefer a real boolean. A **string** value is accepted but parsed as a token, not
by Python truthiness: `"on"`/`"true"`/`"yes"`/`"1"` enable; `"off"`/`"false"`/`"no"`/`"0"`
and any unrecognized string resolve to **off**. This prevents a config-type typo
like `FRISIAN_MCP_USAGE_REPORTING = "false"` (a Python-truthy string) from silently
turning reporting on — the global layer parses tokens exactly as the per-request
flag does.

```python
FRISIAN_MCP_USAGE_REPORTING = True  # attach _usage to successful results by default
```

---

### FRISIAN_MCP_USAGE_REPORTING_POLICY

**Type:** `str` (`"allow"` | `"deny"`) or `None`  
**Default:** `None`

System-level policy for token usage reporting, checked with the **highest
authority** in the three-layer resolution (system policy → per-request flag →
global default):

- `"deny"` — forces reporting **off and locked**; a per-request flag can never
  re-enable it. Use this when `_usage` must not appear on a surface at all.
- `"allow"` — turns reporting on by default while still letting an individual
  request opt out.
- `None` (default) — defers to the per-request flag, then to
  `FRISIAN_MCP_USAGE_REPORTING`.

The policy must be the **exact string** `'allow'` or `'deny'` (case-insensitive,
surrounding whitespace tolerated). Any other value — a non-string such as
`b"deny"` or a list, or a dirty string — is not honored and defers to the lower
layers; it does **not** fail safe to deny. A startup system check,
**`frisian_mcp.W014`**, warns whenever this setting is set to such an unhonored
value, so a deny-intended misconfiguration is surfaced at boot instead of silently
failing open at request time. Fix the value (or unset it) to clear the warning.

Callers express a per-request preference at the transport level via the
`X-Frisian-MCP-Usage` header or the `usage` query parameter (header wins);
malformed values are treated as unset and can never enable the feature. See the
[Token Usage Reporting](../Guide/token-usage-reporting.md) guide for the full
truth table.

```python
FRISIAN_MCP_USAGE_REPORTING_POLICY = 'deny'  # authoritative: no _usage on this surface
```

---

### FRISIAN_MCP_USAGE_IN_CONTENT

**Type:** `bool` (a recognized string token is also accepted)  
**Default:** `False`

Global default for the **model-visible** usage line. By default the `_usage` block is a caller-side sibling of `content` that the model driving a standard MCP client never sees. When this is `True` (and reporting is otherwise enabled), the same usage numbers are also appended as a second `content` item — labeled JSON, `_usage: {…}` — so the agent can read and self-report its own cost. `content[0]` (the tool payload) is never modified.

Like `FRISIAN_MCP_USAGE_REPORTING`, this setting is parsed through the same shared boolean coercion: prefer a real bool, but a **string** value is interpreted as a token — `"on"`/`"true"`/`"yes"`/`"1"` enable; `"off"`/`"false"`/`"no"`/`"0"` and any unrecognized string resolve to **off** — so a config-confused `"false"` can never silently enable the line.

This is a **subordinate** surface: it has no `allow`/`deny` policy of its own and can never turn reporting on. It only chooses *where* usage appears within an already-enabled master decision, so a system-level `deny` (or reporting being off) suppresses **both** the sibling and the in-content line. Per-request override via the `X-Frisian-MCP-Usage-Content` header or `usage_content` query parameter (header wins). `result_tokens` still measures `content[0]` only — the appended line is not counted. See the [Token Usage Reporting](../Guide/token-usage-reporting.md) guide for the interaction matrix, a sample two-block response, and the forward note on MCP `_meta`.

```python
FRISIAN_MCP_USAGE_IN_CONTENT = True  # also surface a model-visible _usage: {…} content line
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

When `True`, `tools/list` is filtered per-request so each caller sees only the tools their identity is permitted to use, decided by `user.has_perm()` for each tool's underlying capability. (As of 1.1.x this replaced the earlier `get_all_permissions()` enumeration: `has_perm()` additionally honors superuser status and a host's view exemptions, and it matches the predicate the host's own queryset scoping uses, so a visible tool is also an invocable one.) Tools outside the caller's permission set are omitted entirely — they do not appear at any tier. On a per-route deployment (`FRISIAN_MCP_ROUTES`), this per-request filter composes with the route's structural allow/deny and tier ceiling.

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
```

> **`ExemptViewPermissionAdapter` is deprecated as of 1.1.0** and is now a no-op alias of `DjangoPermissionAdapter` — it emits a `DeprecationWarning` on instantiation. Permission-aware discovery derives capabilities from `has_perm()` directly, which honors a host's view exemptions natively, so a separate exemption adapter is no longer needed. Leave `FRISIAN_MCP_PERMISSION_ADAPTER` unset unless you have a genuinely custom adapter; the alias will be removed in a future minor.

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
| `mcp_doctor` | Audit the host's frisian-mcp integration end-to-end. Default pass runs eleven checks (INSTALLED_APPS, URL mounting, auth wiring, security settings, cache backend, performance hints, OAuth registration posture, authorize URL reachability, OAuth tier permissions, legacy PKCE redirect-tier-map, per-route surface audit). `--security` adds eight OAuth-specific security checks. Exits non-zero on errors. CI-pipeline usable. | [Guide → mcp_doctor](../Guide/mcp-doctor.md) |
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

For auto-discovered ViewSets, [`FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD`](#frisian_mcp_auto_negotiate_threshold) still observes response size, but plain auto-discovered actions do not publish the continuation call. An over-threshold response from one of those actions is returned complete. Register an explicit `@mcp_heavy` tool when a known-large read should negotiate.

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
- `/oauth/authorize/` — authorization endpoint (Authorization Code + PKCE)
- `/oauth/token/` — token endpoint (accepts the `authorization_code` and `client_credentials` grants)
- `/oauth/register/` — RFC 7591 dynamic client registration

`contrib.oauth` supports two grant types; a client uses whichever fits its shape:

- **Browser and OAuth-configured clients** (Claude.ai, and coding agents such as Claude Code when set up for OAuth) use the **Authorization Code + PKCE** flow: discover the metadata endpoint, register a client, send the user through `/authorize` for consent, then exchange the returned code at `/token` for a bearer token. This is the flow the [connection guide](../Guide/connect-agent.md) walks through per client. (Coding agents like Claude Code can alternatively use a static Bearer token — see the authenticator chain-ordering note above.)
- **Service-to-service clients** (no user, no browser) use the **`client_credentials`** grant: exchange the client's own credentials at `/token` directly.

Both flows land on the same bearer-token surface; the difference is only how the token is obtained.

---

## Common Patterns

### Brownfield: Existing Django App

The most common case. You have a Django app with DRF ViewSets. You want to make it agent-accessible without refactoring.

1. Install, add to `INSTALLED_APPS`, mount the endpoint
2. Set `FRISIAN_MCP_AUTODISCOVER = True` (default)
3. Add `@mcp_ignore` to any ViewSets not appropriate for agent consumption (admin panels, UI-specific endpoints)
4. Set `FRISIAN_MCP_API_KEYS` for initial access
5. Connect your MCP client

Your existing permissions, serializers, and URL structure all work as-is. frisian-mcp introspects your DRF routers, ViewSets, and serializers directly and builds MCP tool definitions from them dynamically — there is no OpenAPI schema generation step to add.

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
import hashlib
import logging
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger("myapp.mcp_auth")

MCP_PATH_PREFIXES = ("/mcp/", "/mcp")  # trailing-slash and bare endpoint; adjust to your FRISIAN_MCP_PATH

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
        # Log a one-way digest fragment, never the credential or any slice of it.
        token_id = hashlib.sha256(credential.encode()).hexdigest()[:12] if credential else "(empty)"
        logger.info("MCP %s %s — auth=%s token_id=%s len=%d (ip=%s ua=%r)",
                    request.method, request.path, scheme, token_id,
                    len(credential), client_ip, ua)
        return None
```

Register the middleware in `MIDDLEWARE` ahead of any auth or CSRF middleware so it sees the request before the token is touched. **Never log the credential or any prefix of it** — even a leading slice is secret material once it lands in centralized log aggregation. Log a one-way `sha256(credential)` fragment plus `len()`: that is enough to correlate a request against an admin record without putting recoverable secret bytes in the logs.

---

### SSE keepalive requires an ASGI worker class

frisian-mcp's MCP endpoints stream over SSE. The WSGI keepalive iterator (`src/frisian_mcp/views.py`) calls `time.sleep(min(15.0, remaining))` to hold the connection open, which ties up one sync worker for the lifetime of each MCP client connection. With sync gunicorn workers (`-k sync`, the default), N workers caps you at N concurrent MCP clients — the (N+1)th connection waits, then the worker pool starves.

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
Check that `FRISIAN_MCP_AUTODISCOVER = True` and your DRF router has ViewSets registered before the first request reaches the endpoint — discovery is deferred to the first request, so late router registration that still completes before that first call is fine. If using explicit registration, verify the dispatcher class is imported at startup.

**`WORKER TIMEOUT` loop after MCP client connects**  
Sync gunicorn workers cannot host SSE keepalive — every connection pins one worker until it times out. See [SSE keepalive requires an ASGI worker class](#sse-keepalive-requires-an-asgi-worker-class) above. Switch to `uvicorn.workers.UvicornWorker` (or plain `uvicorn`).

**404 on `/mcp/`**  
Verify the path is included in your root `urls.py`. Both trailing-slash (`/mcp/`) and non-slash (`/mcp`) variants should be tested — Django's `APPEND_SLASH` setting affects which resolves correctly. If running behind a reverse proxy (nginx, Caddy), confirm the proxy is forwarding the `/mcp/` path to gunicorn and not consuming it.

**Authentication errors on write operations**  
Confirm the caller's API key maps to `read_write` or `admin` in `FRISIAN_MCP_API_KEYS`, or that the OAuth token was issued with appropriate scope. Read-tier callers will not see write-tier tools in `tools/list` at all — if the tool is absent rather than returning a 403, the caller is authenticating below the required tier.

**Auto-discovery picks up ViewSets you don't want exposed**  
Add `@mcp_ignore` to the ViewSet class or to specific action methods. For large apps, it can be easier to set `FRISIAN_MCP_AUTODISCOVER = False` and use explicit `@mcp_dispatcher` registration for the surfaces you want to expose.

---
