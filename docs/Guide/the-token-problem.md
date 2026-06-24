# The Token Problem at MCP Scale

**Category:** guide  
**Slug:** the-token-problem  
**Audience:** Developers and architects evaluating frisian-mcp for production use

---

## What This Document Is

This document explains the core problem frisian-mcp exists to solve. The package's design choices — the dispatcher pattern, the `@mcp_heavy` decorator, the auto-registration of URLs, the permission tier filtering — all flow from one underlying issue: **agent context windows are finite, and naive MCP exposure consumes them before the agent does any work.**

The token math is grounded in numbers measured against three real production systems. Nothing in this document is theoretical.

---

## Three Different Token Problems

The phrase "token usage" gets used loosely. There are actually three distinct problems with different solutions:

**Problem 1: Tool definition bloat.** When an MCP client calls `tools/list`, every registered tool's full schema is loaded into agent context at session start. These schemas remain in context for the entire session. A backend with hundreds of operations exposed as flat tools loads hundreds of schemas — potentially exceeding the context window before any actual work begins.

**Problem 2: Result payload bloat.** When an agent calls a tool that returns a large result set — list operations on tables with hundreds or thousands of rows — the entire result is loaded into context. A single `device_list` call returning 500 records can consume tens of thousands of tokens.

**Problem 3: Write-echo bloat.** When an agent creates or updates objects, the conventional response echoes the full serialized object back. For bulk operations, this echo scales with the number of objects written and can consume tens of thousands of tokens before the agent has processed a single result.

These look similar but require different solutions. The dispatcher pattern addresses tool definition bloat. The `@mcp_heavy` decorator and pagination address result payload bloat. The `@mcp_light` lean envelope addresses write-echo bloat. **No single solution is sufficient on its own for production agent workflows against large datasets.**

---

## Problem 1: Tool Definition Bloat

### The Math

Every MCP tool ships with a JSON schema describing its name, description, and input parameters. The schema size depends on parameter count and complexity, but a reasonable average for a fully-featured DRF endpoint is ~250 tokens per tool — accounting for filter parameters, pagination controls, field selection, and response shape hints.

| System | Tools (flat) | Schema tokens (flat) |
|---|---|---|
| Small Django app (20 endpoints) | 20 | ~5,000 |
| Medium Django app (100 endpoints) | 100 | ~25,000 |
| Network automation platform | 1,967 | ~490,000 |

The 1,967-tool number is from a real integration: a DRF application with 47+ resources across multiple apps (DCIM, IPAM, Circuits, Tenancy, Extras, Virtualization, VPN, Wireless), each with full CRUD plus bulk operations. The number is what `DRFSyncDiscovery` produced on a fresh scan with no filtering applied.

For context, current MCP clients have practical tool list limits well below 100,000 tokens. Many refuse or truncate tool lists at much lower thresholds. At ~490,000 tokens, the tool list itself exceeds reasonable agent context budgets.

### Why Not "Just Use a Bigger Context Window"

Even when the client accepts the full tool list, the agent's reasoning budget is what matters. A 200,000-token context window with 490,000 tokens of tool schemas is unusable. A 200,000-token context window with 60,000 tokens of tool schemas leaves 140,000 tokens for reasoning, conversation history, retrieved data, and results — workable.

The goal is not "fit the tools in" but "leave room for the agent to actually think."

### The Dispatcher Solution

The dispatcher pattern collapses many underlying operations into a small number of MCP tools. A dispatcher accepts `resource` and `action` parameters and routes the call internally. Tool schemas are loaded lazily — the agent calls `action=help` on a dispatcher to discover the resource/action tree only when needed.

| Approach | Tools exposed | Schema tokens |
|---|---|---|
| Flat (raw API) | 1,967 | ~490,000 |
| `FRISIAN_MCP_AUTODISPATCH = True` | ~200 | ~60,000 |
| `FRISIAN_MCP_DISPATCH_GROUPS` | 13 | ~2,000–4,000 |

A 99%+ reduction at the schema level. The agent's full reasoning budget is preserved.

The two configuration paths differ in operator effort versus precision:

- **`FRISIAN_MCP_AUTODISPATCH`** automatically groups by resource. One dispatcher per resource, no operator decisions required. Useful starting point.
- **`FRISIAN_MCP_DISPATCH_GROUPS`** lets the operator define logical domain groupings (e.g. "all DCIM resources under one dispatcher"). More precise compression at the cost of explicit configuration.

