# ADR-010: Per-route permission model — absence is the security boundary

**Date:** 2026-07-10
**Category:** adr
**Supersedes:** —
**Related:** ADR-002 (Dispatcher Pattern), ADR-003 (URL Auto-Registration), ADR-008 (Permission-Aware Tool Discovery), ADR-009 (Authorize-path inputs are never authority)

---

## 1. Context

Configuring read-only, read/write, and admin access *per route* was not
straightforward before v1.1 and was a barrier to integration. An adopter who
wanted "one path a coding agent can only read through, one path an operator can
write through" had to reach for host-side URL wiring and custom permission
classes. The package offered a single endpoint and a single tier ceiling.

The v1.1 model gives each route its own materialized tool surface. It is built
to three properties:

1. **Secure by default.** A route exposes nothing until the operator opens it
   deliberately. The baseline is deny-all.
2. **Reasons like a firewall.** `allow_list` opens the surface; `deny_list`
   carves back out of what was opened. Deny is evaluated after allow and wins.
3. **Absence is the security boundary.** A tool denied on a route does not
   *exist* on that route — at discovery and at invocation both — rather than
   existing and being rejected at call time.

The third property is the reason this ADR exists, so it is worth stating
precisely. A denied-but-mounted tool is a detectable surface. Its invocation
timing, its error shape, and the mere fact that the dispatcher knows its name
are all signals that tell a probing caller "this exists behind a filter; defeat
the filter." Building each route so that denied tools **never enter its
dispatcher** removes that surface. On a route where a tool is denied, asking for
it is indistinguishable from asking for a tool that was never registered
anywhere — same error bytes, same absence from every listing and count.

### 1.1 Composition with permission-aware discovery (ADR-008)

This model and `FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY` (ADR-008) are two
distinct mechanisms that **compose without overlapping**. Stating the boundary
here prevents a future change from building a second action-visibility system:

- **Route allow/deny — this ADR — is STRUCTURAL.** It runs first, at dispatcher
  build. It defines which tools *exist on a path at all*. This is the absence
  property.
- **Permission-aware discovery — ADR-008 — is PER-REQUEST.** Among the tools
  that exist on a route, it filters which *actions this token* may see (a
  read-tier token sees `list` / `retrieve`; write actions are hidden from
  `tools/list`, not merely blocked at execution).

A tool denied by the route filter never reaches permission-aware discovery,
because it was never in that route's dispatcher.

### 1.2 Why absence must be built, not checked at call time

A group dispatcher (ADR-002) compresses many tools behind one tool name. By
deliberate design its input schema does **not** enumerate the member resource
and action names — that is precisely what keeps a grouped schema small
regardless of how many tools it bundles. That design choice has a direct
consequence for this model: a route filter cannot lean on schema validation to
reject a denied resource, and a membership check bolted onto the *shared*
dispatcher closure at call time would make a denied resource **rejected but
still named** — the error path would echo the resource back — rather than
**absent**. The property that makes the dispatcher cheap is exactly why the
filter must remove a denied resource from the dispatcher's inputs at build time.
Absence is therefore established structurally, per route, at construction — not
by a gate evaluated on each call. §7 is the mechanism; this is its rationale.

---

## Decision

Worked examples below use neutral tool names (`catalog`, `item`, `order`,
`ping`) and neutral paths (`/mcp`, `/mcp/elevated`, `/mcp/admin`). These are
illustrative; the tier keys and their paths are operator-defined.

### 2. Tier model and canonical permission-tier names

`FRISIAN_MCP_ROUTES` maps three fixed tier **keys** — `default`, `elevated`,
`admin` — to route configs. The `path` for each is an operator-defined string;
the JSON is illustrative, not literal.

```python
FRISIAN_MCP_ROUTES = {
    "default":  {"path": "/mcp",          "highest_tier": "read",       "auto_discover": False, "auto_register": False, "allow_list": [], "deny_list": []},
    "elevated": {"path": "/mcp/elevated", "highest_tier": "read_write", "auto_discover": False, "auto_register": False, "allow_list": [], "deny_list": []},
    "admin":    {"path": "/mcp/admin",    "highest_tier": "admin",      "auto_discover": False, "auto_register": False, "allow_list": [], "deny_list": []},
}
```

