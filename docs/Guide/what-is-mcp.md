# What Is MCP?

**Category:** guide  
**Slug:** what-is-mcp  
**Audience:** Developers new to the Model Context Protocol

---

## What This Document Is

A practical primer on the Model Context Protocol for developers who haven't worked with it directly. If you've been hearing about MCP, seeing it mentioned alongside Claude and other AI clients, and wondering what it actually is and what it does — this is for you.

This document is not the official MCP specification. The protocol is maintained by Anthropic and contributors, with a public specification document and a growing ecosystem of SDKs, servers, and clients. This is a developer's working understanding of what the protocol does, why it exists, and what it means to "build an MCP server."

---

## The Short Answer

MCP is a protocol that lets AI agents call tools you define. You write a server that exposes operations — read this database, search these documents, create this record. An AI client like Claude or GPT connects to your server, discovers what tools are available, and calls them when the agent decides those operations would help complete a task.

The protocol uses JSON-RPC 2.0 over HTTP. The server speaks a small set of protocol methods: `initialize`, `tools/list`, `tools/call`, plus a few others. The client handles the rest — sending the right messages at the right time, presenting tool results to the agent, deciding when to call tools.

That is the entire protocol at a high level. Everything else is detail.

---

## Why MCP Exists

Before MCP, integrating an AI agent with an external system required custom code on both sides. You wrote a tool wrapper specific to OpenAI function calling, or specific to Anthropic's tool use API, or specific to whatever SDK you were using. Switching clients meant rewriting the integration. Adding a new system meant teaching every agent client about it individually.

MCP standardizes the integration layer. Write the server once. Any MCP-compatible client can connect to it. The agent on the other side does not need to know which specific server it is talking to — the protocol handles the negotiation.

This is the same idea as USB for hardware peripherals. Before USB, every printer needed a specific cable and driver for every computer. After USB, you plug in the printer and it works. The protocol absorbs the integration complexity so individual products do not have to.

For Django developers specifically, MCP solves a real problem: you have a working REST API that already has serializers, permissions, and view logic. You want agents to use that API. Without MCP, you write a separate tool layer for each agent client. With MCP, you expose your existing ViewSets and any compliant agent can use them.

---

## The Core Protocol Methods

An MCP session has a defined lifecycle. The methods are simple.

**`initialize`** — protocol handshake. The client sends its capabilities and the protocol version it speaks. The server responds with its own capabilities and confirms the version. This is the first message of every session.

**`initialized`** — handshake completion notification from client to server. No response expected. Marks the start of normal operation.

**`tools/list`** — the client asks the server "what tools do you have?" The server responds with a list of every tool: name, description, and input schema. The agent now knows what operations are available.

**`tools/call`** — the client invokes a specific tool with arguments. The server runs the tool and returns the result. This is the workhorse method — every actual operation flows through it.

There are additional methods for resources (file-like data the agent can read), prompts (templated prompts the server can offer), and notifications (server-pushed events). For most Django MCP servers, the four methods above cover the entire interaction surface.

---

## What "tools/list" Actually Does to Agent Context

This is where the practical implications of MCP become important for server design.

When the client receives the `tools/list` response, it loads every tool's full schema into the agent's context window. These schemas remain in context for the rest of the session. The agent uses them to decide which tools to call and how to construct the arguments.

For a server with 20 tools, this is a few thousand tokens. Manageable. The agent has plenty of context budget remaining for actual reasoning.

For a server with 2,000 tools, this can be hundreds of thousands of tokens. The tool list alone exceeds the agent's available context. The agent cannot do useful work because there is no context budget left.

This is the core problem frisian-mcp addresses with the dispatcher pattern, documented separately as ADR 002 and explained in detail in "The Token Problem at MCP Scale." Understanding the `tools/list` mechanic is the foundation for understanding why those design choices matter.

---

## What "tools/call" Looks Like

A tool call is a JSON-RPC request:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "exercise_list",
    "arguments": {
      "category": "strength",
      "limit": 10
    }
  }
}
```

The server dispatches this to the registered tool, runs whatever logic the tool defines (which for frisian-mcp is typically a DRF ViewSet action), and returns the result:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{ \"count\": 47, \"next\": ..., \"results\": [...] }"
      }
    ]
  }
}
```

The agent receives the result, reasons about it, and decides what to do next — call another tool, ask the user a follow-up question, produce a final answer. The protocol gets out of the way.

---

## Authentication

MCP itself does not specify an authentication mechanism. The protocol assumes the client and server agree on how to authenticate; the specification points at standard transport-layer mechanisms (Bearer tokens, OAuth) without mandating any particular one.

In practice, AI clients have converged on OAuth 2.0:

