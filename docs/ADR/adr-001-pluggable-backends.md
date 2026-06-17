# ADR 001: Pluggable Discovery and Invocation Backends

**Category:** reference  
**Slug:** adr-001-pluggable-backends  
**Status:** Accepted  
**Date:** 2026-04-14

---

## Context

frisian-mcp's job is turning Django REST Framework ViewSets into MCP tools. Two distinct things happen in that pipeline:

1. **Discovery** — finding which ViewSets exist, which actions they expose, and what their input schemas look like
2. **Invocation** — when a tool is called, dispatching that call to the right ViewSet method and returning the result

The simplest possible design is to hardcode both: walk the DRF router at startup, generate tools, and dispatch tool calls by constructing synchronous DRF `Request` objects and calling the ViewSet methods directly. This works for a standard Django + DRF + WSGI application.

It does not work for a meaningful share of real Django projects. Three categories surfaced during early integration analysis:

**Mature projects with custom ViewSet base classes.** Network automation platforms inherit from custom `ModelViewSet` subclasses — these break naive `issubclass(cls, ViewSetMixin)` discovery. The default schema generation from DRF serializers does not necessarily produce the right input schema for these projects.

**Projects running under ASGI with async ViewSets or Celery job queues.** A purely synchronous invocation path either deadlocks against the event loop or blocks long-running operations that should return immediately and poll. Async dispatch needs `sync_to_async` boundary handling. Long-running operations need a queue and a task ID.

**Projects with no ViewSets at all.** Function-based views decorated with `@api_view` are common, especially in older Django projects or in apps that grew organically. ViewSet-only auto-discovery produces zero tools on these projects.

A rigid pipeline forces these projects to either fork the package or skip MCP integration entirely. Neither outcome is good for adoption or for the AAIF positioning the package aims for.

## Decision

Discovery and invocation are separated into two independent backend contracts with stable base classes:

```python
class BaseDiscoveryBackend:
    def discover_tools(self) -> list[ToolDefinition]:
        raise NotImplementedError

    def get_input_schema(self, view_class, action: str) -> dict:
        raise NotImplementedError


class BaseInvocationBackend:
    def invoke(self, tool: ToolDefinition, arguments: dict, request) -> ToolResult:
        raise NotImplementedError

    def get_request_context(self, mcp_request) -> RequestContext:
        raise NotImplementedError
```

The package ships two default implementations:

- **`DRFSyncDiscovery`** — scans Django URL patterns for DRF ViewSets registered with routers. Generates one tool per action (list, retrieve, create, update, partial_update, destroy, plus any `@action` decorators). Tool schemas derived from DRF serializer fields. Respects `@mcp_ignore`.
- **`SyncInvocation`** — constructs a synthetic DRF `Request`, dispatches to the ViewSet action method, returns the result.

Both are configured via dotted-path settings:

```python
FRISIAN_MCP_DISCOVERY_BACKEND = "frisian_mcp.backends.DRFSyncDiscovery"
FRISIAN_MCP_INVOCATION_BACKEND = "frisian_mcp.backends.SyncInvocation"
```

Projects that need different behavior subclass or replace either backend independently.

## Why Two Backends Instead of One

A project might use the default DRF discovery but need custom invocation — for example, a standard Django + DRF application running under ASGI where every call needs `sync_to_async` wrapping. Or the inverse: explicit `@mcp_tool` registration (bypassing discovery entirely) but standard sync invocation.

A single combined "MCP backend" interface would force projects to reimplement both halves when they only needed to customize one. Splitting the contracts means a project can swap the half it needs and keep the default for the other.

## Consequences

**Positive.** Mature Django projects can adopt frisian-mcp without forking. The package serves as a protocol and registry layer, with project-specific logic plugging in cleanly. The AAIF submission story is "extensible framework" rather than "rigid package."

**Positive.** Future invocation backends — `AsyncInvocation`, `CeleryInvocation` — can ship as additions without breaking existing installs. The extension points exist from v1; the built-in implementations expand over time.

**Positive.** `@mcp_tool` manual registration works as a complete alternative to auto-discovery. Projects with no ViewSets register tools explicitly and skip the discovery backend entirely. This is how the consumer iOS application documented elsewhere on this server integrates — zero ViewSets, all tools registered via decorator.

**Negative.** Two contracts to maintain rather than one. Documentation and testing surface area is roughly doubled.

**Negative.** Configuration has more moving parts. Operators who want to customize must understand the discovery/invocation split rather than overriding a single class.

The configuration cost is paid by projects that need customization. The default settings work for the standard case without operator intervention. This trade-off is the right one for a package targeting both small Django apps and large multi-app frameworks.

## Validation

The pluggable architecture has been validated against three real Django backends:

- A consumer iOS fitness application (function-based views only — auto-discovery returns zero tools, all tools registered via `@mcp_tool`)
- A network automation platform with 1,967 discoverable tools across 47+ resources (custom ViewSet base classes, default discovery backend with `FRISIAN_MCP_DISPATCH_GROUPS`)
- A multi-agent orchestration platform (custom dispatchers exposing 10 tools that represent ~70–90 underlying operations)

All three use the same package. The discovery and invocation choices differ; the protocol and registry layer is identical.

---

*ADR maintained alongside the frisian-mcp source. Architecture decision records capture the reasoning behind durable design choices for future maintainers and adopters.*