Permission tiers are canonical and ordered: **`read` < `read_write` < `admin`**.
There are **no synonyms** — `readonly`, `rw`, `read-only`, and a bare `write`
are rejected, in code, config, *and docs*. `canonical_permission_tier()`
(`route_config.py`) raises `ImproperlyConfigured` on any non-canonical value at
config-parse time; the ordering re-exports the registry's
`_TIER_RANK = {"read": 0, "read_write": 1, "admin": 2}`, so it cannot drift from
the registry's own tier ranking.

The shipped defaults must not trip their own startup warnings, so the two auto-*
flags ship `False`. `highest_tier` is optional at the schema layer
(`RouteConfig.highest_tier: str | None`); its *resolution* is described in §8.

### 3. `auto_register` split into `auto_discover` + `auto_register`

The name `auto_register` previously carried two unrelated meanings. They are
split, because they warrant different startup severities and one flag cannot
carry two severities:

- **`auto_discover`** (tool discovery) — when `True`, newly discovered DRF
  ViewSets join this route's surface. This is the flag that interacts with
  `allow_list: ["*"]`. Secure default `False`; deviation is a SOFT startup
  finding.
- **`auto_register`** (client/agent self-enrollment) — the existing OAuth-style
  meaning (cf. `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER`, ADR-009). Secure default
  `False`; `True` on an anonymous-reachable route is a LOUD finding, because on
  an open route it means anonymous clients can self-enroll.

There was no pre-existing *per-route* `auto_register` to preserve, so the split
ships as a schema from day one — no shim, no silent alias. The only
`auto_register` that predates this work is the OAuth-layer setting, which is
untouched.

### 4. Route existence is pure absence

A tier **absent** from `FRISIAN_MCP_ROUTES` is **not mounted** — no URL, no
handler, nothing beyond the framework default. "Configured" means the tier is
present with a `path`. If an operator configures only `elevated`, then
`default` and `admin` do not exist.

When `FRISIAN_MCP_ROUTES` is unset entirely, the package mounts a single
implicit `__legacy_default__` view (`ceiling=None`, `allow_list=["*"]`,
`deny_list=[]`) that reproduces today's behavior exactly. The startup audit is
silent about the legacy view. Note the two distinct absences, which §8 relies
on: absence of the **setting** means legacy; absence of a **key** on a
configured route means the tier's secure default.

**When `FRISIAN_MCP_ROUTES` is set, the routes are the entire gateway surface.**
The legacy single-path mount (`_install_mcp_url`), `FRISIAN_MCP_EXTRA_PATHS`, and
`FRISIAN_MCP_PROTECTED_PATH` are **not mounted**; each legacy path setting that
is present-but-ignored is logged with a warning naming the route model as its
replacement. This precedence is a fail-closed security requirement, not an
ergonomic one: any legacy path would mount the **full unfiltered registry**
beside the deny-carved routes, and a caller reaching that path would void every
carve-out. An operator who includes `frisian_mcp.urls` directly in their own
URLconf is making an explicit choice and is left untouched; the precedence
governs only the settings-driven mounts. Operators debugging a "missing"
protected path under `FRISIAN_MCP_ROUTES` should expect it — that is the design.

### 5. Firewall semantics — deny-all baseline, allow then deny

The baseline is **deny-all**. Nothing is exposed on a route unless explicitly
allowed.

- `allow_list: []` → the route exposes nothing. **LOUD** (`frisian_mcp.W004`):
  the route is configured but will serve zero tools.
- `allow_list: ["*"]` → expose everything. `*` is **dynamic**: it means
  everything now *and* anything discovered later on a route with
  `auto_discover: True`. This is intentional and documented, so an adopter
  understands that a newly added endpoint auto-appears on a `*` route.
- `deny_list` is a **carve-out from** `allow_list`. Deny is evaluated after
  allow and overrides it; a tool in both is denied.
- `deny_list` **must not accept `*`** — it is circular against the deny-all
  baseline. `*` in `deny_list` is FATAL (`E101`) at parse.