- **Claude.ai and Claude Code** expect OAuth 2.0 with Authorization Code + PKCE (S256). They use RFC 9728 (Protected Resource Metadata) and RFC 8414 (Authorization Server Metadata) for discovery, and RFC 7591 for dynamic client registration.
- **GPT/ChatGPT Actions** also use OAuth 2.0 Authorization Code + PKCE, with similar discovery requirements but a different redirect URI pattern.
- **Coding agents** (Cursor, Windsurf, etc.) typically support both OAuth and simpler Bearer token authentication, configurable per server.

frisian-mcp ships `frisian_mcp.contrib.oauth` to handle the full OAuth 2.0 flow for any host application without requiring custom auth code. The contrib module implements all the discovery endpoints, the authorization flow with PKCE verification, and dynamic client registration. Operators configure `FRISIAN_MCP_OAUTH_ISSUER` and the rest works automatically.

For simpler use cases, `frisian_mcp.contrib.tokens` provides Bearer token authentication with database-backed token management.

---

## Transport

MCP supports multiple transports. The two most relevant for web-based servers are:

**Streamable HTTP** — request/response over HTTP, with optional Server-Sent Events for push notifications. This is what frisian-mcp implements. Standard HTTP infrastructure (load balancers, reverse proxies, TLS termination) works without modification.

**STDIO** — the client launches the server as a subprocess and communicates via standard input/output. Used for local tool servers (filesystem access, git operations) where the server runs on the same machine as the client.

frisian-mcp focuses on Streamable HTTP because that is the right transport for a Django application running behind a real web server. STDIO support is on the package roadmap but not the primary integration path.

---

## What an MCP Server Actually Does

Strip away the protocol vocabulary and an MCP server is doing three things:

1. **Listening** for HTTP requests at a specific endpoint
2. **Translating** JSON-RPC method calls into operations on the underlying application
3. **Returning** results in the format the protocol specifies

For a Django application using frisian-mcp, the underlying application is your existing DRF ViewSets. The translation layer is automated — frisian-mcp introspects your serializers and routes tool calls to ViewSet methods. You do not write the protocol layer; you configure it.

The work that remains for you as a developer is:

- Decide which ViewSets to expose (the rest are marked `@mcp_ignore`)
- Decide whether the surface is small enough to expose flat, or large enough to need the dispatcher pattern
- Configure authentication appropriate to your deployment
- Apply `@mcp_heavy` to any list endpoint that could return large result sets

That is the developer-facing surface. The protocol mechanics happen below.

---

## Common Misconceptions

**"MCP is an AI feature."** It is a protocol. The fact that AI clients are the primary consumers does not make the protocol AI-specific. An MCP server is just a server that speaks JSON-RPC over HTTP with a specific schema. Anything that can speak JSON-RPC can be an MCP client — including command-line tools, test harnesses, and other automation.

**"MCP servers run AI models."** They do not. The AI is on the client side. The server provides tools the AI can use. frisian-mcp running on a Django backend is just a normal Django process — there is no model inference happening server-side.

**"MCP is a replacement for REST APIs."** It is not. frisian-mcp specifically exposes existing REST APIs through an additional protocol surface. The REST API continues to work normally for browsers, mobile apps, and other consumers. MCP is an additional way to access the same operations, not a replacement.

**"MCP requires a special framework."** It does not. The protocol is well-defined enough that any HTTP server can implement it. frisian-mcp exists because *automatic introspection of DRF ViewSets* is valuable for Django developers — the protocol itself can be implemented manually, and several other implementations exist. The package is a productivity layer on top of a standard protocol.

---

## Where to Go From Here

If you are evaluating frisian-mcp:

- "Getting Started" walks through installation and the minimum configuration to expose a Django app via MCP.
- "Installation & Configuration Reference" documents every setting and decorator.
- "The Token Problem at MCP Scale" explains why the dispatcher pattern matters at scale and shows the measured numbers from real integrations.

If you want to go deeper on the protocol itself:

- The MCP specification is the authoritative reference. MCP is now hosted by the Linux Foundation — governance moved from Anthropic-led to foundation-led as of 2026, alongside official SDKs maintained in collaboration with Microsoft (C#) and Google (Go).
- The MCP community publishes specification enhancement proposals (SEPs) for protocol evolution. SEP-2084 (Primitive Grouping) — the most-discussed proposal for addressing the tool surface scale problem — was rejected by core maintainers after four months of working group review. The community could not reach consensus between server-side organization use cases and client-side filtering use cases. The tool surface problem frisian-mcp addresses through the dispatcher pattern remains unsolved at the protocol level.

frisian-mcp's role is to implement the server side of the protocol cleanly for Django + DRF applications, including solving problems the protocol itself has not yet addressed. Understanding the protocol gives you the foundation to use the package effectively and to understand why specific design decisions were made.

---

*Document maintained alongside the frisian-mcp source.*
