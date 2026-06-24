# Throttling Enforcement

**Scenario:** Verify that DRF throttle classes applied to the MCP view are enforced, and that rate limits behave correctly under sequential and burst call patterns typical of agent workflows.

---

## What You Are Testing

frisian-mcp does not ship its own throttle classes — it delegates entirely to DRF's throttling system. The MCP view is a standard `APIView` subclass; any DRF throttle class that works on a DRF view works on frisian-mcp.

Agent workflows differ from human-browser patterns: an agent in a bulk-provisioning loop may call the same tool hundreds of times in a few seconds. This test verifies that throttle classes applied to MCP requests behave predictably under agent-scale load, and that the throttle ceiling is enforced before an agent exhausts a rate-limited upstream resource.

---

## Prerequisites

- DRF throttling configured in `REST_FRAMEWORK` settings, or throttle classes attached to `McpView` via a subclass
- A known rate limit (e.g. 100 requests/minute for anonymous, 1000/minute for authenticated)

---

## Example Configuration

```python
# settings.py
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "600/minute",
    },
}
```

Or scoped to the MCP view only:

```python
# mcpview subclass
from frisian_mcp.views import McpView
from rest_framework.throttling import UserRateThrottle

class ThrottledMcpView(McpView):
    throttle_classes = [UserRateThrottle]
```

---

## Test 1: Throttle Fires on Burst at the Rate Limit

**Setup:** Using an authenticated user at the configured `user` rate limit (e.g. 600/min), send requests to `tools/list` in a tight loop until the limit is reached.

**Pass condition:** On the request that exceeds the limit, the response is HTTP 429 with a `Retry-After` header indicating when the window resets.

**What to verify in the response:**

```json
{
  "detail": "Request was throttled. Expected available in X seconds."
}
```

---

## Test 2: Throttle Is Per-User, Not Per-Server

**Setup:** Two authenticated users, each at the rate limit independently.

**Pass condition:** User A hitting their limit does not affect User B's request count. Each user has an independent throttle window.

---

## Test 3: Unauthenticated Caller Is Throttled by `anon` Rate

**Setup:** Send unauthenticated requests to `/mcp/tools/list` up to the `anon` rate limit.

**Pass condition:** 429 is returned once the `anon` limit is reached. Requests from authenticated users are unaffected.

---

## Test 4: Tool Call Counts Against the Same Rate Window as `tools/list`

**Setup:** Alternate between `tools/list` and `tools/call` requests. Both go through the same MCP view.

**Pass condition:** The combined call count (not separate per-endpoint) depletes the throttle window. Both request types draw from the same bucket.

This matters for agent workflows that call `tools/list` once at startup and then loop on tool calls — the full request sequence counts against the rate limit.

---

## Test 5: Throttle Resets After the Window

**Setup:** Exhaust the rate limit. Wait for the `Retry-After` duration. Send another request.

**Pass condition:** Request succeeds after the window resets. No manual intervention required.

---

## Test 6: Throttle Under Bulk-Provisioning Pattern

**Setup:** Simulate a realistic agent bulk-provisioning workflow:

1. `tools/list` (1 call)
2. Loop of `devices.create` calls × N devices

**Goal:** Determine at what N the agent hits the rate limit. Adjust throttle configuration before deploying if the number is too low for your expected workflows.

**This is a capacity-sizing test, not a pass/fail test.** The result informs your throttle configuration, not whether frisian-mcp has a defect.

Typical agent-scale guidance:

- A 200-device provisioning run with sequential creates needs at minimum 201 calls per session
- If your `user` rate is 600/min and each create takes ~300ms, you have headroom for ~33 devices/minute before hitting the wall
- For bulk operations, use the bulk-create tool (single call, many objects) rather than sequential single creates

---

## Common Failures

| Symptom | Likely cause |
|---------|-------------|
| No 429 at any request rate | Throttle classes not applied to `McpView` — check `DEFAULT_THROTTLE_CLASSES` or view subclass |
| 429 fires too early | `anon` and `user` rates are swapped or the wrong throttle class is being matched |
| `Retry-After` header missing | Custom throttle class not setting the header — DRF's built-ins set it correctly |
| Throttle applies to all views, not just MCP | `DEFAULT_THROTTLE_CLASSES` affects your entire DRF API — scope throttles to a `McpView` subclass if you want MCP-only limits |

---

## Agent Workflow Guidance

Because agents call MCP sequentially and at machine speed:

- Set `user` throttle rates high enough to cover a full provisioning session without interruption
- Monitor actual call rates in your Django logs before setting production limits
- Consider using DRF's `ScopedRateThrottle` to set a separate, higher limit for the MCP surface without affecting other API consumers

```python
# Scoped throttle example
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "mcp_agents": "2000/minute",
        "api_users": "300/minute",
    },
}

class ThrottledMcpView(McpView):
    throttle_scope = "mcp_agents"
```

---

## Related Docs

- [Access Control](access-control.md)
- [Installation & Configuration Reference](../../../../../Reference/installation-configuration-reference.md)
