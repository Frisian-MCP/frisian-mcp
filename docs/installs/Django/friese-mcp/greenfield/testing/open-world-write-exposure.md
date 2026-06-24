# Open-World Write Exposure

**Scenario:** Verify that write and admin operations are absent from public-facing MCP paths, and that path separation prevents a read-tier caller from discovering or invoking write surfaces.

---

## What You Are Testing

frisian-mcp's security architecture recommends separating read and write surfaces at the URL routing level. This is not a package default — it is an architecture you configure. This test verifies that if you have implemented path separation, the boundary holds: callers on the read path cannot reach write tools by any means.

The threat model is an agent — compromised, misconfigured, or adversarial — that:

1. Enumerates tools via `tools/list`
2. Identifies write or admin tool names
3. Attempts to invoke them directly, bypassing the listing

Path separation defeats this at the routing level: write routes are not mounted on the read path. There is nothing to invoke.

See [Security Architecture](../../../../../Security/security.md) for the full design rationale.

---

## Prerequisites

- Read path mounted at `/mcp/` (public, no auth required or read-tier auth)
- Write path mounted at a separate URL (e.g. `/mcp-write/` or `/mcp-protected/`)
- Write tools registered only on the write path's McpView instance

---

## Test 1: Read Path `tools/list` Contains No Write Tools

**Setup:** Call `/mcp/tools/list` with a read-tier token (or no token if the path is public).

**Pass condition:** Response contains no tools with write or admin semantics. Create, update, destroy tools are absent.

**How to verify:** Cross-check the listing against `/mcp-write/tools/list` (with an authorized write token). Tools present on the write path but absent on the read path confirm correct separation.

---

## Test 2: Direct Invocation of Write Tool on Read Path Returns 404

**Setup:** From the write-path `tools/list`, take the name of a write tool. Attempt to invoke it via `/mcp/` directly:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "devices.create",
    "arguments": {"name": "probe-device"}
  },
  "id": 1
}
```

**Pass condition:** Response is a tool-not-found error (not a permission error). The tool does not exist on this path — the router never registered it here.

**Fail condition:** Response is a 403 permission error. This means the write tool IS registered on the read path but blocked by permission class. Path separation has not been achieved — a permission misconfiguration would expose the tool.

---

## Test 3: Error Responses on Read Path Are Uninformative

**Setup:** On the read path (`/mcp/`), call a tool name that exists on the write path but not on the read path.

**Pass condition:** The error response for a non-existent tool is identical in shape and timing to the response for any other non-existent tool name. No timing side-channel, no schema leakage.

**What to avoid:** A read-path 404 that takes 15ms (route found, permission check ran) vs a truly missing route that takes 2ms tells a probing agent the write route exists. Route separation eliminates this timing signal entirely.

---

## Test 4: Admin Tools Absent From Both Read and Write Paths (If Using Three-Tier Architecture)

**Setup:** If you have deployed a three-tier architecture (public read / authenticated write / admin), confirm that admin tools (`admin=True` on `@mcp_tool`) are absent from both `/mcp/` and `/mcp-write/`.

**Pass condition:** Admin tools appear only at the protected admin path. Neither the read nor write `tools/list` includes them.

---

## Test 5: Enumeration Attack Simulation

**Setup:** Using a `read-token`, systematically call `tools/list`, then attempt to invoke each write tool name observed during prior access (before path separation was deployed, or from source code knowledge).

**Pass condition:** All write tool names return tool-not-found on the read path. Zero write operations succeed.

This test is most useful as a regression check after deploying path separation for the first time.

---

## Common Failures

| Symptom | Likely cause |
|---------|-------------|
| Write tools visible on read path | Single `McpView` instance serving both paths — use separate view instances with separate URL mounts |
| Write tool returns 403 instead of 404 | Write tool is registered on the read path but permission-gated; path separation is not achieved |
| Admin tools appear in write-path listing | Tools registered with `admin=True` but no separate admin path configured — they default to the main tool registry |
| Timing difference between 404s | Route lookup cost differs — investigate middleware execution order |

---

## Architecture Note

If you are not using path separation and instead rely on `permission_classes` alone, this test suite will find write tools on the read path (failing Test 1 and Test 2). That is an architectural finding, not a frisian-mcp bug. The package supports both configurations; path separation is a recommendation documented in the security guide. Decide deliberately which model fits your deployment before accepting the test results.

---

## Related Docs

- [Security Architecture](../../../../../Security/security.md)
- [Access Control](access-control.md)
- [Permission Visibility](permission-visibility.md)