Both compose. A project can use auto-dispatch for most resources and explicit groups for cross-resource concerns.

### Real Numbers

The numbers below are measured, not estimated:

**Network automation platform** — full integration with 1,967 discoverable tools:

```text
Flat exposure:           ~490,000 tokens of tool schemas
Group dispatch (13):       ~2,000–4,000 tokens
Reduction:                 99.3%
```

**Multi-agent orchestration platform** (production) — 70–90 underlying operations exposed as 10 dispatchers:

```text
Flat exposure:           ~22,500 tokens of tool schemas  
Dispatcher exposure:       ~2,000 tokens
Reduction:                 91%
```

**Consumer fitness application** — narrow MCP surface by deliberate `@mcp_ignore` choice. 16 operations across 3 dispatchers + 1 RAG tool. No dispatcher pattern needed; the surface is already small. Schema cost: ~1,500 tokens. The fitness application demonstrates that the dispatcher pattern is a tool, not a requirement — surfaces small enough to expose flat should stay flat.

See [Dispatcher Pattern](dispatcher-pattern.md) for the full developer guide.

---

## Problem 2: Result Payload Bloat

### The Math

Tool definition tokens are consumed once per session. Result payload tokens are consumed every time the agent calls a tool. A list operation returning 500 records, at ~620 tokens per record (typical for a fully serialized network device with nested relationships), produces a single response of approximately 310,000 tokens.

That single response, by itself, exceeds most agent context budgets. The agent has not thought about the data, has not made a decision, has not done anything except receive the result.

| Dataset Size | Without `@mcp_heavy` | With `@mcp_heavy` |
|---|---|---|
| 65 devices | ~40,300 tokens | ~31,000 tokens |
| 500 devices | ~310,000 tokens | ~31,000 tokens |
| 2,000 devices | ~1,240,000 tokens | ~31,000 tokens |

The 65-device numbers are from a validated production network automation integration. The 500 and 2,000 numbers are linear extrapolations. At 500 devices, an unpaginated response would consume more context than most agents have available. At 2,000 devices, the context window is gone before the response finishes loading.

### The @mcp_heavy Solution

The `@mcp_heavy` decorator enforces pagination-first behavior. When an agent calls a list endpoint decorated with `@mcp_heavy`, the response includes:

- `count` — total number of records matching the query
- `next` — URL or cursor to fetch the next page
- `previous` — URL or cursor to fetch the previous page (where applicable)
- `results` — first page of records (default 50)

The agent receives the metadata it needs to decide what to do — paginate, refine the filter, work with the summary, or accept the truncation. The context window is not pre-filled with records the agent may never need.

```python
from frisian_mcp.decorators import mcp_heavy

class DeviceViewSet(viewsets.ModelViewSet):

    @mcp_heavy
    def list(self, request):
        queryset = self.get_queryset()
        ...
```

The decorator does not prevent the agent from accessing all 500 records. It changes the access pattern from "load everything by default" to "load metadata, decide, then paginate as needed."

### Why Not "Just Set a Default Limit"

DRF already supports default pagination via `PAGE_SIZE` in `REST_FRAMEWORK` settings. `@mcp_heavy` differs in two ways:

**It enforces pagination metadata in the response.** A standard DRF paginated response includes `count` and `next`. A non-paginated DRF response is a bare list. `@mcp_heavy` ensures the agent receives the structured metadata regardless of the underlying ViewSet's pagination configuration.

**It marks the operation explicitly.** The decorator signals to the schema generation layer that this tool returns metadata, not a complete dataset. The agent's tool description can include this hint, helping the agent make better calls.

Default pagination at the DRF layer plus `@mcp_heavy` annotation is a reasonable belt-and-suspenders configuration for production use.

See [Read-Response Filtering](read-response-filtering.md) for the full developer guide.

---

## Problem 3: Write-Echo Bloat

### The Problem

Tool definition tokens are consumed once per session. Result payload tokens are consumed when the agent reads data. Write-echo tokens are consumed when the agent writes data — and they add up fast in multi-step provisioning workflows.

When an agent creates or updates objects, the conventional DRF response echoes the full serialized object back. For a single-object create, this is modest. For bulk operations, the cost scales directly with the number of objects written.