Worked example — expose everything on the read route except the `order` group:

```python
"allow_list": ["*"], "deny_list": ["order"]
```

A caller with order-read permission (the per-token permission layer) hitting
this route still gets nothing for `order`: the order tools do not exist on this
route, so the permission layer has nothing to act on. Route-level absence is
authoritative over the permission layer.

### 6. Allow/deny grammar

Entries operate at two granularities, with `:` (colon) as the separator:

| Entry | Meaning |
|---|---|
| `catalog` | the whole dispatch group (dispatcher tool + all members) |
| `catalog:item` | one resource within the group; the rest of the group is unaffected |
| `catalog:*` | silently normalized to `catalog` — a redundant way to name the whole group |
| `*` (allow only) | every tool on the surface, dynamically |

Resolution of a bare entry (no colon): if it matches a group name it means the
whole group; otherwise it is treated as a flat tool name (greenfield surfaces
with no dispatch groups). A flat tool that shares a name with a group is
therefore addressable only as `group:resource` — the shadow rule.

The colon is unambiguous — it appears in neither group nor resource names.
`__` (double-underscore) is rejected (`E102`): it collides visually with Django
lookup syntax and with hint-key underscores. Anything beyond bare `*` and
`group:*` is **not** glob support; partial globs (`item*`, `cat_*`, `*item`)
parse as unmatchable and draw a SOFT finding rather than matching anything.

One matcher (`RouteMatcher`, `route_grammar.py`) covers both dispatch-group and
flat surfaces; on a flat surface the group level simply does not appear. The
public seam is `parse_lists(allow, deny, route_name=...) → RouteMatcher`,
`ToolSurface.build(...)`, and `matcher.select(surface) → frozenset[str]`
(computed as `allow_union − deny_union`, so denied entries are absent from the
result). `matcher.audit(surface, route_name=...)` returns
`Finding(severity, code, ...)` records for the startup audit. Grammar codes
occupy the `E1xx` / `W1xx` space: `E100`/`E101`/`E102`/`E103`/`E105` FATAL at
parse; `W110` (partial glob), `W111` (matched nothing), `W112` (unknown group),
`W113` (inert deny that leaves a same-named tool exposed) SOFT/LOUD at audit.

### 7. Per-route dispatcher construction — the `RouteView` seam

Each configured route owns an immutable `RouteView` (`route_views.py`) built
from `(registry_snapshot − deny) ∩ allow`. The process-scoped
`RouteViewRegistry` singleton `route_views` holds one per route; a per-route
`McpView` subclass stamps `request._mcp_route_view` and routes both handler
seams — `tools/list` → `route_view.list_tools(...)`, `tools/call` →
`route_view.dispatch(request, name, args)` — through the view. A dispatch miss
raises the same `ToolNotFoundError` shape a genuinely unknown tool raises (the
error-parity half of §9).

**Flat entries are shared by reference. Dispatcher entries are rebuilt per
route.** This distinction is the core structural decision (BLOCKER-2). A group
dispatcher's members live in a frozenset closed over at build time, and the
closure resolves against the global registry. If a route reused the shared
dispatcher entry, a route that denies `catalog:item` could still invoke `item`
through the shared `catalog` dispatcher — an invocation bypass. So for a group
surface, `RouteView.build(...)` constructs a **route-local** dispatcher entry
whose closed-over inputs are route-pruned. It may still resolve final tool calls
through the global registry; the pruned membership gate is what a denied name
can never get past. There is no second registry and no double-registration —
the rebuilt entry lives only inside the `RouteView`.

A rebuilt dispatcher entry must prune four inputs, or a denied resource leaks
through a different surface than the one just closed:

| Prune site | Leak if left unpruned |
|---|---|
| `tool_names` frozenset (membership gate + dispatch path) | **invocation bypass** — deny unenforced |
| `resource_prefixes` (the suggester / help enumeration input) | a denied resource is named back to the caller in an error string |
| the frozen `description` (`"… N tools across M resources …"`) | advertised-count leak (§9) |
| `group_tool_names` on the entry | downstream readers see global group membership |

