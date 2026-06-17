# ADR 005: Read-Path Response Filtering via @mcp_heavy

**Category:** reference  
**Slug:** adr-005-read-response-filtering  
**Status:** Accepted  
**Date:** 2026-06-02

---

## Context

When an MCP agent calls a list or read endpoint, the underlying DRF ViewSet executes the query and serializes the results. The full serialized payload is returned in the tool response. For a small result set — a handful of records — this is fine. The problem is that the agent does not know how many records match the query before making the call.

A filter like `site=hq-1` might match 5 devices or 5,000. The agent constructs the call without knowing which. If the query matches 5,000 records and no pagination is in force, the full serialized result — potentially hundreds of thousands of tokens — is loaded into the agent's context window in a single operation. The context window is exhausted before the agent has evaluated a single record.

This is structurally different from the write-path problem. On write paths, the agent provided all the data, so the echo is a known quantity. On read paths, the result size is determined by the data stored in the system — unknown to the agent at call time, and potentially unbounded.

Three approaches were considered:

**Hard pagination limits** — DRF's `PAGE_SIZE` setting truncates result sets to a maximum page size. This is necessary but not sufficient. A bare list response returns only the records, not the total count. The agent receives a slice of results with no indication that it is a slice — it cannot distinguish "the 50 records I asked for" from "all 50 records matching my query." It cannot make an informed decision about whether to paginate, refine the filter, or proceed.

**Response streaming** — return records incrementally as the query executes. This prevents the full result from loading at once, but requires streaming MCP transport support (not universally available) and does not solve the core information problem: the agent still does not know how many total records exist.

**Probe-then-fetch** — separate the response into two operations: a probe call that returns metadata (total count, estimated size, pagination cursor) and a fetch call that retrieves data on a page-by-page basis. The agent learns the shape of the result before committing to receiving it. The agent can decide to refine the filter, work with a smaller page, or proceed with pagination.

The probe-then-fetch pattern is the right solution. It gives the agent the information it needs to make a decision while keeping the initial context cost bounded to a small metadata response.

## Decision

frisian-mcp implements read-path response filtering as an opt-in decorator, `@mcp_heavy`, applied to ViewSet list actions.

**Decorator usage:**

```python
from frisian_mcp.decorators import mcp_heavy

class DeviceViewSet(viewsets.ModelViewSet):

    @mcp_heavy
    def list(self, request):
        queryset = self.get_queryset()
        ...
```

**Two-call pattern:**

When an agent calls a list action decorated with `@mcp_heavy`, the first call functions as a probe. The response includes:

- `count` — total number of records matching the applied filters
- `data_size` — estimated response size in bytes for the full result set
- `next` — pagination cursor for the first page of results
- `previous` — pagination cursor for a prior page (where applicable)
- `results` — first page of records (default page size applies)

The agent receives the metadata it needs to make a decision. If the total count is small, the agent can proceed with the first page. If the count is large, the agent can apply additional filters or paginate deliberately. The context window is not pre-filled with records the agent may never use.

Subsequent pages are fetched by calling the same tool with the `next` cursor, continuing as needed.

**Automatic threshold negotiation:**

The `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` setting (in bytes) triggers automatic `@mcp_heavy` behavior on any ViewSet action — even those not explicitly decorated — when the estimated response size exceeds the threshold. This provides a safety net for large Django applications where not every ViewSet has been individually reviewed.

```python
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 50000  # bytes
```

**Relationship to the discovery backend:**

The discovery backend marks `@mcp_heavy` tools in their generated schema so agents can see — in `tools/list` — that a tool returns paginated metadata rather than a complete dataset. This hint improves tool selection: an agent reading tool descriptions knows which operations require pagination planning and which return complete results.

**Cache layer:**

Probe results are cached server-side keyed by `_HEAVY_CACHE_PREFIX` + a token derived from the query parameters. The continuation token returned in the probe response encodes this cache key. When the agent follows the `next` cursor, frisian-mcp resolves the cached query context and returns the next page without re-executing the probe. This same cache layer is reused by the write-path continuation token mechanism introduced in ADR-004.

## Why Not DRF Default Pagination Alone

DRF's built-in pagination truncates result sets but does not provide the metadata the agent needs. The difference is:

**DRF default pagination** returns a slice of records. The agent receives records but may not know a slice was taken. It cannot decide whether the slice is complete or partial without inspecting `count` and `next` — which are only present if the ViewSet uses a pagination class that includes them.

**`@mcp_heavy`** guarantees the structured metadata is always present in the response, independent of the ViewSet's pagination class configuration. The decorator enforces this contract at the MCP layer rather than relying on DRF configuration being set correctly for every ViewSet.

Using both — DRF `PAGE_SIZE` plus `@mcp_heavy` annotation — is the recommended production configuration. DRF pagination limits database query cost; `@mcp_heavy` ensures the agent receives actionable metadata regardless of internal configuration details.

## Consequences

**Positive.** Agents calling list endpoints on large datasets no longer risk context window exhaustion from a single call. The first response is bounded to a small metadata envelope plus the first page of results, regardless of total dataset size.

**Positive.** The agent gains information it would not have from a plain paginated response: the total record count, estimated data size, and a cursor for deliberate pagination. These allow better agent decision-making (refine the filter, work with a summary, or paginate knowingly).

**Positive.** The decorator is additive. Applying `@mcp_heavy` to a ViewSet does not change its behavior for non-MCP callers. The standard DRF response path is unaffected; only MCP-routed calls receive the probe-first response.

**Positive.** The auto-negotiate threshold provides a passive safety net. Large responses on undecorated ViewSets are automatically handled without requiring explicit decoration of every ViewSet in a large application.

**Negative.** List operations that previously returned results in one call now require two calls when the agent chooses to paginate: one probe, then one or more page fetches. For agents fetching small, known-bounded datasets — a filter that reliably returns only a handful of records — the probe call is overhead without benefit.

**Negative.** The decorator requires the developer to identify which ViewSets serve large result sets. While auto-negotiation provides a fallback, the optimal configuration — explicit `@mcp_heavy` decoration on the right ViewSets — requires that review.

**Negative.** The two-call pattern introduces dependency on the server-side cache. If the cache is cleared or expires between the probe call and the first page fetch, the agent receives an error on the continuation call. Cache TTL must be set generously enough for realistic agent interaction pacing.

The cost of an extra round-trip on large list operations is far smaller than the cost of exhausting the context window. The probe-then-fetch pattern is the right default for production MCP servers against any dataset that could exceed a few dozen records.

## Validation

The `@mcp_heavy` pattern was validated against a large open-source Django application with production data. Without the decorator, a list call returning a full dataset would produce response payloads that scale linearly with record count, quickly overwhelming any practical context window. With the decorator in place, the first response is bounded to a predictable metadata envelope and a single page of results, giving the agent the total count and pagination cursor needed to proceed.

The same cache infrastructure introduced for `@mcp_heavy` was subsequently reused for the write-path continuation token (ADR-004), confirming the design is general enough to serve both read and write response filtering needs.

---

*ADR maintained alongside the frisian-mcp source. Architecture decision records capture the reasoning behind durable design choices for future maintainers and adopters.*