A 60-device bulk create in a production network automation integration session produced a full echo response of approximately 10,798 tokens (43,190 bytes). At approximately 603 tokens per device, the cost is proportional: a 200-device bulk create would produce roughly 36,000 tokens from the echo alone. For a provisioning workflow that writes devices, assigns IPs, configures VLANs, and registers DNS — each step a bulk write — the accumulated echo cost can exhaust the context window before the agent completes the workflow.

Unlike result payload bloat, write-echo bloat is structurally predictable: the agent provided all the data, so the echo is a repetition of what the agent sent. For the most common post-write use case — confirming success and continuing — the full echo is waste.

### The @mcp_light Solution

The `@mcp_light` feature applies a lean confirmation envelope to all write operations by default. Instead of echoing the full serialized object, frisian-mcp returns a small set of identifying fields plus metadata:

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

The lean envelope for the 60-device bulk create is approximately 24 tokens (95 bytes) — a 99.8% reduction from the full echo.

### Real Numbers

The measured figures from the production integration session:

```text
60-device bulk create, full echo:   ~10,798 tokens (43,190 bytes)
60-device bulk create, lean envelope:    ~24 tokens (95 bytes)
Reduction:                             99.8%

Per-device object size:               ~603 tokens (~3,800 bytes)
```

The 99.8% reduction is constant regardless of bulk size. The lean envelope is a fixed-size structure; the full echo grows linearly. The larger the bulk operation, the greater the absolute saving.

### Full Object Access

The lean envelope includes a `continuation_token`. The agent can retrieve the full serialized result via the heavy-fetch path without re-executing the write. For cases where the full result is needed immediately, the agent can pass `verify=True` on the write call to receive the complete echo inline.

See [Write-Path Response Filtering](write-path-response-filtering.md) for the full developer guide, including `@mcp_light_key` serializer annotation.

---

## How All Three Solutions Work Together

The dispatcher pattern collapses the tool surface so the agent can see what is available without consuming the context budget. `@mcp_heavy` keeps individual list responses from blowing up the context once the agent is using the tools. `@mcp_light` keeps write operations from consuming context that should be reserved for reasoning and results.

**Dispatcher without read/write filtering:** Agent sees 13 tools instead of 1,967. Agent calls a list endpoint, receives 500 records, context window exhausted on the first call.

**Read/write filtering without dispatcher:** Agent sees 1,967 tools at session start, context window exhausted before the first call.

**All three:** Agent sees 13 tools. Agent calls a list endpoint, receives metadata and the first page. Agent calls a bulk create, receives a 24-token confirmation. Agent's context budget is preserved for reasoning, state retrieval, and the next step.

The three-part pattern is the complete answer for scale. A production MCP server against any non-trivial dataset needs all three.

---

## What This Means for Adoption

For a small Django app with a handful of ViewSets, none of the three problems is acute. frisian-mcp works fine with all default settings; tool counts, result sizes, and write echoes all stay manageable.

For medium applications, default pagination plus `@mcp_heavy` on any list endpoint that could return more than a few dozen records is the practical baseline. Write-path filtering (`@mcp_light`) is applied automatically by default — no additional configuration is needed unless you need to customize which fields appear in the lean envelope.

For large multi-app applications — the kind of system where MCP is most valuable, because the surface is too large for an agent to navigate without help — the dispatcher pattern is a requirement, not an optimization. `FRISIAN_MCP_AUTODISPATCH = True` is the simplest starting point. Move to `FRISIAN_MCP_DISPATCH_GROUPS` once the natural domain boundaries are clear. Pair with `@mcp_heavy` on list endpoints and rely on the `@mcp_light` lean default for write operations.

---

## A Note on Future MCP Spec Work

The MCP community has draft proposals addressing related problems:

- **SEP-2084** (Primitive Grouping) — formalizes a way to group tools at the protocol level. As of late April 2026 this remains in draft with active discussion.
- **SEP-1300** (Tool Filtering) — would let clients request a filtered subset of tools at connection time.
- **SEP-993** (Namespaces) — addresses tool naming collisions across servers.

frisian-mcp's dispatcher pattern is implementable in the existing MCP specification without spec changes. As these SEPs stabilize, the package can adopt them where they provide additional value — but production users do not need to wait for spec evolution to solve the token problem today.

The dispatcher pattern is the working solution. The SEPs may eventually offer alternative or complementary approaches. Both can coexist.

---

*Document maintained alongside the frisian-mcp source. Numbers measured against production systems and validated across multiple integration sessions.*
