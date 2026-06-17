# ADR 004: Write-Path Response Filtering via @mcp_light

**Category:** reference  
**Slug:** adr-004-write-path-response-filtering  
**Status:** Accepted  
**Date:** 2026-06-02

---

## Context

When an MCP agent executes a create, update, or destroy operation, the conventional DRF response echoes the full serialized object back in the response body. For a single-object create on a simple model, this is a few hundred tokens — acceptable.

The problem appears at bulk scale. A large Django application with full CRUD exposes bulk create and bulk update endpoints that accept and echo lists of objects. A 60-device bulk create in a production integration session produced a full echo response of ~10,798 tokens (43,190 bytes). At ~603 tokens per device, larger bulk operations scale linearly: a 200-device bulk create would produce roughly 36,000 tokens from the response alone.

Write operations are inherently sequential in many agent workflows. An agent provisions a set of devices, waits for confirmation, then moves to the next step (IP assignment, VLAN configuration, DNS registration). If each write step consumes tens of thousands of tokens from the context window, the agent's working budget for reasoning, retrieved state, and conversation history evaporates quickly. By the third or fourth bulk write in a session, the context window is gone.

This is the inverse of the read-path problem addressed by `@mcp_heavy`. On read paths, response size is unknown at call time because it depends on the number of matching records. On write paths, the agent already provided all the data — the echo is, by definition, a repetition of what the agent just sent. For the most common post-write use case (confirming success and continuing), the full echo is waste.

Three solutions were considered:

**Truncation** — return the first N fields of the serialized object. Simple, but arbitrary. Different models have different fields; there is no universal truncation point that is both informative and small.

**Write-only confirmation** — return only `{id, status_code}`. Minimal, but loses the URL and any server-assigned fields the agent might need (computed status, assigned IDs for nested objects). Agents that want to validate the write have no retrieval path.

**Lean envelope with optional full retrieval** — return a small set of identifying fields by default, but cache the full serialized result and provide a continuation token the agent can use to retrieve it. The agent that does not need the full echo pays zero cost. The agent that needs it uses the continuation token — no second write is executed.

The lean envelope approach is the right solution. It eliminates the default cost, preserves full access, and reuses existing infrastructure (`@mcp_heavy`'s cache layer) for retrieval.

## Decision

frisian-mcp implements write-path response filtering as a package-level default, applied automatically to all tools whose underlying ViewSet actions are create, update, partial_update, destroy, or any `@action` decorated with `methods=['POST', 'PUT', 'PATCH', 'DELETE']`.

**Default behavior (lean envelope):**

All write tools return a lean confirmation envelope without agent intervention. Three envelope shapes:

- **Single-object create/update:** `{id, url?, name?, status_code, data_size, continuation_token}`
- **Bulk create/update:** `{accepted, failed, status_code, data_size, continuation_token}`
- **Delete:** `{id, deleted: true, status_code}`

**Lean field extraction order:**

The envelope always includes `status_code`, `data_size`, and `continuation_token`. The identifying fields are extracted in priority order from the serialized object: `id`/`pk` → `url` → `name`/`display` → any fields annotated with `@mcp_light_key`.

**`@mcp_light_key` annotation:**

Host app serializers can annotate specific fields to ensure they appear in every lean envelope for that serializer, even when those fields are not the conventional `id`/`url`/`name` fields:

```python
from frisian_mcp.decorators import mcp_light_key

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

This is useful when the agent has reason to validate the full serialized result before continuing (e.g., confirming computed fields, checking nested relationships). The agent opts in per call; the default for all other calls remains lean.

**Continuation token and retrieval:**

The `continuation_token` in the lean envelope reuses `@mcp_heavy`'s existing cache infrastructure (the `_HEAVY_CACHE_PREFIX` key namespace). The agent retrieves the full object by calling the heavy-fetch path with `mode=full` and the continuation token. The write operation is not re-executed — the cached result is returned.

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

**Positive.** Write-heavy agent workflows no longer exhaust the context window on response echoes. The measured reduction for a 60-device bulk create is from ~10,798 tokens (full echo) to ~24 tokens (lean envelope) — a 99.8% reduction. The saving scales linearly with bulk size.

**Positive.** No agent changes required for existing workflows that do not inspect the full write response. The lean envelope is the new default; agents that only need confirmation of success already have what they need.

**Positive.** The `verify=True` opt-in preserves full access without a separate API. Agents that validate writes can do so per call with a single parameter, receiving the full response inline without a cache round-trip.

**Positive.** The continuation token reuses tested infrastructure. No new caching layer is introduced; `@mcp_heavy`'s cache machinery handles retrieval for both read and write paths.

**Negative.** Agents that have historically expected full echo responses on writes will receive lean envelopes instead. This is a behavior change for any agent implementation that parses the full write response body. The migration path is `verify=True` until the agent is updated to use the continuation token flow.

**Negative.** The `@mcp_light_key` annotation adds a non-standard meta attribute to serializer `Meta` classes. While it follows the existing Django pattern for serializer metadata, it is frisian-mcp-specific and will not be understood by tools that inspect serializers for other purposes.

**Negative.** The `data_size` field in the lean envelope reports bytes of the cached full response, not a parsed record count. For bulk operations, agents that want a record count must call the continuation token path to inspect the full response, or infer from `accepted` + `failed` in the bulk envelope.

The write-path token savings are material enough to justify the behavior change. Agents building infrastructure across large datasets — the primary use case for the large Django application integrations this package targets — cannot sustain multi-step workflows without this optimization.

## Validation

The 60-device bulk create measurement (10,798 tokens full echo → 24 tokens lean envelope, 99.8% reduction) was taken during a network automation integration session against a production system. Production device objects are approximately 3,800 bytes each (~603 tokens/device), measured from the same session. The 99.8% reduction figure holds at any bulk size because the lean envelope size is constant regardless of the number of objects written.

---

*ADR maintained alongside the frisian-mcp source. Architecture decision records capture the reasoning behind durable design choices for future maintainers and adopters.*
