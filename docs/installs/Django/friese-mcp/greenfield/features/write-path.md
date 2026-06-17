# Feature: Write-Path Response Filtering

**Audience:** Developers managing write-operation token costs  
**Package version:** 1.0.x

---

## The problem

When an agent creates or updates an object, the conventional DRF response is the full serialized object echoed back. For single-object writes this is manageable. For bulk writes it scales linearly:

- A 60-device bulk create produces ~10,800 tokens of echo
- A 200-device bulk create produces ~36,000 tokens of echo
- Sequential write workflows (create devices → assign IPs → configure VLANs → register DNS) compound the cost at every step

See [Write-Path Response Filtering](../../../../../Guide/write-path-response-filtering.md) and [The Token Problem](../../../../../Guide/the-token-problem.md) for the full analysis including production measurements.

---

## How it works

Write-path filtering is **automatic** — no decorator, no ViewSet change, no opt-in.  Every tool whose underlying action is a write (`create`, `update`, `partial_update`, `destroy`, or any `@action` declared with `methods=['POST', 'PUT', 'PATCH', 'DELETE']`) routes through the MCP gateway with a lean confirmation envelope by default.  Standard DRF clients calling the same ViewSet directly receive the conventional full-echo response; only MCP-routed calls receive the lean envelope.

```python
# No decorator needed.  The ViewSet remains a plain DRF ModelViewSet.
class DeviceViewSet(ModelViewSet):
    queryset = Device.objects.all()
    serializer_class = DeviceSerializer
```

When a write operation routed through the MCP gateway completes, the response is a lean confirmation envelope instead of the full serialized object.  Customise the envelope via the serializer's `Meta.mcp_light_key` attribute (see below) or override per call via `verify=True`.

> **Naming note.** This feature is referred to as `@mcp_light` in design notes and ADRs, mirroring `@mcp_heavy` for read paths.  Despite the name, there is no `mcp_light` decorator in the package — the behaviour is package-level by design (ADR-004).  Earlier draft docs that showed `from frisian_mcp import mcp_light` and `@mcp_light` on a ViewSet were inaccurate; ignore them.

---

## Lean envelope shapes

Single-object create or update:

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

Bulk create or update (when supported by the underlying ViewSet):

```json
{
  "accepted": 60,
  "failed": 0,
  "status_code": 201,
  "data_size": 43190,
  "continuation_token": "<token>"
}
```

Delete:

```json
{
  "id": "abc123",
  "deleted": true,
  "status_code": 204
}
```

Read and list operations are unaffected.

---

## `verify=True` — per-call full-object override

The `verify` parameter is injected automatically into every write tool's input schema. Passing `verify=True` on a specific call returns the full serialized object directly — no caching, no second call:

```json
{
  "resource": "device",
  "action": "create",
  "params": { "name": "edge-01", "site": "hq-1" },
  "verify": true
}
```

---

## Continuation token — retrieve full object without re-executing the write

The `continuation_token` in the lean envelope reuses the `@mcp_heavy` cache infrastructure. Pass it to the heavy-fetch path with `mode=full` to retrieve the complete serialized object. The write is not re-run.

---

## `mcp_light_key` — custom lean envelope fields

To include specific serializer fields in the lean envelope beyond the standard `id` / `url` / `name` extraction, declare `mcp_light_key` in the serializer's `Meta`:

```python
class DeviceSerializer(serializers.ModelSerializer):
    site_slug = serializers.SlugRelatedField(
        source='site', slug_field='slug', read_only=True
    )

    class Meta:
        fields = '__all__'
        mcp_light_key = ['site_slug', 'role']
```

Fields listed in `mcp_light_key` appear in every lean envelope for that serializer, in addition to the standard identifying fields.

**Lean field extraction order:** `id` / `pk` → `url` → `name` / `display` → `mcp_light_key` annotated fields → `status_code`, `data_size`, `continuation_token` (always present).

---

## Precedence

If a tool carries both `@mcp_heavy` decoration and write semantics, `@mcp_heavy` probe behaviour takes precedence on the read path.  Pure write paths always use the lean envelope.

For a backstop that applies to all tools — including unannotated read tools — set `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD`:

```python
# settings.py

# Cap all tool responses at 10 KB
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 10_000
```

---

## See also

- [Write-Path Response Filtering](../../../../../Guide/write-path-response-filtering.md) — design rationale and production measurements
- `features/mcp-heavy.md` — large read response negotiation
- `features/mcp-tool.md` — manual tool registration
