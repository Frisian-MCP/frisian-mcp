# ADR 004: Write-Path Response Filtering via @mcp_light

**Category:** reference  
**Slug:** adr-004-write-path-response-filtering  
**Status:** Accepted  
**Date:** 2026-06-02

---

## Context

When an MCP agent executes a create, update, or destroy operation, the conventional DRF response echoes the full serialized object back in the response body. For a single-object create on a simple model, this is a few hundred tokens — acceptable.

The problem appears at bulk scale. A large Django application with full CRUD exposes bulk create and bulk update endpoints that accept and echo lists of objects. A 60-device bulk create in a production integration session produced a full echo response of ~10,798 tokens (43,190 bytes). At ~180 tokens per device, larger bulk operations scale linearly: a 200-device bulk create would produce roughly 36,000 tokens from the response alone.

Write operations are inherently sequential in many agent workflows. An agent provisions a set of devices, waits for confirmation, then moves to the next step (IP assignment, VLAN configuration, DNS registration). If each write step consumes tens of thousands of tokens from the context window, the agent's working budget for reasoning, retrieved state, and conversation history evaporates quickly. By the third or fourth bulk write in a session, the context window is gone.

This is the inverse of the read-path problem addressed by `@mcp_heavy`. On read paths, response size is unknown at call time because it depends on the number of matching records. On write paths, the agent already provided all the data — the echo is, by definition, a repetition of what the agent just sent. For the most common post-write use case (confirming success and continuing), the full echo is waste.

Three solutions were considered:

**Truncation** — return the first N fields of the serialized object. Simple, but arbitrary. Different models have different fields; there is no universal truncation point that is both informative and small.

**Write-only confirmation** — return only `{id, status_code}`. Minimal, but loses the URL and any server-assigned fields the agent might need (computed status, assigned IDs for nested objects). Agents that want to validate the write have no retrieval path.

**Lean envelope with optional full retrieval** — return a small set of identifying fields by default, but cache the full serialized result and provide a continuation token the agent can use to retrieve it. The agent that does not need the full echo pays zero cost. The agent that needs it uses the continuation token — no second write is executed.

