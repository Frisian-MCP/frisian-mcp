# Per-Route Permissions

**Category:** guide  
**Slug:** per-route-permissions  
**Audience:** Operators configuring read / read-write / admin access on separate MCP routes  
**Since:** v1.1.0

---

## What this feature does

`FRISIAN_MCP_ROUTES` lets you mount several MCP endpoints — typically a
read-only route, a read/write route, and an admin route — each on its own path,
each exposing its own subset of tools at its own permission ceiling.

The organizing principle is **absence**: a tool that is not allowed on a route
does not *exist* on that route. It is missing from `tools/list` and from
`tools/call` on that path, exactly as if it had never been registered — not
present-but-rejected. A caller cannot tell a denied tool apart from one that was
never built.

For the design rationale behind that guarantee, see
[ADR-010](../../ADR/adr-010-per-route-permission-model.md). This guide covers how
to configure it.

---

## The config surface

```python
# settings.py
FRISIAN_MCP_ROUTES = {
    "default":  {"path": "/mcp",          "highest_tier": "read"},
    "elevated": {"path": "/mcp/elevated", "highest_tier": "read_write",
                 "allow_list": ["*"]},
    "admin":    {"path": "/mcp/ops",      "highest_tier": "admin",
                 "allow_list": ["*"], "deny_list": ["billing"]},
}
```

There are three fixed tier **keys** — `default`, `elevated`, `admin`. Each maps
to a route config with these fields:

| Field | Meaning | Default |
|---|---|---|
| `path` | Operator-defined mount path (any string) | required |
| `highest_tier` | Permission ceiling: `read` < `read_write` < `admin` | the tier key's secure default |
| `allow_list` | Tools/groups exposed on the route | `[]` (nothing) |
| `deny_list` | Carve-outs removed from `allow_list` | `[]` |
| `auto_discover` | Newly discovered ViewSets join a `["*"]` surface | `False` |
| `auto_register` | Client self-enrollment (OAuth walk-up) | `False` |

A tier key you do not list is **not mounted** — it does not exist. Configure only
`default` and only `/mcp` exists.

> **Avoid `admin` in the `path`.** A route mounted at `/mcp/admin` was refused
> client-side by ChatGPT, Claude.ai and Grok — before any request reached the
> server. Renaming the path, tier key unchanged, let all three connect. The tier
> key is `admin`; the path does not have to be.
>
> Other segments may behave the same way, and the server log is how you tell. A
> client refusing the path either sends nothing at all, or skips the challenge
> and fetches the bare `/.well-known/oauth-protected-resource` instead of the
> path-suffixed form — then binds to whichever route that document names. If you
> see either shape, change the path suffix.

### Permission tiers

Tiers are canonical and ordered: **`read` < `read_write` < `admin`**. There are
no synonyms — `readonly`, `rw`, `read-only`, and a bare `write` are rejected at
startup with `ImproperlyConfigured`. Use `read_write`, never `write`.

An omitted `highest_tier` on a configured route resolves to that key's **secure
default** (`default` → `read`, `elevated` → `read_write`, `admin` → `admin`) — it
never means "uncapped." The effective ceiling on any request is
`min(token tier, route ceiling, FRISIAN_MCP_MAX_TIER)`.

---

## Allow / deny: a deny-all firewall

The baseline is **deny-all**. A route exposes nothing until you open it.

- `allow_list: ["*"]` — expose everything. `*` is dynamic: it also covers tools
  discovered later on a route with `auto_discover: True`.
- `allow_list: ["catalog", "order"]` — expose only those groups/tools.
- `deny_list` — a carve-out evaluated **after** `allow_list`; a tool in both is
  denied. `deny_list` may **not** contain `*`.

Entries address tools at two granularities, separated by a colon:

| Entry | Selects |
|---|---|
| `catalog` | the whole `catalog` group (dispatcher + all members) |
| `catalog:item` | just the `item` resource within `catalog` |
| `catalog:*` | normalized to `catalog` (the whole group) |

On a flat surface with no dispatch groups, a bare entry matches a tool name
directly. Partial globs (`item*`, `cat_*`) are **not** supported — they match
nothing and draw a soft startup warning.

**Worked example — expose everything except one group on the read route:**

```python
"default": {"path": "/mcp", "highest_tier": "read",
            "allow_list": ["*"], "deny_list": ["billing"]},
```

The `billing` tools do not exist on `/mcp`. A caller who holds billing-read
permission at the token layer still sees nothing for billing here — route-level
absence is authoritative over the per-token permission layer, and the two
compose without overlapping (see
[Permission-Aware Discovery](permission-aware-discovery.md)).

---

## Startup audit

Route configuration is checked at startup. Misconfigurations are graded:

- **FATAL** (boot refused via Django system checks) — e.g. an open-world
  `default` route with a ceiling above `read`; a privileged (`elevated` /
  `admin`) route whose permission classes would admit anonymous callers; a route
  path that collides with another route or shadows a reserved package path.
- **LOUD** (prominent startup warning) — e.g. `allow_list: []` (the route
  exposes nothing); an `allow_list` fully cancelled by `deny_list`; an
  anonymous-reachable SSE surface.
- **SOFT** (logged, non-blocking) — e.g. an allow/deny entry that matched no
  tool; `auto_discover: True`; a global `FRISIAN_MCP_MAX_TIER` capping a route
  below its declared ceiling.

Run `python manage.py mcp_doctor` (see [mcp_doctor](mcp-doctor.md)) and
`python manage.py check --deploy` to surface these before deploying.

---

## What is intentionally *not* here

- **The path segment is not a credential.** A route path is a routing label; it
  does no authorization on its own. Elevated and admin routes are guarded by the
  host auth backend and the token's permission tier, exactly as on a single-path
  deployment.
- **A mounted route returns `401` on bad auth; an unmounted path returns `404`.**
  A prober can therefore learn a route path exists. This is standard web
  behavior and is not treated as a leak — do not attempt to mask it.

See [ADR-010](../../ADR/adr-010-per-route-permission-model.md) for the full model,
the absence invariants, and the alternatives that were rejected.
