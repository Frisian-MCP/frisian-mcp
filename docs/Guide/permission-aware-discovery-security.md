# Permission-Aware Discovery — Security Guidance

**Category:** guide  
**Slug:** permission-aware-discovery-security  
**Audience:** Operators deploying frisian-mcp with `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` enabled

---

## The Two-Layer Model

frisian-mcp separates tool access into two distinct layers that operate independently:

**Discovery** — what tools appear in `tools/list`.  
**Execution** — who the underlying REST calls run as when a tool is invoked.

`FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` operates on the **discovery layer only**. It filters which tools appear in `tools/list` based on the authenticated user's permissions. It does not change who the REST calls execute as. Discovery and execution are governed by different settings.

This distinction matters because it is easy to read the feature name and assume it provides end-to-end permission enforcement. It does not. Operators who misconfigure the execution identity while relying on discovery scoping for security are in a false-safe position.

---

## What `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` Does

When enabled, this feature filters `tools/list` so that each caller sees only the tools their identity is permitted to use. An agent assigned a narrow task — say, reading DNS records — receives a `tools/list` that contains only DNS read tools. Tools covering other systems do not appear at all.

This is a **UX and agent-focus property**, not a security enforcement mechanism:

- An agent scoped to "DNS read" sees a clean, relevant tool surface rather than the full API.
- That agent cannot accidentally call device-write tools because those tools are not in its context.
- Prompt-injection steering toward out-of-scope operations is structurally limited: the agent has no tool names to be steered toward.

These are genuine security benefits. But the execution layer — what actually happens when a tool is invoked — is a separate concern.

---

## The Execution Identity

When a tool is called, frisian-mcp builds a synthetic request and forwards it to the host application's ViewSet. The user that synthetic request runs as is the **execution identity**. That user's credentials determine what the REST layer actually allows.

There are two paths:

### Authenticated callers (OAuth tokens, Bearer tokens)

For callers who present a valid credential, the execution identity is `request.user` as resolved by the authentication backend. For OAuth callers:

- If the `OAuthClient` record has a **user** field set (recommended), the execution identity is that specific Django user.
- If `FRISIAN_MCP_OAUTH_SERVICE_USER` is set globally, the execution identity is that Django user.
- Otherwise, the execution identity is `OAuthServicePrincipal` — a lightweight stand-in with tier-based access but no host-app object permissions.

### Anonymous callers

For callers who present no credential, execution runs as `AnonymousUser` unless `FRISIAN_MCP_SERVICE_ACCOUNT_USER` is configured, in which case execution runs as that named Django user.

---

## The Gap: Discovery Scope vs. Execution Scope

`FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` and the execution identity are configured separately. If they are not aligned, an operator can create a situation where an agent sees a narrowly scoped tool surface but executes with broad permissions.

**Example of the gap:**

```python
# settings.py — DANGEROUS CONFIGURATION
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
FRISIAN_MCP_OAUTH_SERVICE_USER = "admin"  # discovery and execution both as admin
```

In this configuration, the OAuth caller's `request.user` is the `admin` account. The discovery filter runs against admin's permissions — which include everything — so `tools/list` returns the full tool surface. Execution also runs as admin. The discovery feature provides no scoping at all.

**A more dangerous variant:**

```python
# settings.py — ALSO DANGEROUS
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
FRISIAN_MCP_SERVICE_ACCOUNT_USER = "admin"  # anonymous callers execute as admin
FRISIAN_MCP_UNAUTHENTICATED_TIER = "read_write"
```

Here, anonymous callers receive `read_write`-tier tools in discovery (filtered by `AnonymousUser` permissions, which may be broad under some auth backends) and execute all REST calls as the admin account. Any caller with network access to the MCP endpoint has admin execution rights.

---

## Production Guidance

### `FRISIAN_MCP_SERVICE_ACCOUNT_USER = "admin"` is only safe on isolated networks

Setting the service account to an admin user is appropriate for:

- Local development environments with no sensitive data
- Air-gapped demo or test networks where all callers are trusted
- Troubleshooting sessions on isolated hosts

It is **not** safe on any instance where:

- Multiple users or agents connect with different trust levels
- The instance holds real data with access constraints
- The MCP endpoint is reachable from outside a controlled network

On shared or production instances, the service account must be a non-admin user whose own permissions reflect the minimum access required.

### Align the execution identity with the discovery filter

For permission-aware discovery to provide meaningful security properties, the execution identity must be the same user whose permissions are used for the discovery filter. The mechanism to achieve this is the per-client user mapping.

**Recommended: Per-client user on `OAuthClient`**

Each OAuth client record has a `user` field. Set it to a Django user whose permissions match exactly what you want that client to see and do.

```
OAuthClient "dns-agent"
  └─ user: dns_service_account  ← has view_dnsrecord, no write, no other models
```

With this configuration:
- `request.user` = `dns_service_account` (set by the authentication backend)
- Discovery filter: runs against `dns_service_account`'s permissions → shows only DNS read tools
- Execution: REST calls run as `dns_service_account` → host app enforces `dns_service_account`'s permissions

Discovery scope and execution scope are the same user. The security property is real.

**Acceptable: Global `FRISIAN_MCP_OAUTH_SERVICE_USER`**

If all OAuth clients should resolve to the same execution identity, set `FRISIAN_MCP_OAUTH_SERVICE_USER` to a non-admin user whose permissions represent the minimum required for the exposed tool surface. This applies a single execution identity to all OAuth callers; for finer granularity, use per-client users.

### Verify alignment with `mcp_doctor`

After configuring permission-aware discovery, run:

```bash
python manage.py mcp_doctor
```

The doctor checks will flag common misconfigurations — including the case where `contrib.oauth` is installed but no user identity is configured for OAuth callers.

---

## Summary

| Property | Governs | Setting |
|---|---|---|
| What tools appear in `tools/list` | Discovery (visibility) | `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`, `FRISIAN_MCP_PERMISSION_ADAPTER` |
| Who REST calls execute as (OAuth) | Execution (enforcement) | `OAuthClient.user` (per-client) or `FRISIAN_MCP_OAUTH_SERVICE_USER` (global) |
| Who REST calls execute as (anonymous) | Execution (enforcement) | `FRISIAN_MCP_SERVICE_ACCOUNT_USER` |

`FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` is a **UX and agent-focus feature**. It limits what an agent can discover and therefore what it can be steered toward. It does not replace proper execution identity configuration.

Execution enforcement is governed by the host application's permission system (DRF permission classes, object-level restrictions). frisian-mcp passes the resolved `request.user` through to the host app on every tool call. The host app decides what that user can actually do.

---

## Related

- [Permission-Aware Discovery — User Guide](permission-aware-discovery.md) — setup, adapters, and configuration
- [Security-First MCP Architecture](../Security/security.md) — path separation and deployment patterns
- [Installation & Configuration Reference](../Reference/installation-configuration-reference.md) — full settings reference