Pruning `tool_names` fixes the hint dict and the help resource map **for free**,
because both are already comprehensions filtered against the closure's
`tool_names`. That is the payoff of building absence into the inputs once,
rather than guarding each derived surface separately. `advertised_counts` and
`hint_key_allow` on the `RouteView` are derived *from the rebuilt entry*, not
computed beside a shared one, so they cannot drift from `entries`.

**Rebuild atomicity.** A `RouteView` is immutable once built. A rebuild
constructs the new view fully and then swaps the pointer under a lock in a
single dict assignment — never mount-unfiltered-then-prune, never a two-phase
clear-then-repopulate. A request grabs its `RouteView` reference once, so a
rebuild that lands mid-request does not affect that request.

**Rebuild triggers.** Process start (appended to the existing deferred-discovery
pass) and, for a host with genuine runtime plugin loading, an explicit
`route_views.rebuild_all(...)` the custom backend calls as the last step of its
dynamic load. Under standard Django there is no runtime "a ViewSet appeared"
event, so there is **no watcher, no polling thread, no signal-driven refresh** —
building one would be building a reaction to an event that cannot fire.

**Fail-closed on a missing snapshot.** On the one path where deferred discovery
never fires (`AUTODISCOVER=False`), a route's view may be absent when the first
request arrives. `McpView.post()` builds the carved view at request time rather
than falling back to the global registry — falling back would drop the carve-out
and fail open. Fail-closed under uncertainty is the standing rule.

### 8. Effective-tier cap applies to discovery, not only execution

Enforcement uses **`min(token_tier, route_ceiling, FRISIAN_MCP_MAX_TIER)`**,
computed once in `McpView.post()` (via `_min_tier`, `route_views.py`) and stamped
on `request._mcp_effective_tier`. `min` is monotone — it can only narrow, never
widen. Every reader — discovery, invocation, audit logging, error messages —
reads that one attribute; `registry._resolve_request_tier` short-circuits on the
stamp, so nothing recomputes it. `_min_tier` fails closed on an unrecognized tier
string.

**Discovery must read the capped tier, not the raw token tier.** A write-capable
token on a `read`-ceiling route sees only `{list, retrieve}` in `tools/list`; it
is never *shown* a write action that then fails at invoke. Showing at discovery
and rejecting at invoke would be both an inconsistency and an existence leak.
Synthesized actions (e.g. `bulk_create`) respect the capped ceiling for free,
because synthesis runs inside the same tier-filtered schema build.

**An omitted `highest_tier` on a configured route never means uncapped.**
`resolve_route_ceiling(route)` (`route_views.py`) resolves it to the tier key's
secure default via `SECURE_DEFAULT_CEILING`: `default` → `read`, `elevated` →
`read_write`, `admin` → `admin` (the values the §2 example labels as secure
defaults). The decisive reason: if omission meant *uncapped*, an open `default`
route with no `highest_tier` would be strictly more dangerous than the FATAL
condition of §10 — anonymous callers at their full token tier — while sailing
past a check that only fires on a value literally *above* `read`. A config more
dangerous than the fatal one, and silent, is not acceptable. This resolution is
the cap layer's concern (`min`); the schema layer keeps `highest_tier` as
`str | None` and treats absent and explicit-`None` identically. The
`__legacy_default__` view (§4) is the one genuinely uncapped view, and it exists
only when `FRISIAN_MCP_ROUTES` is unset.

Where a global `FRISIAN_MCP_MAX_TIER` caps a route below its declared
`highest_tier`, the audit emits a SOFT finding (`frisian_mcp.W007`) — otherwise
an operator's `admin` route is silently inert and nothing says why.

The ceiling extends the absence property for free: when `request._mcp_max_tier`
is set, `registry.dispatch` converts a tier denial into the same
`ToolNotFoundError` an unknown tool raises, so a write action above a route's
ceiling is absent at *both* discovery and invocation — never shown then
rejected.

### 9. Absence invariants — error parity, counts, hints

"The tool does not exist on this route" holds only if every observable surface
agrees. Three leaks are closed and golden-tested:

