# Permission-Aware Discovery

**Category:** guide  
**Slug:** permission-aware-discovery  
**Audience:** Developers configuring per-identity tool surface filtering in frisian-mcp

---

## What This Feature Does

By default, frisian-mcp exposes the same tool surface to every caller of a given permission tier. All callers at the `read` tier see the same read tools; all callers at `read_write` see the same read-write tools.

`FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` changes this: it filters `tools/list` so that each caller sees only the tools their **specific identity** is permitted to use, based on Django's standard permission interface. An agent whose identity has view permission on DNS records but nothing else receives a `tools/list` containing only DNS read tools. Tools for other systems do not appear.

This serves two related goals:

1. **Agent focus** — an agent assigned a narrow task works from a narrow surface. It does not need to reason about or navigate through operations unrelated to its task.
2. **Blast radius reduction** — out-of-scope tools are unknown to the agent, not merely forbidden. A compromised or prompt-injected agent cannot be steered toward operations that are not in its context.

> **Important:** This feature controls tool *visibility* (discovery), not *execution* enforcement. Read [Security Guidance](permission-aware-discovery-security.md) before deploying this feature in production.

---

## Enabling the Feature

Add to `settings.py`:

```python
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
```

This enables the filter with the default `DjangoPermissionAdapter`, which delegates to Django's standard `user.get_all_permissions()`. For backends that use `EXEMPT_VIEW_PERMISSIONS` semantics, use `ExemptViewPermissionAdapter` instead (see Built-In Adapters below).

By default, the feature is **off**. Default-off means upgrading installs see zero behavior change unless they explicitly opt in.

---

## How the Filter Works

On every `tools/list` request, frisian-mcp:

1. Resolves `request.user` (the authenticated identity for this request)
2. Calls `adapter.is_unrestricted(user)` — if `True` (e.g. superuser), all tools are returned with no filtering
3. Calls `adapter.get_capabilities(user)` — returns the set of `"app_label.action_model"` strings this user holds
4. Filters the tool registry: a tool is included only if the user holds the required permission for its content type and action

This adds one cached query per `tools/list` request. Subsequent capability checks are O(1) in-memory lookups. At 50 or 500 tools, the overhead is negligible.

### CRUD action mapping

Standard CRUD actions map automatically:

| ViewSet action | Permission required |
|---|---|
| `list`, `retrieve` | `app_label.view_<model>` |
| `create` | `app_label.add_<model>` |
| `update`, `partial_update` | `app_label.change_<model>` |
| `destroy` | `app_label.delete_<model>` |

Non-CRUD actions require explicit annotation (see `backend_action` below).

### Dispatcher visibility

Group dispatchers are filtered: a dispatcher group tool is shown only if the user holds at least one permission covering a resource in that group.

Plain class-based dispatchers (registered via `@mcp_dispatcher` without group configuration) are always visible — per-content-type filtering for class-based dispatchers is a V2 concern.

Custom `@mcp_tool` registrations (without model metadata) are always visible.

### Superuser behavior

Superusers bypass the filter and see all tools. This matches the behavior of most Django backends where superusers implicitly hold all permissions regardless of explicit assignments.

---

## Built-In Adapters

### `DjangoPermissionAdapter` (default)

Works for any project using Django's standard auth backend. Delegates to `user.get_all_permissions()`.

No configuration needed when `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True` — this adapter is used automatically.

### `ExemptViewPermissionAdapter`

For backends that mark certain models as globally readable via an `EXEMPT_VIEW_PERMISSIONS` setting. This adapter synthesizes `"app_label.view_<model>"` capabilities for those models so their corresponding tools appear in `tools/list` for all authenticated users, matching the implicit read-access semantics.

```python
FRISIAN_MCP_PERMISSION_ADAPTER = (
    "frisian_mcp.contrib.permissions.exempt_view_adapter.ExemptViewPermissionAdapter"
)
```

Supports both `"__all__"` (all installed models are view-exempt) and an explicit list of `"app_label.model_name"` strings.

---

## Custom Adapter

To integrate with a non-standard permission backend, implement the `PermissionAdapter` protocol:

