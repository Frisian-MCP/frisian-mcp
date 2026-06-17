# Write-Path Response Filtering with @mcp_light

**Category:** guide  
**Slug:** write-path-response-filtering  
**Audience:** Developers annotating ViewSets and serializers for production MCP use

---

## What @mcp_light Does

`@mcp_light` is frisian-mcp's write-path response filter. It changes the default MCP response for create, update, and destroy operations from a full serialized object echo to a lean confirmation envelope: a small set of identifying fields plus metadata that lets the agent retrieve the full object if needed.

The lean envelope is the default for all write tools — no agent action required, and no host-app decorator either.  An agent that only needs confirmation of success receives exactly that, at a fraction of the token cost of a full echo.  An agent that needs the full serialized result can request it per call using `verify=True`, or retrieve it via the continuation token without re-executing the write.

The feature is package-level by design (see [ADR-004](../ADR/adr-004-write-path-response-filtering.md)): it applies automatically to every tool whose underlying action is a write, with no `@mcp_light` decorator to import or apply.  Standard DRF clients calling the same ViewSet directly receive the conventional full-echo response; only MCP-routed write calls receive the lean envelope.

---

## Why Write-Echo Is a Problem

When an agent creates or updates an object, the conventional response is the full serialized object. For a single-object create on a simple model, this is a modest number of tokens. For bulk operations, the cost scales with the number of objects written.

A 60-device bulk create in a production integration session produced a full echo response of approximately 10,798 tokens (43,190 bytes). Production device objects are approximately 3,800 bytes each (~603 tokens per device). Larger bulk operations scale linearly: a 200-device bulk create would produce roughly 36,000 tokens from the echo alone, before the agent has done anything with the result.

Write operations are often sequential in agent workflows: provision a batch of devices, then assign IP addresses, then configure VLANs, then register in DNS. Each step produces an echo. By the third or fourth bulk write step, the context window is significantly depleted — not from reasoning or retrieved state, but from echoes of data the agent already sent.

See [The Token Problem](the-token-problem.md) for the full analysis, including Problem 3.

---

## The Lean Confirmation Envelope

The lean envelope is returned by default for all write operations. Its structure depends on the operation type.

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

**Bulk create or update:**

```json
{
  "accepted": 60,
  "failed": 0,
  "status_code": 201,
  "data_size": 43190,
  "continuation_token": "<token>"
}
```

**Delete:**

```json
{
  "id": "abc123",
  "deleted": true,
  "status_code": 204
}
```

**Field extraction order for single-object envelopes:**

The identifying fields are extracted from the full serialized response in priority order: `id`/`pk` first, then `url`, then `name`/`display`, then any fields listed in the serializer's `Meta.mcp_light_key`. The `status_code`, `data_size`, and `continuation_token` are always included.

---

## Retrieving the Full Object

The lean envelope includes a `continuation_token`. This token reuses the existing `@mcp_heavy` cache infrastructure: the full serialized result is cached server-side, and the continuation token encodes the cache key.

To retrieve the full object after a write, the agent calls the heavy-fetch path with `mode=full` and the continuation token. The write is not re-executed — the cached result is returned.

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

The lean envelope default is transparent for agents that only need write confirmation. The agent calls a create or update operation and receives a small response confirming success, the new object's ID and URL, and the continuation token if the full object is needed later.

**Provisioning workflow example:**

An agent creating 60 devices in a network automation session:

- Write call returns `{accepted: 60, failed: 0, status_code: 201, data_size: 43190, continuation_token: "..."}` — approximately 24 tokens
- Agent confirms success and moves to the next step (IP assignment, VLAN configuration)
- If any device record needs inspection, the continuation token retrieves it without a second write

Without write-path filtering, the same call returns the full serialized echo of all 60 devices — approximately 10,798 tokens — before the agent has done anything with the result. A multi-step provisioning session of four or five such operations would consume tens of thousands of context tokens from echoes alone.

The 60-device measurement represents a 99.8% token reduction from the full echo to the lean envelope. At larger bulk sizes, the saving grows proportionally — the lean envelope is a constant size regardless of how many objects were written.

---

## Relationship to @mcp_heavy

`@mcp_light` and `@mcp_heavy` address opposite ends of the same problem:

- `@mcp_heavy` handles **read-path response bloat**: list responses where the result size is unknown at call time
- `@mcp_light` handles **write-path response bloat**: echo responses where the agent already provided the data

The continuation token mechanism is shared: both features cache large responses server-side and return a token the agent can use to retrieve the full result. The cache infrastructure is the same; only the trigger differs. (`@mcp_heavy` IS a real decorator that the host app applies to a tool; `@mcp_light` is a feature name only — it is package-level and applies automatically with no decorator to import.)

If a custom action both reads and writes and the read path is decorated with `@mcp_heavy`, `@mcp_heavy` takes precedence. Write-path lean envelope behavior applies only on pure write paths where `@mcp_heavy` is not in effect.

See [Read-Response Filtering](read-response-filtering.md) for the `@mcp_heavy` guide.

---

## Summary: Default Behavior and Opt-In

| Scenario | Default behavior | Override |
|---|---|---|
| Single create/update | Lean envelope (`id`, `url`, `name`, `status_code`, `data_size`, `continuation_token`) | `verify=True` for full inline response |
| Bulk create/update | Lean envelope (`accepted`, `failed`, `status_code`, `data_size`, `continuation_token`) | `verify=True` for full inline response |
| Delete | Lean envelope (`id`, `deleted`, `status_code`) | No override needed; delete echoes are not large |
| Read/list | Unaffected — `@mcp_light` does not apply | N/A |
| Extra fields in envelope | Standard fields only | Add `mcp_light_key` to serializer Meta |
| Full object after write | Retrieve via continuation token | `verify=True` for inline receipt |

The defaults are designed to be correct for the common case — bulk and sequential writes in agent workflows — without any agent configuration. The opt-ins exist for the cases where the full response is genuinely needed.

---

*Document maintained alongside the frisian-mcp source. See [ADR 004](../ADR/adr-004-write-path-response-filtering.md) for the architectural decision record.*