- **Error parity.** Invoking a denied resource returns a response
  **byte-identical** to invoking a resource that never existed anywhere. The
  correct test holds the resource *name* constant and varies only the reason for
  its absence — denied-on-route-A versus never-registered — because a test that
  compares two *different* names (`item` vs `zzz`) compares two different
  interpolated strings and can pass while still leaking. See §14.4.
- **Advertised counts.** A dispatcher's description ("N tools across M
  resources") is computed from the route's filtered set, not the global
  registry. A read route that advertises 13 resources but lists 12 has leaked
  the 13th.
- **Hints.** Hint-dict entries for denied resources are filtered per route. A
  denied resource whose hint still appears in `action="help"` output is a direct
  leak — and an easy one to miss, because hints live in a separate dict from the
  resource tree. §7's `tool_names` pruning closes this by construction.

The error-enrichment path is the same hazard viewed from the other side. The
lite error hatch (`lite: true`) attaches a tool's `inputSchema` to a validation
error so a caller can self-correct; resolving that schema through the *global*
registry would attach a denied or tier-hidden tool's full input contract to its
own absence error. The enrichment path therefore resolves through the
route-visible entry (`_request_visible_entry`, `views.py`), so an absence error
never carries the absent tool's schema. Plain (non-route) mounts keep the global
lookup unchanged.

### 10. Startup config-audit — FATAL / LOUD / SOFT

A single startup audit pass grades findings by severity. FATAL conditions must
genuinely stop the process, not merely print a banner: they are raised through
Django's system-check framework at `Error` level and via `ImproperlyConfigured`
in `AppConfig.ready()`, so boot fails with a non-zero exit and
`manage.py check --deploy` surfaces them. A banner is human-visible dressing on
top of a real boot refusal, never the mechanism.

| Severity | Mechanism | Representative triggers (shipped codes) |
|---|---|---|
| **FATAL** | `checks.Error` + `ImproperlyConfigured`; boot fails | open-world `default` with `highest_tier` above `read` (`frisian_mcp.E004`); route schema invalid (`E005`); duplicate/overlapping or reserved-path collision (§11, `E203`/`E204`); a literal `AllowAny` (or unmodified subclass) on an `elevated`/`admin` route (`E006`, §12) |
| **LOUD** | prominent startup log (WARNING), mirrored to stdout under `FRISIAN_MCP_STARTUP_PRINT` | `allow_list: []` (`W004`); non-empty `allow_list` fully zeroed by `deny_list` (`W008`); `auto_register: True` on an anonymous-reachable route (`W005`); an inert `deny_list` entry that leaves a same-named tool exposed (`W113`); anonymous GET/SSE-reachable route (`W010`, §12); opaque permission class that cannot be statically proven auth-requiring on `elevated`/`admin` (`W011`, §12) |
| **SOFT** | logged, non-blocking (INFO) | working carve-out where survivors remain (`W009`); allow/deny entry matching no tool (`W110`/`W111`/`W112`); `auto_discover: True` (`W006`); a global `MAX_TIER` capping a route below its declared ceiling (`W007`) |

The empty-exposure rule: warn LOUD only when the net exposed set is empty
despite a non-empty `allow_list`. A carve-out that leaves survivors is a working
config — SOFT at most. The audit never warns on every allow/deny overlap, only
on a net-empty result. `audit_route_surface()` never raises; it is advisory and
wrapped so a defect in it cannot take down tool discovery or the gateway. The
grammar's own severities (`W110`–`W113`) are consumed verbatim from
`matcher.audit(...)` — one source of truth, never re-derived in the audit layer.

*All audit checks in this table are shipped: the config- and surface-time
audits (`E004`/`E005`, `W004`–`W009`, and the wrapped grammar `W110`–`W113`) and
the anonymous-reachability checks of §12 (`E006`, `W010`, `W011`).*

### 11. Path validation

"Overlap" means **exact match after slash-normalization**, not shared prefix.

| Comparison | Exact match | Prefix nesting |
|---|---|---|
| route vs route | FATAL `E203` | **legal** — `/mcp`, `/mcp/elevated`, `/mcp/admin` resolve by longest match |
| route vs reserved package path | FATAL `E204` | FATAL in both directions |