```python
from frisian_mcp.contrib.permissions.base import PermissionAdapter

class MyPermissionAdapter:

    def get_capabilities(self, user) -> frozenset[str]:
        """
        Return frozenset of 'app_label.action_model' strings the user holds.
        Return an empty frozenset on error (fail closed, not open).
        """
        try:
            return frozenset(str(p) for p in user.get_all_permissions())
        except Exception:
            return frozenset()

    def is_unrestricted(self, user) -> bool:
        """Return True when the user should see all tools (e.g. superuser)."""
        return bool(getattr(user, "is_superuser", False))
```

Register it in settings:

```python
FRISIAN_MCP_PERMISSION_ADAPTER = "myapp.permissions.MyPermissionAdapter"
```

The adapter is loaded once at startup and called on every `tools/list` request.

---

## OAuth Configuration

Permission-aware discovery requires the OAuth token to resolve to a real Django user. `OAuthServicePrincipal` — the default OAuth identity when no user mapping is configured — holds no Django permissions and would produce an empty `tools/list`.

frisian-mcp raises a startup error (E002) if `contrib.oauth` is installed, `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` is enabled, and no OAuth user resolution is configured.

### Per-client user (recommended)

Each `OAuthClient` record has a **user** field in the admin. Set it to the Django user whose permissions should define that client's tool surface. This gives independent scoping per OAuth client.

```
OAuthClient "dns-agent"
  └─ user: dns_service_account

OAuthClient "device-agent"
  └─ user: device_service_account
```

Each agent sees and executes as its own scoped user.

### Global fallback (`FRISIAN_MCP_OAUTH_SERVICE_USER`)

When all OAuth clients should use the same execution identity:

```python
FRISIAN_MCP_OAUTH_SERVICE_USER = "mcp_service_account"
```

If neither per-client user nor the global fallback is set, startup check E002 fires.

---

## Startup Checks

### E002 — OAuth identity gap

**Trigger:** `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True`, `frisian_mcp.contrib.oauth` is installed, and no OAuth user resolution is configured (no per-client users and no `FRISIAN_MCP_OAUTH_SERVICE_USER`).

**Fix:** Set `FRISIAN_MCP_OAUTH_SERVICE_USER` in settings, or configure a user on each `OAuthClient` record in the admin.

### E003 — Unannotated non-CRUD action

**Trigger:** `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True` and a `@mcp_dispatcher` has a non-CRUD action without a `backend_action` annotation.

**Fix:** Add `backend_action` to the `@mcp_action` decorator (see below).

---

## `backend_action` for Non-CRUD Actions

Standard CRUD actions (`list`, `retrieve`, `create`, `update`, `partial_update`, `destroy`) map to Django permission verbs automatically. Custom actions do not — they require explicit annotation.

```python
from frisian_mcp.decorators import mcp_dispatcher, mcp_action

@mcp_dispatcher(name="network_device")
class NetworkDeviceDispatcher:

    @mcp_action(description="List devices.")
    def list(self, request, params):  # CRUD — no annotation needed
        ...

    @mcp_action(
        description="Run a diagnostics check on a device.",
        backend_action="view",  # maps to app_label.view_<model>
    )
    def diagnostics(self, request, params):  # non-CRUD — annotation required
        ...
```

Valid `backend_action` values are the Django permission verbs: `"view"`, `"add"`, `"change"`, `"delete"`, or any custom action string your backend supports. The adapter's `get_capabilities()` result is checked against `f"{app_label}.{backend_action}_{model}"`.

If `backend_action` is missing on a non-CRUD action and `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` is enabled, startup check E003 fires.

---

## V1 Scope and Limitations

- **Content-type + action granularity only.** Discovery filters at the model level, not the object level. An agent scoped to "devices in region X" sees device tools, not only region-X device tools. Object-level constraints are enforced automatically at execution time by the host backend's query restriction machinery.
- **Class-based dispatchers are not filtered.** Only group-based dispatcher tools participate in the capability filter. This limitation is documented in ADR-008 and will be addressed in V2.
- **Anonymous callers.** Anonymous users are not authenticated, so `get_capabilities()` returns an empty set under most auth backends. An anonymous caller will see no tools when `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` is enabled.

---

## Related

- [Permission-Aware Discovery — Security Guidance](permission-aware-discovery-security.md) — the discovery vs. execution gap, service account configuration, and production deployment
- [Dispatcher Pattern](dispatcher-pattern.md) — how `@mcp_dispatcher` and `@mcp_action` work
- [Installation & Configuration Reference](../Reference/installation-configuration-reference.md) — full settings reference for all `FRISIAN_MCP_PERMISSION_*` settings
