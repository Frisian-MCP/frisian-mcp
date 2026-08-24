# Write-Path Response Filtering with @mcp_light

**Category:** guide  
**Slug:** write-path-response-filtering  
**Audience:** Developers annotating ViewSets and serializers for production MCP use

---

## What @mcp_light Does

`@mcp_light` is frisian-mcp's write-path response filter. It changes the default MCP response for create, update, and destroy operations from a full serialized object echo to a lean confirmation envelope: a small set of identifying fields plus metadata that lets the agent retrieve the full object if needed.

The lean envelope is the default for write calls — no agent action required, and no host-app decorator either.  An agent that only needs confirmation of success receives exactly that, at a fraction of the token cost of a full echo.  An agent that needs the full serialized result can always request it per call using `verify=True`; where the envelope carries a `continuation_token`, it can also be retrieved with that, without re-executing the write.

Whether the envelope carries a token depends on what the calling tool's published schema discloses, because the package never hands a caller a field its schema gives it no legal slot to return. That produces three outcomes rather than two, set out under [The Lean Confirmation Envelope](#the-lean-confirmation-envelope) below.

The feature is package-level by design (see [ADR-004](../../ADR/adr-004-write-path-response-filtering.md)): it applies where a write call is routed through an MCP tool entry, with no `@mcp_light` decorator to import or apply.  Standard DRF clients calling the same ViewSet directly receive the conventional full-echo response; only eligible MCP-routed write calls receive the lean envelope.

---

## Why Write-Echo Is a Problem

When an agent creates or updates an object, the conventional response is the full serialized object. For a single-object create on a simple model, this is a modest number of tokens. For bulk operations, the cost scales with the number of objects written.

A 60-device bulk create in a production integration session produced a full echo response of approximately 10,798 tokens (43,190 bytes) — about 180 tokens per device within the bulk echo. Larger bulk operations scale linearly: a 200-device bulk create would produce roughly 36,000 tokens from the echo alone, before the agent has done anything with the result.

Write operations are often sequential in agent workflows: provision a batch of devices, then assign IP addresses, then configure VLANs, then register in DNS. Each step produces an echo. By the third or fourth bulk write step, the context window is significantly depleted — not from reasoning or retrieved state, but from echoes of data the agent already sent.

See [The Token Problem](the-token-problem.md) for the full analysis, including Problem 3.

---

## The Lean Confirmation Envelope

The lean envelope is returned by default for write operations. Its structure depends on the operation type, and on whether the calling tool's published schema discloses the continuation call.

**Three outcomes.** A write whose schema discloses continuation receives the envelope with a `continuation_token`. A write whose schema does not receives the same envelope with the token omitted, provided what remains still lets the caller reach the written object — its identifier on a single write, the accepted `ids` on a bulk one. Where it would not, the full serialized result is returned instead, because a confirmation the caller cannot act on is worse than a large response. Dispatcher-routed writes disclose through their outer entry and always take the first path.

**Single-object create or update:**

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

Where the schema does not disclose continuation, the same envelope is returned without the `continuation_token` key. The `id` is what makes that envelope usable, so a serialized result carrying no identifier falls back to the full response instead.

**Bulk create or update:**

```json
{
  "accepted": 60,
  "status_code": 201,
  "data_size": 43190,
  "continuation_token": "<token>"
}
```

Where the schema does not disclose continuation, the token is replaced by the accepted objects' identifiers:

```json
{
  "accepted": 60,
  "status_code": 201,
  "data_size": 43190,
  "ids": ["abc123", "def456", "..."]
}
```

`accepted` alone would tell the caller that 60 objects exist and let it name none of them, so the identifiers are what make the tokenless bulk envelope usable. If they cannot all be resolved, the full result is returned rather than a partial list — a partial echo cannot be matched to the objects it names.

**Delete:**

```json
{
  "deleted": true,
  "status_code": 204
}
```

**Field extraction order for single-object envelopes:**

The identifying fields are extracted from the full serialized response in priority order: `id`/`pk` first, then `url`, then `name`/`display`, then any fields listed in the serializer's `Meta.mcp_light_key`. `status_code` is included on every lean envelope. `data_size` is included on create, update and bulk envelopes only, as is `continuation_token` where the schema discloses it — a delete envelope carries `deleted` and `status_code` and nothing else, so clients must not require those fields on a delete response. **Clients must not require `continuation_token` on any write response**, since a write whose schema does not disclose it will not carry it.

---

## Retrieving the Full Object

There are two routes to the full object after a write, and which are available depends on the envelope.

**`verify=True` works on every write.** It is injected into every auto-discovered write schema, so it is always a legal argument, and it returns the full serialized object inline on the original call.

**A `continuation_token`, where the envelope carries one**, reuses the existing `@mcp_heavy` cache infrastructure: the full serialized result is cached server-side and the token encodes the cache key. To retrieve the full object, the agent calls the heavy-fetch path with `mode=full` and the token. The write is not re-executed — the cached result is returned.

Where the envelope carries no token, nothing is cached, and the object is reached with `verify=True` on the call or a `retrieve` on the identifier the envelope carries.

This pattern is useful when:

- An agent needs to inspect computed fields that the server assigned (auto-generated slugs, computed status, nested foreign key IDs)
- An audit step requires verifying the full stored state after a write
- A subsequent operation depends on a server-assigned field not included in the lean envelope

The continuation token is optional infrastructure. Agents that do not need the full response ignore it.

---

## Requesting Full Response Inline: verify=True

For cases where the agent wants the full serialized result immediately — without a second call — use `verify=True`:

