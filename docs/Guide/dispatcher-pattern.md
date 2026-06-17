# The Dispatcher Pattern

**Category:** guide  
**Slug:** dispatcher-pattern  
**Audience:** Developers configuring frisian-mcp for large Django applications

---

## What Is the Dispatcher Pattern

The dispatcher pattern is frisian-mcp's solution to a specific problem: a large Django application may expose hundreds or thousands of DRF operations, but loading all of them into an MCP server at full resolution would overwhelm the agent's context window before any work begins.

A dispatcher is a single MCP tool that wraps a group of underlying operations. Instead of registering 300 individual tools, you register one tool. The agent calls that tool with a `resource` and `action` parameter, and the dispatcher routes the call to the appropriate ViewSet method internally. The agent discovers what operations are available within the group by calling `action=help` — a lazy, on-demand schema fetch — rather than having all schemas preloaded at session start.

The result is a dramatic reduction in up-front context cost. A large Django application with 634+ API operations exposed flat as MCP tools produces approximately 240,000 tokens of tool schema. Grouped into 13 dispatchers, the initial context load drops to approximately 2,000 tokens — a 99% reduction. The agent still has access to every operation; it simply discovers them progressively rather than receiving them all at once.

---

## Why the Dispatcher Exists

See [The Token Problem](the-token-problem.md) for the full analysis. The short version:

When an MCP client calls `tools/list`, every registered tool's full schema is loaded into agent context. These schemas remain in context for the entire session. A backend with hundreds of operations exposes hundreds of schemas — potentially exceeding the context window before the agent has done any work.

The naive solutions fail:

- **Truncating the tool list arbitrarily** loses agent capability silently. The agent does not know which tools are missing.
- **Filtering by session intent** requires the agent to know what it wants before it knows what is available.
- **Documenting the API** abandons the point of MCP — structured tool calls with schemas the agent can validate against.

The dispatcher pattern solves this within the existing MCP specification, without client-side changes, and without losing access to any operation.

---

## How It Works

### Tool Surface

Without a dispatcher, a Django application with 200 endpoints exposes 200 tools. The agent's session begins with 200 tool schemas loaded.

With dispatcher groups, the same application exposes one tool per group. The agent's session begins with only the group-level tools loaded. A typical 13-group configuration produces approximately 2,000 tokens of schema — enough to fit in the working context of any current MCP client.

### Progressive Discovery

When the agent needs to operate on a resource within a group, it calls the dispatcher with `action=help`:

```json
{
  "resource": "device",
  "action": "help"
}
```

The dispatcher returns the full resource/action tree for that resource, including parameter schemas. This is the one-time cost of using a dispatcher — the agent spends one call learning the schema for a resource it has not used before in this session. After that call, the schema is in the agent's context for the remainder of the session.

Agents that already know the resource vocabulary — for example, in a long-running automated workflow — skip the help call entirely and call operations directly.

### Routing

After discovery, the agent calls operations normally:

```json
{
  "resource": "device",
  "action": "list",
  "params": { "site": "hq-1", "limit": 20 }
}
```

The dispatcher extracts `resource` and `action`, locates the corresponding ViewSet, and dispatches to it. The ViewSet handles the call exactly as it would from any other client. Authentication, permissions, serialization, and filtering all work through the existing DRF stack — the dispatcher is a thin routing layer, not a reimplementation.

---

## Configuration

Two configuration paths are available. They can be used together.

### FRISIAN_MCP_DISPATCH_GROUPS

Explicit operator-defined groups. Each key becomes one MCP tool. The value is a list of resource name prefixes that belong to that group.

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    'dcim':          ['device', 'rack', 'interface', 'cable', 'location', 'site'],
    'ipam':          ['ipaddress', 'prefix', 'vlan', 'vrf', 'asn'],
    'circuits':      ['circuit', 'provider', 'circuittermination'],
    'tenancy':       ['tenant', 'tenantgroup'],
    'virtualization':['virtualmachine', 'vminterface', 'cluster'],
    'extras':        ['tag', 'customfield', 'webhook'],
}
```

With this configuration, the agent sees six tools: `dcim`, `ipam`, `circuits`, `tenancy`, `virtualization`, and `extras`. Each tool routes to the ViewSets whose names begin with the listed prefixes.

Resources not listed in any group remain as flat tools. This means partial adoption is safe: you can group the domains you want to compress and leave others as flat tools until you are ready to group them.

**Choosing group boundaries:** Natural domain boundaries make the best groups. A DRF application structured around apps — each app with its own models and ViewSets — typically maps cleanly. Group by app. Resources within the same app are usually related enough that an agent working on one may need others; keeping them in one group reduces help calls.

### FRISIAN_MCP_AUTODISPATCH

Automatic grouping with no operator configuration required.

```python
FRISIAN_MCP_AUTODISPATCH = True
```

When `FRISIAN_MCP_AUTODISPATCH = True`, frisian-mcp automatically creates one dispatcher per resource. A resource named `device` becomes a dispatcher that wraps all device operations: `device_list`, `device_create`, `device_retrieve`, `device_update`, `device_partial_update`, `device_destroy`, and any `@action` methods on the DeviceViewSet.

Auto-dispatch reduces the tool count significantly but not as dramatically as explicit groups: one dispatcher per resource versus one dispatcher per domain. A 200-operation application with 40 resources auto-dispatched exposes 40 tools; the same application with 6 explicit groups exposes 6 tools.

Auto-dispatch is the right starting point. Move to explicit groups once you understand the natural domain boundaries in your application.

### Composing Both

The two settings compose. A project can define explicit groups for high-value domains and let auto-dispatch handle the rest:

```python
FRISIAN_MCP_AUTODISPATCH = True

