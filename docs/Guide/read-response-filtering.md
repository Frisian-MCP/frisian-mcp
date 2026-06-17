# Read-Response Filtering with @mcp_heavy

**Category:** guide  
**Slug:** read-response-filtering  
**Audience:** Developers annotating ViewSets for production MCP use

---

## What @mcp_heavy Does

`@mcp_heavy` is a decorator applied to ViewSet list actions. It changes the MCP response from a bare result set into a structured metadata envelope, ensuring the agent receives size and pagination information before committing to a full data transfer.

Without `@mcp_heavy`, a list call returns all matching records in a single response. With `@mcp_heavy`, the first call returns a probe response: total record count, estimated data size, and the first page of results. The agent then decides whether to paginate, refine the filter, or proceed with the data it has received.

The decorator does not affect non-MCP callers. Standard DRF clients, browseable API users, and other consumers of the ViewSet receive normal DRF responses. Only MCP-routed calls receive the probe-first behavior.

---

## Why Large Reads Are a Problem

When an agent calls a list endpoint, the response size depends on how many records match the query — a quantity the agent cannot know before making the call. A filter that seems narrow ("devices at site hq-1") might match 12 records today and 1,200 after a data import. The agent constructs the call identically in both cases and receives wildly different response sizes.

Without read-response filtering, the agent's context window is at the mercy of the data. See [The Token Problem](the-token-problem.md) for the full analysis of result payload bloat. The problem is not theoretical: a list response returning a large number of fully serialized objects can exhaust most agent context budgets before the agent has evaluated a single record.

This is the read-path problem. It is structurally different from the write-path problem (`@mcp_light`): on write paths, the agent provided the data so the echo is bounded; on read paths, the result is determined by the stored data and is unknown at call time. The solution must account for that uncertainty at the response layer.

---

## How the Two-Call Probe Pattern Works

`@mcp_heavy` implements a probe-then-fetch pattern. The initial call does two things at once: it executes the query and returns the first page of results, but it also returns the metadata the agent needs to understand the full result shape.

**Call 1 — probe (automatic on first call):**

The agent calls the list tool normally:

```json
{
  "resource": "device",
  "action": "list",
  "params": { "site": "hq-1" }
}
```

The response includes:

```json
{
  "count": 847,
  "data_size": 3218600,
  "next": "<continuation_token>",
  "previous": null,
  "results": [ ... first page of records ... ]
}
```

- `count` — total records matching the filter
- `data_size` — estimated full response size in bytes
- `next` — continuation token to fetch the next page
- `results` — first page (default page size applies)

**Call 2+ — page fetches:**

If the agent decides to paginate, it calls the same tool with the continuation token:

```json
{
  "resource": "device",
  "action": "list",
  "params": { "site": "hq-1" },
  "next": "<continuation_token>"
}
```

Each page fetch returns the next set of results plus an updated continuation token for the following page. When `next` is null, all records have been retrieved.

**Agent decision point:**

After the probe response, the agent has enough information to decide:

- The total count is small → proceed with the first page, no further calls needed
- The total count is large, but the agent only needs an aggregate → work with `count` alone, no page fetches
- The total count is large and the agent needs all data → paginate deliberately, fetch as many pages as needed
- The filter was too broad → refine the filter and call again with narrower parameters

This decision-making happens within the agent's context. Without the probe response, the agent has no basis for these decisions — it either receives everything or nothing.

---

## How to Register a Heavy Tool

`@mcp_heavy` is an **explicit MCP tool registration** decorator — sibling of `@mcp_tool` / `@mcp_dispatcher` / `@mcp_action`, not a bare wrapper for a DRF `ModelViewSet` method. It requires `name`, `description`, and `input_schema`, and the decorated callable must have a `(arguments, request)` signature:

```python
from frisian_mcp.decorators import mcp_heavy

@mcp_heavy(
    name="devices.search",
    description="Search devices and return a probe envelope with pagination metadata.",
    input_schema={
        "type": "object",
        "properties": {
            "site": {"type": "string"},
            "role": {"type": "string"},
        },
    },
)
def search_devices(arguments, request):
    qs = Device.objects.filter(**arguments)
    return DeviceSerializer(qs, many=True).data
```