`/mcp` and `/mcp/` normalize to the same path; two tiers there is FATAL `E203`
(which ceiling applies is undefined). Shared-prefix nesting between tier paths
is legal and encouraged, so the audit must not flag it. A tier path that
exactly matches, nests under, or would *swallow* a reserved package path
(`oauth`, `.well-known`, the registration stub, and the healthcheck paths read
from settings at audit time) is FATAL `E204`, in both directions; a greedy mount
at `/` normalizes to the empty path and is FATAL as `E201`. Reserved-path
reservation is unconditional — not gated on whether OAuth is installed — because
gating would convert a boot-time config error into a silently shadowed token
endpoint the day an operator enables OAuth. Comparisons are on segment
boundaries, so `oauthx` does not collide with `oauth`. Path template braces
(`/mcp/{id}`) are FATAL `E202` — the mechanical defense of Deferral 1 (§16), not
an implementation of it.

### 12. Anonymous-reachability is a property of `(route, method)`

The anonymous-admin check cannot be decided for arbitrary custom permission
classes by name. It is defined concretely, and the predicate lives once beside
the resolver in `route_views.py`; the audit imports it rather than re-deriving:

- **FATAL** (`E006`) if an `elevated`/`admin` route's effective permission
  classes are anonymous-granting. "Anonymous-granting" is stronger than a
  literal `AllowAny` name check, which fails open: it is
  `issubclass(cls, AllowAny)` *and* `cls.has_permission is AllowAny.has_permission`.
  The identity clause keeps it honest — an unmodified anonymous grant is FATAL,
  while a subclass that genuinely overrides the gate falls through to opaque →
  LOUD, where it belongs.
- **LOUD** (`W011`) if classes on an `elevated`/`admin` route cannot be
  statically recognized as auth-requiring (opaque custom classes). Silent-pass
  is rejected; so is the naive "FATAL unless `IsAuthenticated` is present,"
  which false-positives on legitimate custom auth.

`route_effective_permission_classes(route)` resolves the classes: a non-empty
global `FRISIAN_MCP_PERMISSION_CLASSES` wins verbatim for every route; empty +
`default` → `[]`; empty + `elevated`/`admin` → `[IsAuthenticated]`. That last
rule is load-bearing — without it an admin route with no configured classes
would silently serve anonymous traffic and nothing else would catch it.

The conceptual correction that makes the two findings fall out cleanly:
**"anonymous-reachable" is a property of a `(route, HTTP method)` pair, not of a
route.** Under a partial-anonymous class such as `IsAuthenticatedOrReadOnly`,
`tools/list` and `tools/call` are JSON-RPC methods dispatched inside
`McpView.post()`; `POST` is not a safe method, so an anonymous caller is denied
at the permission layer before any tier logic runs — no enumeration, no
invocation, and `FRISIAN_MCP_UNAUTHENTICATED_TIER` is never consulted. What an
anonymous caller *does* reach is `McpView.get()` — the SSE keepalive, held open
up to `FRISIAN_MCP_SSE_MAX_STREAM_SECONDS`, each stream pinning a worker thread.
That is resource exhaustion, not disclosure. So there are two predicates and two
findings:

- `route_is_anonymous_reachable(route)` — the POST / tool surface. Feeds the
  FATAL and the `auto_register` LOUD.
- `route_is_anonymous_sse_reachable(route)` — the GET / SSE surface. Feeds the
  partial-anonymous LOUD (`W010`), which is emitted on **every** such route and
  is **never** conditioned on `highest_tier` — lowering a route's ceiling does
  not mitigate an anonymous-SSE stream, so conditioning the severity on the
  ceiling would mislead the operator about the real mechanism. It is suppressed
  under `FRISIAN_MCP_ALLOW_UNAUTHENTICATED`, the same acknowledged-open-demo gate
  the open-gateway warning uses, so the open `default` demo does not
  double-warn.

### 13. Audit-context logging seam

`McpView.post()` stamps `request._mcp_audit_context` once per request:
`route_name`, `route_path`, `route_ceiling`, `token_tier`, `effective_tier`,
`tool_name` (filled at dispatch), `resolve_decision`, and a request id. Every
existing `logger.*` call in the view layer that already carries `extra={...}`
splats this context; token value, auth header, and user identity are
deliberately excluded. No durable sink is built here — the seam is a read-only
contract a later audit-logging change consumes verbatim, so the permission
context is emitted rather than computed and discarded, and no re-instrumentation
is needed when the durable sink lands.

