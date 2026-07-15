# Nautobot: Per-Route Gateway Integration

**Audience:** Nautobot administrators deploying frisian-mcp with more than one tool surface
**Applies to:** frisian-mcp 1.1.0 · Nautobot 3.x · Django 5.x · Python 3.11+

---

## What this guide covers

The [base Nautobot install](../../installs/Django/nautobot/3.x/install.md) mounts a single MCP gateway at one path. This guide covers the **per-route permission model** (`FRISIAN_MCP_ROUTES`, new in 1.1.0), which mounts several independent gateway paths on the same Nautobot instance — each with its own permission-tier ceiling and its own allow/deny tool surface.

The pattern this guide ships is **three authenticated routes** — a read surface, a read-write surface, and a full-access admin surface — so you can hand a read-only agent a genuinely read-only endpoint while an operator keeps a separate full-surface endpoint, without running two Nautobot deployments.

For the route grammar itself (tiers, the deny-all baseline, the startup audit), see the [Per-Route Permissions guide](../../v1.1/Guide/per-route-permissions.md) and [ADR-010](../../ADR/adr-010-per-route-permission-model.md). This guide is the Nautobot-specific application of it.

> When `FRISIAN_MCP_ROUTES` is set, the package mounts **only** those routes. The single-mount `FRISIAN_MCP_PATH` no longer mounts a gateway; `FRISIAN_MCP_EXTRA_PATHS` and `FRISIAN_MCP_PROTECTED_PATH` are ignored with a startup warning. Choose one model or the other, not both.

---

## The three-route model

```python
# nautobot_config.py

INSTALLED_APPS.append("frisian_mcp")
INSTALLED_APPS.append("frisian_mcp.contrib.oauth")
INSTALLED_APPS.append("frisian_mcp.contrib.tokens")

# Hard authentication on every route. A global permission class applies to
# ALL routes verbatim — this is what makes the read route authenticated too,
# not just the write and admin routes.
FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]

# Shared surface for the two scoped routes: expose the operational apps, and
# carve out secrets material and the object-change audit trail so they never
# appear on anything but the admin route. Extend the allow list to the
# Nautobot apps and plugins you actually run.
_SCOPED_ALLOW = [
    "dcim", "ipam", "circuits", "tenancy",
    "virtualization", "golden_config", "dns", "extras",
]
_SCOPED_DENY = [
    "extras:secret",
    "extras:secretsgroup",
    "extras:secretsgroupassociation",
    "extras:objectchange",
]

FRISIAN_MCP_ROUTES = {
    # Read surface. Ceiling `read`: write actions are hidden from discovery
    # and refused at execution, regardless of the caller's own tier.
    "default": {
        "path": "api/mcp/read",
        "highest_tier": "read",
        "allow_list": list(_SCOPED_ALLOW),
        "deny_list": list(_SCOPED_DENY),
    },
    # Read-write surface. Same carved surface as the read route, but the
    # ceiling permits writes for a caller whose own token tier allows them.
    "elevated": {
        "path": "api/mcp/write",
        "highest_tier": "read_write",
        "allow_list": list(_SCOPED_ALLOW),
        "deny_list": list(_SCOPED_DENY),
    },
    # Full-surface admin route. The resources the scoped routes carved out
    # (secrets, the audit trail, user accounts) exist here and only here.
    "admin": {
        "path": "api/mcp/admin",
        "highest_tier": "admin",
        "allow_list": ["*"],
    },
}
```

A few things worth calling out about this block:

- **The three outer keys are fixed** — `default`, `elevated`, `admin`. They name the *tier slot*, not the path or the ceiling. A tier you omit is simply not mounted (pure absence). Any other outer key is a hard configuration error at startup.
- **The paths are independent siblings.** `api/mcp/read`, `api/mcp/write`, and `api/mcp/admin` share a parent but none is a prefix of another, so no proxy path-normalization can confuse a read door for the admin door. Prefer this over nesting a privileged path *under* a public one.
- **`users` is never in an allow list**, so account, API-token, and object-permission resources are absent from both scoped routes — not merely denied. Absence is byte-identical to a resource that was never registered (see [Per-Route Permissions → the deny-all firewall](../../v1.1/Guide/per-route-permissions.md#allow--deny-a-deny-all-firewall)).
- **The ceiling only narrows, never grants.** A `read_write` route ceiling does not give a read-only token write access; it caps everyone at read-write and lets an already-write-capable token through.

### The read-ceiling safety net (E004)

If you ever open the read route to anonymous callers (drop the global `IsAuthenticated` and set `FRISIAN_MCP_ALLOW_UNAUTHENTICATED = True`), the `default` route's ceiling **must** be exactly `read`. An anonymous-reachable `default` route with a ceiling above `read` is `frisian_mcp.E004` — a **FATAL** startup check, and the package refuses to boot rather than serve writes to the world. The authenticated posture above sidesteps this entirely, but the guard is there if you deliberately open the read door for a public showcase.

> **On an anonymous-reachable route, an invalid or expired Bearer behaves exactly like no Bearer.** Credentials are only *validated* where credentials are *required*. The authenticators return "not mine" on a token they don't recognize rather than rejecting the request, so on an open route an unrecognized or expired Bearer falls through to anonymous — a `read`-tier, public surface — instead of returning `401`. This is not an escalation (a bad token confers exactly anonymous privileges, and it never becomes a token-validation oracle: valid and invalid tokens produce the identical open-route surface), and the *same* token is correctly rejected with `401` on the authenticated routes. The only practical consequence is debuggability: an operator whose token has expired sees an empty/public surface on the open door, not a bad-auth signal. If you open the read door, document that for whoever connects to it.

---

## Permission-aware discovery on Nautobot

Turn on per-identity tool filtering so each agent's `tools/list` reflects the Nautobot `ObjectPermission`s its principal actually holds:

```python
FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY = True
```

**Leave `FRISIAN_MCP_PERMISSION_ADAPTER` unset.** The default `DjangoPermissionAdapter` resolves capabilities through `user.has_perm()`, which on Nautobot means the principal's real `ObjectPermission`s — the only capability source you want. (The old `ExemptViewPermissionAdapter` is a deprecated no-op in 1.1.0; see [Permission-Aware Discovery](../../v1.1/Guide/permission-aware-discovery.md). Do not set it.)

> **Do not set a global `EXEMPT_VIEW_PERMISSIONS` on a scoped deployment.** A wildcard view exemption grants `view_<model>` on every model to *every* authenticated principal — not just a guest. That silently defeats per-principal scoping on every route: a service account whose `ObjectPermission` covers DNS records only would suddenly see the entire read estate in discovery, capped by nothing but the route ceiling. If per-principal scoping is to mean anything, every principal must earn its capabilities from a real `ObjectPermission`. Nautobot's own default is `EXEMPT_VIEW_PERMISSIONS = []`; leave it there.

---

## Authentication chain

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

Two ordering rules that matter on Nautobot specifically:

1. **Nautobot's NTC `TokenAuthentication` must not appear in this list.** It consumes any `Bearer` header it sees and rejects frisian-mcp tokens with `AuthenticationFailed`. A Nautobot API token (`Authorization: Token <key>`) is therefore not usable at the MCP endpoint — use a frisian-mcp MCP token or an OAuth-issued Bearer.
2. **`FrisianMcpTokenAuthentication` comes before `OAuthTokenAuthentication`.** Both use the `Bearer` scheme; the first class in the chain shapes the `WWW-Authenticate` challenge on a 401. Tokens-first emits a bare `Bearer` challenge that static-token connectors (Claude Code, Codex, Gemini CLI) accept, rather than an OAuth challenge that would send them down a dynamic-registration path you have closed.

With the global `IsAuthenticated` above, an anonymous request to any of the three routes returns `401` with a `WWW-Authenticate: Bearer` challenge — never a 404 mask, on POST and on SSE (`GET`) alike.

---

## OAuth for browser-based agents

Install `frisian_mcp.contrib.oauth` (already in the `INSTALLED_APPS` above) so Claude.ai, ChatGPT, and Grok can connect via the OAuth 2.1 PKCE authorization-code flow.

```python
# Public origin — the LB-terminated URL of your Nautobot instance. No port if
# a reverse proxy terminates TLS on 443.
FRISIAN_MCP_OAUTH_ISSUER = "https://your-nautobot.example.com"

# Dedicated HMAC key so rotating Nautobot's SECRET_KEY does not invalidate
# every frisian-mcp token and client secret.
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
FRISIAN_MCP_HMAC_KEY = "replace-with-generated-secret"

FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1  # nginx hops in front of Nautobot

# Client lifecycle — locked down. The operator pre-registers each client in
# the Django admin and shares the client_id out of band.
FRISIAN_MCP_OAUTH_REGISTRATION_OPEN = False
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = False
FRISIAN_MCP_OAUTH_AUTO_APPROVE = False
FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION = "read"
```

Pre-register a client under **Plugins → frisian-mcp → OAuth Clients → Add**: choose a permission tier, attach the Django user the client's requests run as, and copy the one-time `client_id` / `client_secret` into the agent's connector settings, pointed at the route you want it to use (for example `https://your-nautobot.example.com/api/mcp/read/`).

**Token lifetime.** The package default is `FRISIAN_MCP_OAUTH_TOKEN_EXPIRY_SECONDS = 3600` — one hour. Set a longer value only if your rotation policy allows; there is no need to raise it for normal connector use.

> **Optional — consent-gated self-registration.** If you must let a client that cannot be handed a `client_id` out of band (e.g. the hosted Claude.ai connector) bootstrap itself, enable PKCE auto-registration in its *safe* form only: keep `AUTO_APPROVE = False` (a logged-in operator must click Allow), keep `REGISTRATION_OPEN = False`, keep the default tier at `read`, and pin the hosts that may auto-register:
>
> ```python
> FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER = True
> FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST = ["claude.ai"]
> ```
>
> An empty host allowlist fails closed. Never raise `PKCE_DEFAULT_PERMISSION`; promote a specific client to a higher tier in the Django admin and have it reconnect (token authority is fixed at issuance, so promoting a client does not widen tokens already issued).

### Route-aware discovery metadata

OAuth metadata discovery is route-aware in 1.1.0. When a client is challenged on one of the gated routes and follows the RFC 9728 → RFC 8414 cascade:

- **An anonymous-reachable route is never advertised as a protected resource.** Asking for its protected-resource metadata returns a 404 — the gateway does not describe a door it is not guarding.
- **The resource a client is told about is the route it was challenged from.** A 401 from `api/mcp/write` hands back `api/mcp/write`'s metadata (the RFC 9728 path-suffixed form), not some other route's.
- **The bare `/.well-known/oauth-protected-resource`** (for clients that ignore the `resource_metadata` pointer) resolves to the **lowest-privilege authenticated route**, so a client that will not say which door it wants is steered to the least-privileged one.

Keep `FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY = True` (the default) for any spec-compliant browser client — the metadata documents advertise endpoint URLs, not credentials, and `registration_endpoint` is only advertised when `REGISTRATION_OPEN` is `True` (which it is not here).

---

## Dispatch groups

Nautobot exposes roughly 1,900 ViewSet actions. Bundle them into topic-level dispatcher tools so an agent sees a handful of groups instead of thousands of flat tools. The group basenames follow DRF's convention — `Model._meta.object_name.lower()` — not URL slugs.

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    "dcim": [
        "device", "rack", "rackgroup", "rackreservation",
        "interface", "interfacetemplate", "cable",
        "location", "locationtype", "manufacturer", "devicetype",
        "platform", "inventoryitem",
        "consoleport", "powerport", "powerfeed", "powerpanel",
        "module", "modulebay", "moduletype",
        "virtualchassis", "softwareimagefile", "softwareversion",
    ],
    "ipam": [
        "ipaddress", "ipaddresstointerface", "prefix",
        "vlan", "vlangroup", "vrf", "routetarget",
        "namespace", "rir", "service",
    ],
    "circuits": [
        "circuit", "circuittype", "circuittermination",
        "provider", "providernetwork",
    ],
    "tenancy": [
        "tenant", "tenantgroup", "contact", "contactassociation", "team",
    ],
    "virtualization": [
        "cluster", "clustergroup", "clustertype",
        "virtualmachine", "vminterface",
    ],
    "dns": [  # nautobot-app-dns-models
        "dnsview", "dnszone", "nsrecord", "arecord", "aaaarecord",
        "cnamerecord", "mxrecord", "txtrecord", "ptrrecord", "srvrecord",
    ],
    "golden_config": [  # nautobot-app-golden-config
        "goldenconfig", "goldenconfigsetting",
        "compliancefeature", "compliancerule", "configcompliance",
        "configremove", "configreplace", "remediationsetting", "configplan",
    ],
    "extras": [
        "tag", "configcontext", "customfield", "customlink",
        "relationship", "dynamicgroup",
        "job", "jobresult", "scheduledjob", "webhook", "gitrepository",
        "role", "status", "secret", "secretsgroup",
        "note", "objectchange",
    ],
}
```

Extend or trim per the Nautobot apps and plugins you run. A group whose basenames match no registered ViewSet logs a startup warning and is skipped — including a group for an app you don't have installed costs nothing. Remember that the `deny_list` entries above (`extras:secret`, `extras:objectchange`, …) still carve those resources out of the scoped routes even though they appear in the `extras` group.

---

## Verify the deployment

Run the doctor after every config change:

```bash
nautobot-server mcp_doctor
```

On a per-route deployment the gateway check reports each mounted route rather than probing for the single-mount name:

```text
✓ MCP gateway mounted per-route at 3 path(s): api/mcp/read, api/mcp/write, api/mcp/admin
  (FRISIAN_MCP_ROUTES; the legacy frisian_mcp:gateway mount is intentionally absent)
```

The **per-route surface audit** (also run by `manage.py check`, and promoted to an error under `mcp_doctor --strict`) reports the findings a plain Django check structurally can't — most usefully `W009` (a *working carve-out*: the deny list removed tools and the route still exposes others), which you should expect on both scoped routes here because `_SCOPED_DENY` is doing its job. A `W008` (a route that resolves to *zero* tools) means an allow/deny list canceled itself out and that route serves nothing — fix it before deploying. See the [mcp_doctor guide](../../v1.1/Guide/mcp-doctor.md) and [Per-Route Permissions → startup audit](../../v1.1/Guide/per-route-permissions.md#startup-audit) for the full finding matrix.

`mcp_doctor --strict` has clean exit-code semantics: SOFT findings like `W009` do **not** fail it — only an error-level check, a LOUD finding, or an audit that could not run exits non-zero. That makes it safe to wire as a CI gate on your own Nautobot config; the package uses exactly this check as a required gate in its own pipeline.

---

## Connect an agent to a route

Each route is an ordinary MCP endpoint; point the client at the path whose ceiling matches the agent's job. A read-only analysis agent gets the read route:

```json
{
  "mcpServers": {
    "nautobot-read": {
      "type": "http",
      "url": "https://your-nautobot.example.com/api/mcp/read/",
      "headers": {
        "Authorization": "Bearer <frisian-mcp-token>"
      }
    }
  }
}
```

Mint the token under **Plugins → frisian-mcp → MCP Tokens → Add**, attaching the Django user whose `ObjectPermission`s should define that agent's surface. On the read route the agent's `tools/list` shows only `list`/`retrieve` actions for the resources its permissions cover; write actions are absent from discovery, not merely blocked at execution. See [Connect an Agent](../../v1.1/Guide/connect-agent.md) for per-client setup.

---

## Related

- [Per-Route Permissions](../../v1.1/Guide/per-route-permissions.md) — the route grammar, tiers, deny-all baseline, and startup audit
- [ADR-010: Per-Route Permission Model](../../ADR/adr-010-per-route-permission-model.md) — the design rationale
- [Permission-Aware Discovery](../../v1.1/Guide/permission-aware-discovery.md) — per-identity `tools/list` filtering via `has_perm()`
- [Nautobot 3.x Install](../../installs/Django/nautobot/3.x/install.md) — the base single-mount install and prerequisites
- [Security](../../v1.1/Security/security.md) — public-surface shaping, per-mount scoping, and hardened-posture guidance
- [mcp_doctor](../../v1.1/Guide/mcp-doctor.md) — the configuration audit command
- [Installation & Configuration Reference](../../v1.1/Reference/installation-configuration-reference.md) — every `FRISIAN_MCP_*` setting

---

*Document maintained alongside the frisian-mcp source.*