The lean envelope approach is the right solution. It eliminates the default cost, preserves full access, and reuses existing infrastructure (`@mcp_heavy`'s cache layer) for retrieval.

## Decision

frisian-mcp implements write-path response filtering as a package-level default for write calls. Eligible actions include create, update, partial_update, destroy, or any `@action` decorated with `methods=['POST', 'PUT', 'PATCH', 'DELETE']` when routed through an MCP tool entry.

**Whether the envelope carries a `continuation_token` depends on what the caller's published schema discloses, and there are three outcomes rather than two.** See the 2026-08-24 amendment for the full contract; in summary, a write whose schema discloses continuation receives the envelope with a token; a write whose schema does not receives the same envelope *without* one, provided the envelope still lets the caller reach what was written; and where it would not, the full result is returned instead.

**Default behavior (lean envelope):**

Eligible write tools return a lean confirmation envelope without agent intervention. Three envelope shapes:

- **Single-object create/update:** `{id, url?, name?, status_code, data_size}`, plus `continuation_token` where the schema discloses it
- **Bulk create/update:** `{accepted, status_code, data_size}`, plus either `continuation_token` where the schema discloses it, or `ids` — the accepted objects' identifiers — where it does not
- **Delete:** `{id, deleted: true, status_code}`

**Lean field extraction order:**

The envelope includes `status_code` and `data_size` on create/update shapes, and `continuation_token` on those whose published schema discloses it. The identifying fields are extracted in priority order from the serialized object: `id`/`pk` → `url` → `name`/`display` → any fields listed in the serializer's `Meta.mcp_light_key`.

**`Meta.mcp_light_key` annotation:**

Host app serializers can annotate specific fields to ensure they appear in every lean envelope for that serializer, even when those fields are not the conventional `id`/`url`/`name` fields:

```python
from rest_framework import serializers

class DeviceSerializer(serializers.ModelSerializer):
    site_slug = serializers.SlugRelatedField(
        source='site', slug_field='slug', read_only=True
    )

    class Meta:
        fields = '__all__'
        mcp_light_key = ['site_slug', 'role']
```

Fields marked with `mcp_light_key` appear in the envelope in addition to the standard identifying fields.

**`verify=True` per-call override:**

The `verify` parameter is injected into every write tool's inputSchema automatically by the discovery backend. When an agent passes `verify=True` on a specific call, the full serialized object is returned directly in the response — no caching, no second call, no continuation token:

```json
{
  "resource": "device",
  "action": "create",
  "params": { "name": "edge-01", "site": "hq-1" },
  "verify": true
}
```

This is useful when the agent has reason to validate the full serialized result before continuing (e.g., confirming computed fields, checking nested relationships). The agent opts in per call; the default for other eligible write calls remains lean.

**Continuation token and retrieval:**

Where the envelope carries a `continuation_token`, it reuses `@mcp_heavy`'s existing cache infrastructure (the `_HEAVY_CACHE_PREFIX` key namespace). The agent retrieves the full object by calling the heavy-fetch path with `mode=full` and the continuation token. The write operation is not re-executed — the cached result is returned.

Where the envelope carries no token, the object remains reachable by two routes that are published and schema-legal on every write: `verify=True` on the original call, and a `retrieve` on the identifier the envelope carries. Nothing is cached on that path, so no entry is held for a token no caller could send back.

**Precedence:**

If a tool carries both `@mcp_heavy` and `@mcp_light` semantics (e.g., a custom action that reads and writes in one call), `@mcp_heavy` probe behavior takes precedence. `@mcp_light` applies only to pure write paths where `@mcp_heavy` is not in effect.

Read and list paths are unaffected. The `verify` parameter is a no-op on read tools.

**Implementation surface:**

- `backends/invocation.py` — `_extract_lean_envelope()` builds the confirmation envelope from the full serialized response
- `views.py` — strips `verify` before dispatch; routes lean vs. full response post-dispatch
- `backends/discovery.py` — injects `verify` schema param into write-action tools; sets `is_write=True` on ToolDefinition
- `backends/base.py` — `is_write: bool = False` field on ToolDefinition dataclass
- `registry.py` — `is_write` on `_ToolEntry` and `register()`
- `apps.py` — forwards `is_write` from ToolDefinition to registry at startup

## Consequences

**Positive.** Eligible write-heavy agent workflows no longer exhaust the context window on response echoes. The measured reduction for a 60-object bulk create was from ~10,798 tokens (full echo) to ~24 tokens (lean envelope) — a 99.8% reduction. That measurement predates the tokenless envelope; see the 2026-08-24 amendment for why the saving can no longer be stated as a single figure that holds at every batch size.

**Positive.** No agent changes required for existing eligible workflows that do not inspect the full write response. The lean envelope is the default on schema-disclosing write paths; agents that only need confirmation of success already have what they need.

**Positive.** The `verify=True` opt-in preserves full access without a separate API. Agents that validate writes can do so per call with a single parameter, receiving the full response inline without a cache round-trip.

**Positive.** The continuation token reuses tested infrastructure. No new caching layer is introduced; `@mcp_heavy`'s cache machinery handles retrieval for both read and write paths.

**Negative.** Agents that have historically expected full echo responses on eligible writes will receive lean envelopes instead. This is a behavior change for any agent implementation that parses the full write response body. The migration path is `verify=True`, which is available on every write regardless of what its schema discloses, until the agent is updated to read the envelope.

**Negative.** The `@mcp_light_key` annotation adds a non-standard meta attribute to serializer `Meta` classes. While it follows the existing Django pattern for serializer metadata, it is frisian-mcp-specific and will not be understood by tools that inspect serializers for other purposes.

**Negative.** The `data_size` field in the lean envelope reports bytes of the cached full response, not a parsed record count. For bulk operations, `accepted` is the count of objects written; an agent that needs more than a count reaches the full response by `verify=True` on the call, or by the continuation token where the envelope carries one.

The write-path token savings are material enough to justify the behavior change on eligible write paths. Agents building infrastructure across large datasets — the primary use case for the large Django application integrations this package targets — cannot sustain multi-step workflows without this optimization.

## Validation

The 60-device bulk create measurement (10,798 tokens full echo → 24 tokens lean envelope, 99.8% reduction) was taken during a network automation integration session against a production system. A standalone full device representation is approximately 3,800 bytes (~603 tokens); within the bulk echo the per-device cost is lower, about 720 bytes (~180 tokens), consistent with the 10,798-token / 60-device figure above. That figure is a record of what was measured on this fixture at the time, not a constant: see the 2026-08-24 amendment.

## Amendments

### 2026-08-24 — a write whose schema discloses nothing keeps the envelope and loses only the token

Status remains **Accepted**. This amendment records what the 2026-08-22 entry below no longer covers. That entry is left as written: it is dated evidence of what the contract was, and editing it in place would leave a reader unable to tell which parts survived.

**The 2026-08-22 entry remains true of the gate and is no longer true of the outcome.** Eligibility for a `continuation_token` is still derived from the published schema, exactly as it describes, and the invariant it states — that the package must not hand a caller a token its schema gives it no legal slot to return — is unchanged and still enforced. What has changed is what happens to a write that fails that gate. The earlier text left it implied that such a write returns whole; it now returns the same lean envelope with the token omitted.

**Why the earlier outcome was wrong.** The token was the only part of the envelope a schema-blind caller could not use. Everything else is ordinary data. Returning the whole serialised result to withhold one field cost a 60-item bulk create roughly 25 tokens to over seven thousand — a regression introduced by the gate rather than by the decision the gate protects.

**There are now three outcomes on the write path:**

1. **The published schema discloses continuation** — lean envelope including `continuation_token`, which redeems as before. Dispatcher-routed writes disclose through their outer entry and take this path.
2. **It does not, and the envelope still lets the caller reach what was written** — the same envelope with the token omitted. A single write carries its identifier; a bulk write carries `ids`, the identifiers of the accepted objects, in place of the token.
3. **It does not, and the envelope would not** — the full result is returned. A bulk result whose identifiers are not all resolvable falls back here too, not only one that carries none: a partial echo cannot be matched to the objects it names, which is worse for the caller than a large response.

Outcome 2 is only defensible because the object stays reachable without the token, and it does: `verify=True` is injected into every auto-discovered write schema, and the identifier in the envelope supports a `retrieve`. Both are published and schema-legal. Nothing is cached on that path.

**Consequence for the reduction figure.** The lean envelope is no longer a constant size. Where it carries `ids`, it grows with the batch, so the original claim that the 99.8% reduction "holds at any bulk size" is wrong in shape, not merely stale in its number. The saving on that path is properly stated as a bound rather than a fixed percentage: on the 60-item fixture the tokenless bulk envelope measured well under a fifth of the full echo. Outcomes 1 and 3 are unaffected.

### 2026-08-22 — write lean envelope now gates on published schema disclosure

Carried by branch `feat/post-1.1.0-hardening` as part of the post-1.1.0 hardening CR. Status remains **Accepted** — this narrows where the package-level write filter applies without replacing the lean-envelope decision.

The previous text said write-path response filtering was "applied automatically to all tools whose underlying ViewSet actions are create, update, partial_update, destroy..." That is no longer the complete contract. The flat write mint path now derives eligibility from the published schema, the same way the read-size backstop does: a write call receives the lean envelope only where the published schema discloses continuation. Dispatcher-routed writes still disclose through their outer entry and keep the lean envelope. The architectural invariant is that the package must not hand a caller a continuation token its published schema gives it no legal slot to return.

---

*ADR maintained alongside the frisian-mcp source. Architecture decision records capture the reasoning behind durable design choices for future maintainers and adopters.*