---

## 14. Consequences

### Positive

- **Absence is structural, not a runtime obligation.** Because a denied tool is
  pruned out of a route's dispatcher inputs at build time, every derived surface
  — invocation, discovery, counts, hints, help, suggester — is correct without a
  per-surface guard. There is no call site that has to remember to re-check the
  route.
- **The two mechanisms compose cleanly.** Route allow/deny (structural) and
  permission-aware discovery (per-request) never contend, because a route-denied
  tool never reaches the per-request layer (§1.1).
- **Secure defaults do not warn.** Shipped defaults (`auto_discover=False`,
  `auto_register=False`, `read` ceiling on `default`) trip none of their own
  startup findings; an operator sees a finding only for a real deviation.
- **Backwards compatible.** With `FRISIAN_MCP_ROUTES` unset, the implicit legacy
  view reproduces today's single-endpoint behavior, and the audit stays silent.
- **FATAL genuinely fails boot.** Config errors that matter are `checks.Error` +
  `ImproperlyConfigured`, visible to `manage.py check --deploy` with a non-zero
  exit, not a log line a container swallows.

### Negative / risks

- **A `*` route grows silently.** `allow_list: ["*"]` with `auto_discover: True`
  means a newly added ViewSet auto-appears on that route at the next process
  start. This is intentional and documented, but an operator who forgets it can
  be surprised by a new tool on an open route. The mitigation is to name groups
  explicitly on sensitive routes rather than relying on `*`.
- **Route paths are operator responsibility.** Shared-prefix nesting is legal
  and encouraged, so the audit cannot catch a *semantic* mistake in a path
  layout — only exact collisions and reserved-path shadows. An operator who
  intends `/mcp/admin` to be more restrictive than `/mcp` must still set its
  `highest_tier` and classes correctly; the router will not infer intent from
  the path.
- **The `min` cap can render a route inert.** A global `FRISIAN_MCP_MAX_TIER`
  below a route's declared `highest_tier` narrows the route silently; the SOFT
  `W007` finding is the only signal, and an operator who ignores INFO logs may
  not see why an `admin` route serves nothing.
- **Rebuild is process-start only.** A host with genuine runtime plugin loading
  must call `route_views.rebuild_all(...)` itself; the package does not detect
  such loads. A host that dynamically registers ViewSets and never triggers a
  rebuild will not see them on any route until the next process start.

### Neutral

- The per-route `McpView` subclass mirrors the existing protected-view pattern;
  hosts already on that pattern see a familiar shape.
- `RouteConfig.highest_tier` remains `str | None` at the schema layer even
  though an omitted value resolves to a secure default at the cap layer (§8);
  absent and explicit-`None` are indistinguishable in config by design.

## 15. Alternatives considered

- **Call-time filtering (tool present, rejects on invoke).** Rejected. It leaves
  a detectable surface — timing, error shape, name knowledge — that tells a
  probing caller the tool exists behind a filter. Pure absence is the stronger
  property and the one the model rests on. A membership check inside the shared
  dispatcher closure is the group-surface form of this alternative and is
  rejected for the same reason (§1.2, §7): it makes a denied resource rejected
  and named, not absent.
- **`deny_list` accepting `*`.** Rejected — circular against the deny-all
  baseline. FATAL `E101`.
- **Per-action route filtering (`resource_action`).** Deferred / dropped in
  favor of the per-token permission layer, which already does action-level
  visibility. Filtering actions by both route and permission creates an ugly
  interaction matrix and re-introduces an existence-leak surface. Route
  filtering stops at `group` / `group:resource` (§16, Deferral 3).
- **One-shot dispatcher compile at setup.** Rejected for `*` routes: `*` is
  dynamic and its dispatcher must rebuild on discovery change (process start),
  not compile once and freeze.