FRISIAN_MCP_DISPATCH_GROUPS = {
    'dcim': ['device', 'rack', 'interface', 'cable', 'location', 'site'],
    'ipam': ['ipaddress', 'prefix', 'vlan', 'vrf', 'asn'],
}
```

Resources covered by an explicit group are routed through that group's dispatcher. Resources not covered by any explicit group are auto-dispatched.

---

## Explicit Dispatcher Registration

For full control over the dispatcher's name, description, and action implementations, register dispatchers explicitly using `@mcp_dispatcher` and `@mcp_action`:

```python
from frisian_mcp.decorators import mcp_dispatcher, mcp_action

@mcp_dispatcher(name='network_infrastructure')
class NetworkInfrastructureDispatcher:

    @mcp_action(
        description='List network devices filtered by site, role, or status. '
                    'Returns count and pagination metadata for large result sets.',
    )
    def list_devices(self, request, params):
        site = params.get('site')
        ...
        return Response(data)

    @mcp_action(
        description='Create a new network device with full DCIM record.',
        write=True
    )
    def create_device(self, request, params):
        ...
```

Explicit registration matters most for tool description quality. Auto-discovered dispatchers receive generic descriptions ("Operations on the dcim domain"). An explicitly registered dispatcher can carry descriptions that help the agent select the right group without a help round-trip, and action descriptions that give the agent confidence it is calling the right operation.

For production agent workflows with complex multi-resource operations, explicit dispatchers also allow custom logic that spans ViewSets — provisioning a device that requires simultaneous DCIM, IPAM, and DNS operations can be wrapped in a single dispatcher action rather than requiring the agent to coordinate three separate tool calls.

---

## The Agent Experience

From the agent's perspective, the dispatcher pattern adds one discovery step when first encountering an unfamiliar resource. Steady-state operation looks like any other tool call.

**Session start:** Agent calls `tools/list` and receives the dispatcher group tools (e.g., 13 tools at ~2,000 tokens total). The agent can read all tool descriptions and understand the domain structure.

**First use of a resource:** Agent calls the dispatcher with `action=help` and receives the full resource/action schema for that resource. One round-trip, then the schema is in context.

**Subsequent operations:** Agent calls operations directly. No help round-trip unless the agent encounters a resource it has not used before in this session.

**Agents that already know the API:** Automated pipelines that have been trained on the resource vocabulary skip the help step entirely. They call operations directly from the first call.

---

## Relationship to @mcp_heavy

The dispatcher pattern reduces context cost at the **tool definition** layer — the schemas loaded at session start. `@mcp_heavy` reduces context cost at the **result payload** layer — the response returned when a tool is called.

These are complementary solutions to different parts of the same problem. A session with dispatcher-compressed tools but no result filtering can still exhaust the context window on a single large list response. A session with result filtering but no dispatcher compression can exhaust the context window before the first tool call.

For production agent workflows against large datasets, both are needed. See [Read-Response Filtering](read-response-filtering.md) for the `@mcp_heavy` guide.

---

## Performance Considerations

**Help call overhead:** The one-time help call per resource is a single MCP round-trip. For most agent workflows, this is negligible — the saved context budget across all subsequent calls far outweighs the cost of a few help calls at session start.

**Group granularity:** Coarser groups (more resources per dispatcher) produce smaller initial context but more help calls when the agent first uses each resource. Finer groups (fewer resources per dispatcher) produce slightly larger initial context but fewer help calls during active work. For most applications, grouping by app domain is the right granularity.

**Steady-state agents:** Agents running the same workflow repeatedly — automated pipelines, scheduled jobs — pay the help call cost once, then operate at full speed with no discovery overhead. The dispatcher pattern is particularly cost-efficient for repeated workflows.

---

*Document maintained alongside the frisian-mcp source. See [ADR 002](../ADR/adr-002-dispatcher-pattern.md) for the architectural decision record.*