```json
{
  "resource": "device",
  "action": "create",
  "params": {
    "name": "edge-01",
    "site": "hq-1",
    "role": "access-switch"
  },
  "verify": true
}
```

When `verify=True` is set, the full serialized object is returned directly in the response. No caching occurs, no continuation token is issued. The agent receives the complete echo inline.

The `verify` parameter is injected into every write tool's inputSchema automatically by the discovery backend — no manual schema changes are needed. The parameter is a no-op on read tools.

**When to use `verify=True`:**

- The agent needs a specific server-assigned field immediately (e.g., an auto-generated ID needed for the next call in a chain)
- A validation step requires comparing the stored state to the input
- Debugging a write operation and you need to see the full response

**When not to use `verify=True`:**

- Bulk operations where the full echo would be large (use the continuation token instead)
- Sequential workflows where only confirmation of success is needed

---

## Annotating Serializer Fields with `mcp_light_key`

The default lean envelope includes `id`, `url`, and `name`/`display`. For models where other fields are more meaningful for agent confirmation, declare `mcp_light_key` as a class attribute on the serializer's `Meta`:

```python
class DeviceSerializer(serializers.ModelSerializer):
    site_slug = serializers.SlugRelatedField(
        source='site', slug_field='slug', read_only=True
    )
    primary_ip = serializers.CharField(source='primary_ip4.address', read_only=True)

    class Meta:
        model = Device
        fields = '__all__'
        mcp_light_key = ['site_slug', 'primary_ip', 'status']
```

Fields listed in `mcp_light_key` appear in every lean envelope for that serializer, in addition to the standard identifying fields. Use this to surface fields the agent frequently needs to confirm or use immediately after a write.

The `mcp_light_key` attribute follows the Django pattern for serializer meta configuration. It is frisian-mcp-specific and has no effect on non-MCP serializer usage.

---

## The Agent Experience

The lean envelope default is transparent for agents that only need write confirmation. The agent calls an eligible create or update operation and receives a small response confirming success, the new object's ID and URL, and the continuation token if the full object is needed later.

**Provisioning workflow example:**

An agent creating 60 devices in a network automation session:

- Write call returns a lean envelope — `{accepted: 60, status_code: 201, data_size: 43190, ...}` — instead of the full echo
- Agent confirms success and moves to the next step (IP assignment, VLAN configuration)
- If any device record needs inspection, it is retrieved without a second write: by the continuation token where the envelope carries one, otherwise by `verify=True` on the call or a `retrieve` on one of the returned ids

Without write-path filtering, the same call returns the full serialized echo of all 60 devices — approximately 10,798 tokens — before the agent has done anything with the result. A multi-step provisioning session of four or five such operations would consume tens of thousands of context tokens from echoes alone.

The 60-device measurement represents a 99.8% token reduction from the full echo to the envelope carrying a continuation token. **That envelope is a fixed size, so the figure holds at any bulk size; the tokenless envelope is not, and the figure does not carry over to it.** Where the token is replaced by the accepted ids, the envelope grows with the batch, and the saving is properly read as a bound rather than a fixed percentage — on the same 60-item fixture it measured well under a fifth of the full echo. Either way the envelope stays far smaller than the echo; only the constant-size claim is specific to the token path.

---

## Relationship to @mcp_heavy

`@mcp_light` and `@mcp_heavy` address opposite ends of the same problem:

- `@mcp_heavy` handles **read-path response bloat**: list responses where the result size is unknown at call time
- `@mcp_light` handles **write-path response bloat**: echo responses where the agent already provided the data

When either feature applies, the continuation token mechanism is shared: both cache large responses server-side and return a token the agent can use to retrieve the full result. The cache infrastructure is the same; only the trigger differs. (`@mcp_heavy` IS a real decorator that the host app applies to a tool; `@mcp_light` is a feature name only — it is package-level and applies automatically on write paths. What the published schema discloses decides whether the envelope carries a continuation token, not whether the envelope applies.)

If a custom action both reads and writes and the read path is decorated with `@mcp_heavy`, `@mcp_heavy` takes precedence. Write-path lean envelope behavior applies only on pure write paths where `@mcp_heavy` is not in effect.

See [Read-Response Filtering](read-response-filtering.md) for the `@mcp_heavy` guide.

---

## Summary: Default Behavior and Opt-In

| Scenario | Default behavior | Override |
|---|---|---|
| Eligible single create/update | Lean envelope (`id`, `url`, `name`, `status_code`, `data_size`), plus `continuation_token` where the schema discloses it | `verify=True` for full inline response |
| Eligible bulk create/update | Lean envelope (`accepted`, `status_code`, `data_size`), plus `continuation_token` where the schema discloses it, otherwise `ids` | `verify=True` for full inline response |
| Write whose envelope could not identify what was written | Full serialized result — the fallback, not an envelope | None needed; the full result is already inline |
| Eligible delete | Lean envelope (`deleted`, `status_code`) | No override needed; delete echoes are not large |
| Read/list | Unaffected — `@mcp_light` does not apply | N/A |
| Extra fields in envelope | Standard fields only | Add `mcp_light_key` to serializer Meta |
| Full object after write | Retrieve via continuation token where present, else `retrieve` on the returned id | `verify=True` for inline receipt, on any write |

The defaults are designed to be correct for eligible bulk and sequential writes in agent workflows, without any agent configuration. The opt-ins exist for the cases where the full response is genuinely needed.

---

*Document maintained alongside the frisian-mcp source. See [ADR 004](../../ADR/adr-004-write-path-response-filtering.md) for the architectural decision record.*
