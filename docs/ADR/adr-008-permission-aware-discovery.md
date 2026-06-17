# ADR-008: Permission-Aware Tool Discovery

**Status:** Proposed
**Date:** 2026-06-05
**Category:** adr
**Supersedes:** —
**Related:** ADR-001 (Pluggable Backends), ADR-002 (Dispatcher Pattern), ADR-003 (URL Auto-Registration), ADR-006 (Repeated-Path Token Reduction)

---

## Context

frisian-mcp's tool surface is, by default, system-wide. Tier gating (read / read-write / admin) is a global ceiling: there is no way to express "this agent may read DNS records but only write device names, and should not see IPAM, Golden Config, or anything else." Every authenticated caller of a given tier sees the same surface.

This is a problem specifically for **agent** consumers. When an agent is assigned a scoped task — e.g. "reconcile DNS records against device names" — exposing the full tool surface creates three distinct risks:

1. **Scope creep.** The agent can act on systems unrelated to its task.
2. **Prompt-injection blast radius.** A compromised or manipulated agent can only damage what it can reach; a full surface maximizes that reach.
3. **Discoverability leakage.** An agent that can *see* IPAM tools may reason about, or be steered toward, using them — even if it shouldn't.

The desired behavior: an agent's discoverable tool surface is filtered to **exactly** the operations its identity is permitted to perform, and out-of-scope tools **do not appear at all**. The agent, without prior knowledge or external research, does not know those capabilities exist.

This must work across **all backends** frisian-mcp supports (ADR-001), not just Nautobot. Nautobot is the reference validation case because its ObjectPermission system is a mature, real-world per-object-type + per-action permission model; but the mechanism defined here is backend-agnostic and keys off Django's standard auth interface.

## Decision

### 1. Treat non-discoverability as a security property, not a convenience

A tool the caller is not permitted to use is **omitted from `tools/list` entirely** and is **refused at the dispatcher boundary** if invoked by name. This is the same enforcement point that already gates the read-only tier today. Hiding is not cosmetic; it is a deliberate reduction of attack surface and agent blast radius.

### 2. Authority is the auth backend, never frisian-mcp

frisian-mcp does **not** invent, store, or carry a permission scope. The token authenticates an identity (a user/principal); that identity's **existing permissions, as reported by the active auth backend**, define the surface. frisian-mcp reads those permissions and reflects them into discovery and enforcement. It adds no parallel permission model.

This is the load-bearing design choice. It is what makes the feature a *projection* of the host system's permission model rather than an *addition* to it — which is both architecturally correct (single source of truth) and adoption-critical (a host project's maintainers will reject anything that asks their system to reason about authorization a new way).

### 3. Capability is resolved via Django's standard permission interface

Discovery filtering keys off `user.get_all_permissions()` — Django's standard auth-backend method. Any backend that implements it (Nautobot's `ObjectPermissionBackend`, Django's default `ModelBackend`, or a custom backend) is supported with no backend-specific code in the discovery path.

The expected return shape is a mapping of `"<app_label>.<action>_<model>"` permission strings to optional constraint metadata. Discovery checks membership of the relevant `content-type + action` permission string in this mapping — an O(1) check after a single cached query. No object-level queries are performed at discovery time.

### 4. Backend adapter contract

Each backend integration provides an adapter exposing:

- **`get_capabilities(user) -> set[str] | dict[str, Any]`** — the permission strings the identity holds. Default implementation delegates to `user.get_all_permissions()`. Backends needing custom resolution override this.
- **`map_action(mcp_action: str) -> str`** — maps an MCP dispatcher action to the backend permission action string. Standard CRUD maps trivially (`list`/`retrieve` → `view`, `create` → `add`, `update`/`partial_update` → `change`, `destroy` → `delete`). Non-CRUD actions require explicit declaration (see §5).
- **`is_unrestricted(user, content_type) -> bool`** — returns `True` when the identity should bypass capability filtering for the given type (e.g. superuser, or a backend-defined exemption). Discovery and enforcement MUST consult this in addition to `get_capabilities()`. (See Consequences — the superuser caveat.)

Nautobot's adapter implements these against `ObjectPermissionBackend.get_all_permissions()`, the `f"{app_label}.{action}_{model}"` naming, and `is_superuser` / `permission_is_exempt()`.

### 5. Custom (non-CRUD) actions require explicit annotation

Backends generally do not validate that a custom action string corresponds to a real operation; the string is free-form. Therefore any non-CRUD dispatcher tool MUST declare the backend action it maps to:

```python
@mcp_dispatcher(..., backend_action="napalm_read")
```

Standard CRUD tools need no annotation; the adapter's `map_action()` default covers them. `backend_action` is the generic parameter name; the Nautobot adapter consumes it as the Nautobot action string (e.g. `napalm_read` → `dcim.napalm_read_device`).

### 6. Object-level constraints are honored automatically at execution — not at discovery

Discovery filters at **content-type + action granularity only** (V1 scope). Object-level constraints (e.g. "only devices in region X") are **not** evaluated at discovery time. They are enforced **automatically at execution** by the backend's existing query-restriction machinery, which the dispatcher already invokes. No additional frisian-mcp code is required for constraint enforcement on reads; we inherit it for free.

### 7. The OAuth identity MUST resolve to a real backend user (hard prerequisite)

This is the critical configuration gate.