The tool is registered at import time and surfaced in `tools/list` with the five probe-protocol parameters (`continuation_token`, `mode`, `page`, `page_size`, `filter_keys`) merged into the input schema automatically.

### For Auto-Discovered ViewSets — use the threshold backstop

Most large applications expose their REST surface through auto-discovered `ModelViewSet`s, and you do not want to register every list endpoint by hand. Set `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` instead — any auto-discovered tool whose response exceeds the byte threshold is auto-wrapped in the same probe envelope without a per-ViewSet code change:

```python
# settings.py
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 50_000  # bytes
```

Use `@mcp_heavy` directly when you want **explicit** control — a named heavy tool that surfaces in `tools/list` with a curated input schema, distinct from the auto-discovered ViewSet action.

---

## Automatic Threshold Negotiation

For large applications where not every ViewSet has been individually reviewed, `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` enables automatic `@mcp_heavy` behavior as a safety net:

```python
# settings.py
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 50000  # bytes
```

When a list response would exceed this threshold (in bytes), frisian-mcp applies probe-first behavior automatically even on ViewSets that are not explicitly decorated. The response format is identical to an explicitly decorated ViewSet.

Auto-negotiation is a fallback, not a replacement for explicit annotation. An explicitly decorated ViewSet is always probe-first; auto-negotiation only triggers when the response would be large enough to warrant it. For ViewSets where you know the result will always be large, explicit annotation is clearer and more predictable.

---

## Relationship to DRF Pagination

DRF's built-in `PAGE_SIZE` setting truncates result sets to a maximum page size. `@mcp_heavy` is complementary, not a replacement.

DRF pagination limits database query cost and response transfer size — useful for all callers. `@mcp_heavy` ensures the structured metadata (`count`, `data_size`, `next`) is present in the MCP response regardless of the underlying ViewSet's pagination class configuration. Some ViewSets use custom pagination classes that do not include all these fields; `@mcp_heavy` guarantees the contract at the MCP layer.

The recommended production configuration is both: DRF `PAGE_SIZE` for general query cost control, plus `@mcp_heavy` on any list endpoint that could return more than a few dozen records.

---

## The Agent Experience

From the agent's perspective, `@mcp_heavy` is largely transparent. The agent calls the tool the same way it would any list endpoint. The difference is in what comes back.

**Without `@mcp_heavy`:** Agent receives a list of records. If the list is short, fine. If the list is long, the context window fills before the agent can reason about the results.

**With `@mcp_heavy`:** Agent receives count, estimated size, and the first page. The agent now knows whether it received everything ("count: 12, results has 12 entries") or a partial view ("count: 847, results has 50 entries, next: ..."). The agent can make an informed decision about what to do next without exhausting its context window on data it may not need.

For automated pipelines that only need summary information (how many devices are offline? does this prefix exist?), the probe response often provides all needed information in a single call — no page fetches required.

---

## Relationship to @mcp_light

`@mcp_heavy` addresses the read path: responses from list and retrieve operations that may be unexpectedly large. `@mcp_light` addresses the write path: responses from create, update, and destroy operations that echo back the full serialized object.

If a custom action both reads and writes, and is decorated with both, `@mcp_heavy` takes precedence.

See [Write-Response Filtering](write-path-response-filtering.md) for the `@mcp_light` guide.

---

## Summary: When to Apply @mcp_heavy

Apply `@mcp_heavy` to any list action where:

- The record count depends on user-supplied filters that could match an unbounded number of records
- The underlying model has many fields or nested relationships, making individual records large
- The list endpoint is expected to be called frequently in agent workflows

Do not apply `@mcp_heavy` to list actions where:

- The result set is bounded by design and will always be small (e.g., a list of status choices, a handful of configuration objects)
- The endpoint is not intended for agent consumption and is excluded via `@mcp_ignore`

When in doubt, apply it. The probe overhead is one round-trip. The cost of a context window exhausted on a single list call is the entire session.

---

*Document maintained alongside the frisian-mcp source. See [ADR 005](../ADR/adr-005-read-response-filtering.md) for the architectural decision record.*
