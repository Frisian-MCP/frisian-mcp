# Permission Visibility

**Scenario:** Verify that `tools/list` returns the correct tool set for each permission tier, and that tools absent from a tier's listing cannot be invoked by a caller at that tier.

---

## What You Are Testing

frisian-mcp filters `tools/list` by the caller's permission tier. A `read` token sees only `read`-tier tools. A `read_write` token sees `read` and `read_write` tools. An `admin` token sees all tools. This test verifies that the boundary holds in both directions: tools are visible exactly when they should be, and invisible tools cannot be invoked.

Tier ranks: `read (0) < read_write (1) < admin (2)`

---

## Prerequisites

- Three tokens configured in `FRISIAN_MCP_API_KEYS` or equiv:
  - `read-token` → `read`
  - `rw-token` → `read_write`
  - `admin-token` → `admin`
- At least one tool registered at each tier (auto-discovery from a ModelViewSet produces read and read_write tools automatically)

---

## Test 1: Read Tier Sees Only Read Tools

**Setup:** Call `tools/list` with `read-token`.

**Pass condition:**

- Response contains all `read` tier tools.
- Response does NOT contain any `read_write` or `admin` tier tools.

**How to verify tier assignment:** Each registered tool in `tools/list` does not expose its tier directly. Verify by cross-checking: call the same tool with `rw-token` — if additional tools appear that were absent with `read-token`, those are the `read_write` tools correctly hidden from the read caller.

---

## Test 2: Read-Write Tier Sees Read and Read-Write Tools

**Setup:** Call `tools/list` with `rw-token`.

**Pass condition:**

- Response is a superset of the `read-token` listing.
- Response contains all write-capable tools (create, update, destroy equivalents from auto-discovered ViewSets).
- Response does NOT contain `admin` tier tools.

---

## Test 3: Admin Tier Sees All Tools

**Setup:** Call `tools/list` with `admin-token`.

**Pass condition:**

- Response is a superset of the `rw-token` listing.
- Any tools registered with `admin=True` on `@mcp_tool` appear only in this listing.

---

## Test 4: Hidden Tool Cannot Be Invoked Cross-Tier

**Setup:** Identify a `read_write`-tier tool name from the `rw-token` listing. Attempt to invoke it using `read-token`.

**Pass condition:** Tool call returns a permission error (403 equivalent). The tool cannot be invoked even though the caller supplies the correct tool name directly.

**Fail condition:** Tool executes successfully with `read-token`. This indicates the permission_tier filter is not enforced at invocation time — only at listing time. Both boundaries must hold.

---

## Test 5: Dispatcher Tools Are Always Visible

**Setup:** If any tools are registered via `@mcp_dispatcher`, call `tools/list` with `read-token`.

**Pass condition:** Dispatcher tools appear in the `read-token` listing. Dispatchers are always registered at tier `read` because they are navigation entry points, not gated resources. Per-action permissions are enforced at dispatch time.

---

## Test 6: Unauthenticated Caller Behavior

**Setup:** Call `tools/list` with no `Authorization` header.

**Expected behavior depends on `FRISIAN_MCP_UNAUTHENTICATED_TIER` setting:**

| Setting value | Expected listing |
|--------------|-----------------|
| `'read'` (default) | Read-tier tools visible |
| `None` | 401 or empty listing |

**Pass condition:** Behavior matches the configured setting.

---

## Common Failures

| Symptom | Likely cause |
|---------|-------------|
| Admin tools visible to read token | `permission_tier` not set on `@mcp_tool` call — defaults to `read` |
| Write tools missing from rw-token listing | Tool registered with `admin=True` accidentally |
| Dispatcher tool missing from any listing | Dispatcher incorrectly registered with `permission_tier` other than `read` |
| Cross-tier invocation succeeds | `permission_classes` not set on the tool — access control falls through to tool handler |

---

## Related Docs

- [Access Control](access-control.md)
- [Open-World Write Exposure](open-world-write-exposure.md)
- [Installation & Configuration Reference](../../../../../Reference/installation-configuration-reference.md)