API-token auth already resolves to a real backend user, so its permissions are authoritative with no extra config. **OAuth does not, by default.** Unless an OAuth-client → real-user mapping is configured (`FRISIAN_MCP_OAUTH_SERVICE_USER` in the Nautobot adapter, or the equivalent in another adapter), the OAuth path yields a lightweight service principal whose permission set is **empty** and whose access is tier-based only.

A feature that filters discovery against an empty permission set would show **nothing** — indistinguishable, to an operator, from the feature being broken; and worse, an operator could believe they have per-identity scoping while silently getting tier-based access.

**Decision: permission-aware discovery is a hard prerequisite on the identity resolving to a real backend user.** When the feature flag is enabled:

- If the authenticated identity resolves to a real backend user → permission-aware discovery is active.
- If it does NOT resolve (e.g. OAuth without the service-user mapping) → frisian-mcp MUST **fail loudly** — refuse to enable the feature, raising a clear configuration error at startup naming the missing mapping. It MUST NOT silently fall back to tier-based access.

**Rejected alternative — graceful fallback to tier-based access.** Considered and explicitly rejected. For a *security* feature, failing open is the wrong default: an operator who mis-configures the service user would believe an agent is scoped to "DNS-read, device-write" while it silently receives tier-based access. The flexibility is not worth an authorization control that can silently no-op. Fail loud, not open.

### 8. Opt-in, default off, no migration impact

A single feature flag (default `False`) governs the behavior. Default-off is byte-for-byte today's behavior: existing tokens, tiers, and discovery are unchanged; no migration is introduced; upgrading installs see zero behavior change unless they opt in. Enabling the flag is what introduces the per-identity discovery filter (and, per §7, the resolve-to-real-user precondition).

## Consequences

### Positive

- **Least privilege, properly.** An agent sees and can invoke exactly the operations its identity permits — nothing more.
- **Bounded blast radius.** Prompt-injection and scope-creep damage are limited to the identity's actual permissions, structurally rather than by trusting the agent to behave.
- **Single source of truth.** Authorization remains owned by the host system; frisian-mcp never diverges from or duplicates it.
- **Adoption-friendly.** Because it is a projection of the host's permission model and uses the host's own resolution methods, it is reviewable as "MCP made permission-aware against our model," not "a new permission system bolted on."
- **Cheap at discovery.** Single cached permission query per request; O(1) per-tool capability check. No object scans at `tools/list` time.
- **Free object-level safety on reads.** Constraint enforcement is inherited from the backend's existing query restriction; no extra code.
- **Realizes the stated differentiator.** This is auth-graph-derived, permission-aware discovery — the architectural moat, made concrete.

### Negative / risks

- **Superuser / exemption caveat (implementation requirement).** A backend may grant access via a path *other* than enumerated permissions — e.g. a superuser with no explicit permissions, or a view-exempt model. `get_capabilities()` alone will under-report these and would wrongly hide tools. Discovery MUST also consult the adapter's `is_unrestricted()` per content-type to match the backend's actual enforcement. Failure to do so makes superusers see an empty surface.
- **Custom-action mapping is manual.** Non-CRUD tools require correct `backend_action` annotation. A wrong or missing annotation mis-gates the tool (hidden when it shouldn't be, or shown when it shouldn't). Mitigated by defaulting CRUD automatically and surfacing unmapped non-CRUD actions in a startup check.
- **OAuth prerequisite is operational friction.** Per §7, OAuth deployments must configure the service-user mapping before the feature does anything. This is intentional (fail-loud over fail-open) but is a documentation and onboarding burden; the startup error must name the exact missing setting.
- **Public contract surface.** `backend_action`, the feature flag name, and the adapter method signatures become public API once shipped. They must be right before release; renaming later is a breaking change.

### Neutral

- frisian-mcp remains opinionated on *enforcement mechanics* (where discovery filters, where the dispatcher refuses) and unopinionated on *administration* (who assigns permissions remains the host system's concern, unchanged).
- V1 deliberately scopes to content-type + action. Object-level discovery filtering is a possible future increment but is explicitly out of scope here; constraint enforcement at execution already provides object-level safety on reads.

## Backend validation reference (Nautobot)

The Nautobot adapter validated the generic contract above against source:

| Contract element | Nautobot mechanism |
|---|---|
| `get_capabilities()` | `ObjectPermissionBackend.get_all_permissions()` — single cached query, no object touch |
| permission string form | `f"{app_label}.{action}_{model}"` (e.g. `dcim.napalm_read_device`) |
| `is_unrestricted()` | `user.is_superuser` OR `permission_is_exempt()` (view-action only; `EXEMPT_VIEW_PERMISSIONS`) |
| constraint enforcement | `RestrictedQuerySet.restrict(user, action)` applies constraints automatically at execution |
| OAuth resolution gate | requires `FRISIAN_MCP_OAUTH_SERVICE_USER`; otherwise `OAuthServicePrincipal` with empty permission set |

Other backends (default Django `ModelBackend`, custom backends) satisfy the contract by implementing the adapter methods against their own equivalents.

## Open items for implementation

1. Final names: the feature flag, the `backend_action` decorator parameter, and the adapter method signatures (these become public contract).
2. Startup validation: when the flag is on, verify (a) the OAuth identity resolves to a real user where applicable, and (b) every registered non-CRUD dispatcher tool has a `backend_action` mapping; fail loud with actionable errors otherwise.
3. Whether `is_unrestricted()` needs a per-action signature (Nautobot's exemption is view-only) or whether per-content-type is sufficient for V1.