- **Principal-in-path as a security gate.** Rejected as interim security. A
  path-embedded id lives in logs, proxies, and history; it is not a credential
  (ADR-009's invariant, restated for this surface). See §16, Deferrals 1–2.
- **Glob support beyond bare `*` and `group:*`.** Rejected. Partial globs reopen
  the existence-leak surface; they parse as unmatchable and draw a SOFT finding
  instead (§6).

## 16. Deferred — out of scope for v1.1

These are deliberately not built. A follow-up must not read their absence as an
oversight and silently implement them.

1. **`{optional_principal_id}` path templating.** v1.1 paths are literal
   strings; template braces are FATAL `E202` (§11).
2. **Principal-to-identity binding.** A path segment is a routing label, never a
   credential, and is not matched against the authenticated identity. Elevated
   and admin routes are guarded in v1.1 by the host auth backend and the token's
   permission tier (the `highest_tier` ceiling plus DRF permission classes) —
   the same model already live on single-path deployments.
3. **Per-action route filtering (`resource_action`).** Route filtering stops at
   `group` / `group:resource`.
4. **Runtime discovery watcher / polling / filesystem watchdog.** "Rebuild on
   discovery change" means process start / app reload under standard Django
   (§7).
5. **Glob beyond bare `*` and `group:*` → `group` normalization.**
6. **404-on-auth-failure masking.** A mounted route returns `401` on bad auth
   while an unmounted path returns `404`, so a prober can learn a route *path*
   exists. This is standard web behavior and acceptable — the path is explicitly
   not a credential — and must not be "fixed" by returning 404 on auth failure,
   which would break the OAuth `WWW-Authenticate` challenge flow (§17, WI-8).
7. **Permission-tier synonyms.** Never accepted, anywhere (§2).

## 17. Watch items as acceptance criteria

These are enforced, not aspirational. Each maps to where it lives:

| # | Invariant | Where enforced |
|---|---|---|
| WI-1 | Absence holds across error shape, advertised counts, and hints | §7 prune sites; §9; golden tests |
| WI-2 | Effective tier caps DISCOVERY, not just execution | §8 |
| WI-3 | Anonymous-admin FATAL semantics (empty or anonymous-granting = FATAL; opaque = LOUD; not "require `IsAuthenticated`") | §12 |
| WI-4 | Rebuild atomicity — build fully, then swap | §7 |
| WI-5 | FATAL actually stops the process (`checks` + `ImproperlyConfigured`) | §10 |
| WI-6 | Grammar edge cases (`group:*` → `group`, `*` in `deny_list` rejected, `__` rejected, partial glob SOFT) | §6 |
| WI-7 | Path validation (exact-match FATAL, reserved shadow FATAL, shared-prefix nesting legal) | §11 |
| WI-8 | 401-vs-404 route-existence disclosure is an accepted non-leak | §16, Deferral 6 |

### 17.1 Verifying the absence invariant

The absence invariant is verified by holding a tool name **constant** and
varying only *why* it is absent — denied on one route versus never registered on
a bare registry — and asserting the two responses are byte-identical. A test
that instead compares two *different* names is a shape check, not a reachability
check: it can pass while the surface still leaks, because the two names produce
two different interpolated error strings by construction. This distinction —
shape is not reachability — is the standing rule for any future test of this
model.

## 18. References

- ADR-002 — The Dispatcher Pattern for Tool Surface Compression. The enum-free
  grouped schema whose token-efficiency property is why absence must be built,
  not checked at call time (§1.2).
- ADR-003 — URL Auto-Registration via `AppConfig.ready()`. The discovery
  mechanism whose process-start pass the per-route rebuild appends to.
- ADR-008 — Permission-Aware Tool Discovery. The per-request action-visibility
  layer that composes with, and does not overlap, the structural route filter
  (§1.1).
- ADR-009 — Authorize-path inputs are never authority. The "request inputs are
  never a credential" invariant, restated here for the path segment (§16,
  Deferrals 1–2).
- `../v1.1/Security/security.md` — threat model and recommended deployment
  patterns.
- `../v1.1/Reference/installation-configuration-reference.md` — the complete
  settings reference, where `FRISIAN_MCP_ROUTES` and the split auto-* flags
  land.
