# ADR 002: The Dispatcher Pattern for Tool Surface Compression

**Category:** reference  
**Slug:** adr-002-dispatcher-pattern  
**Status:** Accepted  
**Date:** 2026-04-28

---

## Context

An MCP client connecting to a server calls `tools/list` and receives every tool's full schema. The schemas are loaded into agent context at session start. They remain there for the duration of the session.

For small Django applications this is a non-issue. A backend with 20–30 ViewSet actions produces 20–30 tools, costing maybe 5,000–8,000 tokens of schema overhead. Manageable.

The problem appears at scale. A network automation platform integration produced 1,967 discoverable tools across 47+ resources. At roughly 250 tokens per tool schema, that is approximately 490,000 tokens of tool definitions. MCP clients refuse or truncate tool lists at this size. Even when the client accepts the full list, no useful context budget remains for the agent to actually do work.

This is not a corner case. Any DRF application with multiple apps and full CRUD on each tends toward this problem. The fitness application has a narrower MCP surface (16 operations) by deliberate `@mcp_ignore` choices, but its full backend would produce a similar tool count if everything were exposed.

The naive solutions are inadequate:

**Truncating the tool list arbitrarily** loses agent capability silently. The agent does not know which tools are missing.

**Filtering tools by client capability or session intent** requires the client to know what it wants before it knows what is available. Tool selection is the agent's job.

**Documenting the API and asking the agent to construct calls manually** abandons the entire point of MCP — structured tool calls with schemas the agent can validate against.

A different approach is needed: collapse the visible tool surface without losing access to the underlying operations.

## Decision

frisian-mcp implements a **dispatcher pattern** as a first-class package feature. The pattern compresses many underlying operations into a small number of MCP tools by introducing a single layer of indirection.

A dispatcher is one MCP tool that accepts three parameters:

```json
{
  "resource": "device",
  "action": "list",
  "params": { "site": "starbase-1", "limit": 20 }
}
```

The dispatcher routes the call internally based on `resource` and `action`. Calling a dispatcher with `action=help` returns the full resource/action tree available within that group, including parameter schemas — discovered lazily, only when the agent needs to use the tool.

Two configuration paths are supported:

**`FRISIAN_MCP_DISPATCH_GROUPS`** — operator-defined groups mapping group names to resource prefix lists:

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    'dcim':     ['device', 'rack', 'interface', 'cable', 'location'],
    'ipam':     ['ipaddress', 'prefix', 'vlan', 'vrf'],
    'circuits': ['circuit', 'provider', 'circuittermination'],
}
```

Each group becomes one tool. Resources not included in any group remain as flat tools (no breaking change for partial adoption).

**`FRISIAN_MCP_AUTODISPATCH = True`** — automatic resource-level grouping. One dispatcher per resource. No manual configuration required.

Both settings are composable. A project can use auto-dispatch for most resources and define explicit groups for cross-resource concerns.

## Token Math

The numbers from the network automation platform integration:

| Approach | Tools exposed | Schema tokens |
|---|---|---|
| Flat (raw API) | 1,967 | ~490,000 |
| `FRISIAN_MCP_AUTODISPATCH` | ~200 | ~60,000 |
| `FRISIAN_MCP_DISPATCH_GROUPS` | 13 | ~2,000–4,000 |

A 99%+ reduction at the schema level. The agent's full reasoning budget is preserved for actual work.

This compression is at the **tool definition** layer, not the **result payload** layer. When an agent calls `dcim → devices → list` and the underlying ViewSet returns 500 device records, the response payload is the same regardless of how the tool was exposed. Result payload size is what `@mcp_heavy` and pagination address — a separate concern with a separate solution.

The two halves work together. The dispatcher pattern collapses the tool surface so the agent can start working. `@mcp_heavy` keeps individual list responses from blowing up the context once the agent is using the tools. Neither is sufficient on its own for production agent workflows against a large dataset.

## Why Not Filter, Tag, or Namespace?

Several alternatives were considered:

**Tool filtering by tags** — tag tools by domain, let clients request only relevant tags. This puts the burden of knowing what is needed on the client before it has seen what is available. It also requires client-side support that does not exist uniformly across MCP clients today.

**Hierarchical tool namespacing** (e.g. `dcim.device.list` as a separate tool from `dcim.device.create`) — this is what flat tool exposure already does. The token cost is the same. Namespacing helps human readers; it does not reduce the schema payload.

**Lazy tool registration** — only register tools when first called. Breaks the MCP contract: `tools/list` is supposed to be authoritative. Agents that have not yet called a tool would have no way to discover it exists.

**External SEP proposals (SEP-2084 Primitive Grouping, SEP-1300 Tool Filtering)** — SEP-2084 was rejected by the MCP core maintainers after four months of working group participation. The proposal to add Groups as a formal server capability could not reach consensus, with server-side use cases and client-side use cases pulling in incompatible directions. SEP-1300 (Tool Filtering) remains in discussion but has no reference implementation. The dispatcher pattern does not depend on either proposal — it is implementable in the existing MCP specification without any spec changes, and production deployments are running it today.

The dispatcher pattern is the cleanest solution that works within the current MCP specification, requires no client-side changes, and produces measurable token savings on real systems.

## Consequences

**Positive.** Production-scale Django applications become viable MCP servers without overwhelming agent context. The 1,967-tool integration demonstrates this directly.

**Positive.** Adoption is incremental. A project can start with flat tools and migrate to dispatchers over time. Resources outside dispatch groups continue to work as flat tools.

**Positive.** The pattern is decoupled from the underlying ViewSets. The dispatcher tool is a thin routing layer; the ViewSet does its normal job. No business logic moves into the dispatcher.

**Negative.** An additional tool call is required when the agent first encounters a resource within a group — the `action=help` call to discover the parameter schema. This is a one-time cost per resource, and the schema is then in agent context for the rest of the session. Steady-state agents that already know the resource vocabulary skip the help call entirely.

**Negative.** Operators with very flat data models gain less from the pattern. A Django app with 30 unrelated resources does not have natural groupings. For these projects, `FRISIAN_MCP_AUTODISPATCH = True` provides automatic resource-level grouping without requiring operator decisions, but the savings are smaller than for projects with clear domain hierarchies.

**Negative.** Tool descriptions for dispatchers are necessarily generic ("Operations on the dcim domain"). Specific tool descriptions for individual operations live in the help response rather than in `tools/list`. Agents that select tools purely from `tools/list` descriptions may need to invoke `action=help` before they can choose the right operation.

This last consequence is the strongest argument for explicit `@mcp_dispatcher` registration over auto-dispatch: a developer who writes the dispatcher description by hand can craft language that helps the agent select the right group without the help round-trip.

## Validation

The dispatcher pattern is validated in production across two consumer applications and one large enterprise integration. The 13-group configuration on a 1,967-tool platform reduced the exposed surface by 99.3% while preserving full operational coverage. Agents successfully completed multi-step workflows (build infrastructure, validate state, render configurations) against the dispatched surface with no loss of capability compared to the flat alternative.

The pattern originated in the multi-agent orchestration platform documented elsewhere on this server, where ten dispatchers represent 70–90 underlying operations. frisian-mcp generalizes the pattern as a configurable package feature available to any Django application.

---

*ADR maintained alongside the frisian-mcp source.*
