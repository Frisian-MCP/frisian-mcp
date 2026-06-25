# About frisian-mcp

## What This Is

frisian-mcp is a Django package that turns Django REST Framework applications into MCP servers. It exists because traditional MCP implementations dump entire API surfaces into agent context, causing hallucinations, token exhaustion, and crashes at production scale.

This isn't theoretical. This was built after watching agents fail repeatedly when connected to real systems.


## The Problem

If you've tried connecting an agent to a production API via MCP, you've probably seen this:

- Agent gets 400+ tool schemas at session start, or small toolsets with massive payloads that are forced on the connecting agent.
- Context window fills before the agent does any actual work
- Agent hallucinates operations or misuses tools due to cognitive overload
- Single list operation with 500+ records crashes the agent mid-task
- Tool selection takes 15-20 messages because the agent is drowning in options

Traditional MCP servers expose everything eagerly. Agents can't handle it.

## What frisian-mcp Does Differently

### Dispatcher Pattern
Instead of exposing 634 individual tools, frisian-mcp groups them into logical dispatchers. The agent sees one gateway tool and discovers resources progressively as needed. Tool schemas stay out of context until requested.

### @mcp_heavy Decorator
Large result sets get automatic pagination. The agent receives metadata (total count, next URL) instead of 500 records dumped into context. The agent decides whether to paginate, filter, or work with the summary. Context window is preserved.

### Permission-Aware Discovery
Only tools the authenticated user can actually call are exposed. No wasted tokens on permission errors. No retry loops burning context budget.

## Two Use Cases

### Brownfield: Overlay Existing Apps
If you have a Django app with DRF ViewSets, frisian-mcp can expose it via MCP without refactoring. Your existing REST API becomes agent-accessible. URLs, serializers, permissions—everything works as-is.

### Greenfield: Agent-First Architecture
If you're building new systems, frisian-mcp lets you design for agents as users, not consumers. Agents are smart enough to understand what they need. The question shifts from "how do we expose this?" to "how do agents interact with data?"—whether that's network configs, marketing reports, infrastructure state, or anything else. Building agent-first from day one means designing for how agents actually retrieve and act on data — not retrofitting a human API for machine consumption.

In this model, agents are first-class users.

## Design Philosophy

**Agents are users.** Design for agent interaction patterns. Progressive discovery beats eager exposure. Metadata beats data dumps. Let agents decide what they need.

**Data-centric.** It's about data retrieval quality, not tool count. A well-paginated result set with metadata is more useful than 500 unpaginated records.

**Context-aware.** Don't fill the context window with things the agent might never use. Lazy load. Defer discovery. Preserve reasoning budget.

**Production-viable.** This runs on a T3.medium without breaking a sweat. It's built for real systems, not demos.

## What This Isn't

This is not a framework. It's a Django package. Install it, register your ViewSets, and it works.

This is not opinionated about your data models. It introspects your OpenAPI schema and builds MCP tools dynamically. Your models, your serializers, your permissions.

## How It Works

frisian-mcp uses Django's existing infrastructure:

- **Discovery backend:** Reads your DRF OpenAPI schema to find ViewSets and actions (intentionally blocks UI ViewSets)
- **Invocation backend:** Routes MCP calls to your ViewSet methods
- **Permission backend:** Uses Django's permission system to filter exposed operations

Backends are pluggable. Swap them if you need GraphQL discovery, async invocation, or custom auth.

## Real Numbers

We validated this against a Nautobot instance with 634+ API operations:

- **Tool schema overhead:** 2,000 tokens (dispatcher) vs. 240,000+ tokens (flat exposure)
- **Device list (65 devices):** 31,000 tokens (paginated) vs. 40,300 tokens (full dump)
- **Device list (500 devices):** 31,000 tokens (paginated) vs. 310,000 tokens (context crash)
- **Zero errors** across 65-device infrastructure build
- **T3.medium stable** under production-like load

These aren't projections. We measured them.

## Status

Version 1.0.12. Works with Django 5.x, DRF 3.x, Python 3.11+.

Tested against:
- Nautobot (network automation platform)
- Multi-agent orchestration systems
- Fitness tracking applications
- Small/medium business management tools

If you're building agent-accessible Django apps and you're tired of context bloat, this might help.


## Zero-Touch Integration

frisian-mcp does not modify the host application's source code.

No changes to urls.py. No modifications to existing models, serializers, or permissions. No middleware injected into the host's request pipeline.  The entire integration lives inside the installed package — registered via Django's AppConfig.ready() and mounted as a separate URL namespace.

This was validated across every tested integration: Nautobot, NetBox, Paperless-ngx, edX, a multi-agent orchestration platform, and a consumer fitness application. In every case, the host application's codebase was untouched. Existing APIs, UI, and admin interfaces continued to function without modification.

For plugin-based hosts like NetBox, the integration uses a thin plugin wrapper that wires frisian-mcp into the plugin system — again, without touching NetBox core. The wrapper is ~100 lines and lives entirely outside the NetBox source tree.

## Who Built This

Built by an engineer frustrated with context bloat in MCP tooling — where connecting to a large API meant burning an entire agent session just to discover what tools were available. The MCP standard has been a focus since Anthropic introduced it in 2024, through its growth and its donation to the AAIF under the Linux Foundation in December 2025. This package is the result of nearly two years of working with agents in production — learning what works and what doesn't.

frisian-mcp isn't designed only for enterprise systems. The solo developer automating a side project deserves tools that don't hallucinate. The vibe coder building a small business app deserves agent integration that doesn't exhaust context windows. frisian-mcp is for anyone building with Django who wants agents to actually work.

