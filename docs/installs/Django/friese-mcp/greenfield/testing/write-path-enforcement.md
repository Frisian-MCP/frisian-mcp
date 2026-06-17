# Write-Path Enforcement

**Scenario:** Verify that write operations are absent from read-tier MCP paths and that lean confirmation envelopes are returned when write tools are invoked correctly.

---

## What You Are Testing

frisian-mcp applies `@mcp_light` write-path filtering by default: create, update, and destroy operations return a lean confirmation envelope rather than echoing the full serialized object. This test verifies:

1. The lean envelope shape is returned — not a full object echo.
2. Bulk operations return the `accepted`/`failed` count shape — not a list of full objects.
3. A `continuation_token` is present when the full object is available for retrieval.
4. `verify=True` returns the full object when explicitly requested.

---

## Prerequisites

- frisian-mcp installed and mounted at `/mcp/`
- At least one write-capable ViewSet registered (or use the example model if using the demo app)
- An agent token with `read_write` permission tier

---

## Test 1: Single-Object Create Returns Lean Envelope

**Setup:** Call a create tool (`POST` equivalent).

**Input:**
```json
{
  "name": "test-device-01",
  "status": "active"
}
```

**Expected response shape:**
```json
{
  "id": "<uuid or pk>",
  "url": "<canonical object URL>",
  "name": "test-device-01",
  "status_code": 201,
  "data_size": "<integer — bytes of the full object>",
  "continuation_token": "<opaque token>"
}
```

**Pass condition:** Response contains `id`, `status_code`, `data_size`, and `continuation_token`. Response does NOT contain nested serialized fields beyond those listed.

**Fail condition:** Response contains the full DRF-serialized object (all fields echoed back). This indicates write-path filtering is not applied.

---

## Test 2: Bulk Create Returns Count Envelope

**Setup:** Call a bulk-create tool with multiple objects.

**Input:**
```json
[
  {"name": "batch-device-01", "status": "active"},
  {"name": "batch-device-02", "status": "active"},
  {"name": "batch-device-03", "status": "active"}
]
```

**Expected response shape:**
```json
{
  "accepted": 3,
  "failed": 0,
  "status_code": 201,
  "data_size": "<integer>",
  "continuation_token": "<opaque token>"
}
```

**Pass condition:** Response contains `accepted`, `failed`, `status_code`, `data_size`, and `continuation_token`. No array of serialized objects is returned.

**Fail condition:** Response is an array of full serialized objects. Token cost scales linearly with batch size when this fires — a 60-object batch produced ~43 KB in integration testing.

---

## Test 3: Delete Returns Lean Confirmation

**Setup:** Call a destroy tool on an existing object.

**Expected response shape:**
```json
{
  "deleted": true,
  "status_code": 204
}
```

**Pass condition:** Response is the lean delete confirmation. No serialized object data.

---

## Test 4: `verify=True` Returns Full Object

**Setup:** Call a create tool with `verify=True`.

**Input:**
```json
{
  "name": "verify-device-01",
  "status": "active",
  "verify": true
}
```

**Pass condition:** Response contains the full serialized object. This is the explicit opt-in path.

---

## Test 5: Continuation Token Retrieves Full Object

**Setup:** From Test 1, take the `continuation_token` from the lean envelope. Call the same tool again with:

```json
{
  "continuation_token": "<token from test 1>"
}
```

**Pass condition:** Response contains the full serialized object for the record created in Test 1. No new write operation is executed.

---

## Common Failures

| Symptom | Likely cause |
|---------|-------------|
| Full object echoed on create | `@mcp_light` filtering not active — check `FRISIAN_MCP_WRITE_FILTERING` is not disabled |
| `continuation_token` missing | frisian-mcp version does not support continuation on write path |
| 403 on write tool call | Token is `read` tier — use a `read_write` or `admin` tier token |
| 404 on write tool call | Write tools are not mounted on this path — expected if using path separation architecture |

---

## Related Docs

- [Write-Path Response Filtering](../../../../../Guide/write-path-response-filtering.md)
- [The Token Problem](../../../../../Guide/the-token-problem.md)
- [Permission Tiers](permission-visibility.md)
