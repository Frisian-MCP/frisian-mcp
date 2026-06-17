# ADR 006: Repeated-Path Token Reduction via Per-Call `lite` Parameter

**Category:** reference  
**Slug:** adr-006-repeated-path-token-reduction  
**Status:** Accepted  
**Date:** 2026-06-04

---

## Context

When an agent uses the dispatcher pattern over a multi-step session, each time it calls `tools/list` it receives the full tool manifest — name, description, and `inputSchema` for every tool visible to that endpoint. For a session provisioning 100 devices in sequence, the agent may call `tools/list` once per logical step to re-orient. If the manifest contains 30 DCIM tools with full schemas, that single call costs roughly 6,000 tokens. Over 100 provisioning steps, repeated orientation calls account for a material fraction of the context budget before any real data work begins.

The same pattern appears with the `help` method and with dispatcher sub-tool listings. When an agent calls a dispatcher group tool, the response includes the full `inputSchema` for every sub-tool. On the first call this is necessary — the agent is learning the surface. On the second through hundredth call, the agent already knows the surface; the schema payload is wasted tokens.

This is structurally different from the read-path and write-path problems addressed by `@mcp_heavy` and `@mcp_light`. Those deal with response size from data operations. This problem is protocol overhead that accumulates from the MCP handshake layer itself — orientation metadata the agent receives whether it needs it or not.

---

## Approaches Considered

### Approach A: /mcp-lite URL Overlay (Rejected)

**How it works.** A Django `process_request` middleware intercepts requests before URL resolution. If `request.path_info` starts with `/mcp-lite`, the middleware rewrites the path to `/mcp` and stamps `request._mcp_lite = True`. URL resolution then proceeds normally. All routes under `/mcp` are automatically available under `/mcp-lite` with no additional URL configuration. When `_mcp_lite` is True, `tools/list` returns an empty list, orientation metadata is suppressed in dispatcher responses, and agents may pass `help=true` in any `tools/call` arguments for a per-call schema opt-in.

**Why rejected.** This approach requires a separate agent connector. Agents must be configured to connect to a second endpoint URL. An agent already using `/mcp` cannot use lite mode without reconfiguring its connection to point at `/mcp-lite`. The `/mcp-lite` overlay was implemented and tested before this problem was identified. The separate-connector requirement adds deployment friction — host applications must mount two URL prefixes and register middleware, and agents in multi-agent setups must be deliberately configured to the correct path. An agent that connects to `/mcp` (the default) cannot opt into lite mode on a single call; it must be rebuilt or reconfigured.

### Approach B: Separate Registered 'Lite Mode' Tool (Rejected)

**How it works.** A dedicated MCP tool (for example, `set_lite_mode`) is registered alongside all other tools. The agent calls it to toggle a session-level flag. Subsequent responses are stripped of orientation scaffolding until the flag is cleared or the session ends.

**Why rejected.** The contract between the toggle tool and the suppression behavior is an implicit dependency that drifts as the codebase evolves. When a developer changes the suppression logic in `_handle_tools_call`, there is no static link to `set_lite_mode` that forces them to verify the two remain consistent. In production, this creates a class of failures that are difficult to diagnose: when an agent behaves unexpectedly, the first question is always "did it call `set_lite_mode` first, and did that call succeed?" The session-level state model also puts the burden on the agent to verify its own session configuration before trusting that lite mode is active. Per-call declaration is simpler and removes this uncertainty entirely.

### Approach C: `lite: true` Per-Call Parameter (Accepted)

**How it works.** The agent passes `"lite": true` in any `tools/call` arguments object on the existing `/mcp` connection. The MCP layer extracts and strips the flag before dispatch — tool implementations never see it. The response is post-processed to suppress instructional scaffolding. On failure (dispatch error, bad or unknown parameters), the error response re-includes the tool's `inputSchema` from the registry so the agent can recover without switching modes.

**Why accepted.** The agent uses its existing `/mcp` connection without reconfiguration. No second endpoint to mount, no middleware to install, no separate path to document. Per-call granularity means the agent decides on each individual call whether it needs scaffolding — a single session can mix oriented calls and lean calls freely. Failure is safe by design: a `lite: true` call that fails returns the schema in the error response, so the agent cannot reach a state where it is missing orientation data with no recovery path. The contract is as simple as possible: `lite: true` means "assume I know the interface, just run it." Tool implementations are unaffected because the flag is stripped before dispatch.

---

## Decision

> **Implementation note:** The /mcp-lite URL overlay approach (Approach A above) was implemented and then reverted. See Approaches Considered for the full rationale. The git history on the `repeated_path_token` branch contains the overlay implementation if reference is needed.

### Per-Call `lite: true` Parameter

`lite: true` is an argument-level flag that agents pass in any `tools/call` `arguments` object on the existing `/mcp` endpoint. It is not a separate path, not a session-level toggle, and not a registered tool.

### Protocol Contract

`lite: true` is extracted from `arguments` before any dispatch. Tool implementations never see the key. The extraction is unconditional: any `tools/call` that includes `"lite": true` at the top level of `arguments` will have it stripped, whether or not the tool's own schema defines a `lite` field.

On any `tools/call` with `lite: true`, the response suppresses the following:

- Action tree enumeration in dispatcher responses (sub-tool listings with `inputSchema` and `description`)
- Parameter descriptions embedded in dispatcher sub-tool entries
- Help text and "use `action='help'`" hints from dispatcher call responses
- Full method listings and field-level hint documentation from `help` method responses

The actual operation result — the data returned by the tool implementation — is returned unchanged. The suppression applies only to the instructional scaffolding layered on top of results by the MCP protocol layer.

The `@mcp_light` lean envelope (write-path confirmation) is returned unchanged in lite mode. The `@mcp_heavy` probe envelope (`continuation_token`, `preview`, `total_size`, `available_modes`) is returned unchanged in lite mode. Both envelopes are response-content mechanisms, not orientation metadata, and suppressing them would break their respective workflows.

On failure — dispatch error, bad parameters, unknown action — the error response includes the tool's `inputSchema` from the registry. Lite mode suppresses the teaching on success; a failure re-teaches. An agent operating in bulk lite mode that makes a malformed call receives the schema it needs to correct itself in the same response that reports the error. The agent cannot get stuck without a recovery path.

`lite: true` on `/mcp` is the only behavior. There is no separate path.

### What Lite Suppresses in Practice

**Dispatcher `action=help` responses.** Strip `inputSchema` and `description` from each action entry. Return action names only.

**Dispatcher call responses.** Strip any embedded "use `action='help'`" hints from the result envelope.

**`help` method.** Return server name and `mcp_url` only. No method listing, no field-level hints.

### Backward Compatibility

`/mcp` behavior for calls without `lite: true` is unchanged. No existing agent or integration is affected. Agents that do not pass `lite: true` continue to receive the full orientation metadata on every call.

---

## Implementation Surface

All changes are in the frisian_mcp package in `views.py`. No middleware changes are needed. `LiteModeMiddleware` (from the /mcp-lite approach) is removed. The `apps.py` lite middleware install instruction is removed.

| File | Change |
|---|---|
| `frisian_mcp/views.py` | `_handle_tools_call`: extract `_lite = bool(arguments.pop('lite', False))` before dispatch; post-process response to strip scaffolding when `_lite` is True; on dispatch failure when `_lite` is True, include the tool's `inputSchema` from the registry in the error response |

**No middleware changes needed.** The `/mcp-lite` middleware and URL configuration are removed.

**Host app configuration.** None required. `lite: true` is handled entirely within `McpView` on the existing `/mcp` endpoint. Zero settings changes, zero URL changes beyond whatever the host app already has for `/mcp`.

---

## Consequences

**Positive.** Single connection point. Agents use the same `/mcp` endpoint they already connect to. No second endpoint to configure, deploy, or document. No host app URL or middleware changes required.

**Positive.** Per-call granularity. The agent decides on each individual call whether it needs scaffolding. A single session can freely mix oriented calls (scaffolding present) and lean calls (`lite: true`) without any state management.

**Positive.** Failure is safe. A `lite: true` call that fails re-includes the tool's `inputSchema` in the error response. The agent cannot reach a state where it is operating without orientation data and has no way to recover.

**Positive.** Transparent to tool implementations. The `lite` flag is stripped from `arguments` before dispatch. No tool implementation needs to handle, check, or document it.

**Positive.** Any Django project that installs frisian-mcp gets this behavior automatically on its existing `/mcp` endpoint. No per-project re-implementation required.

**Negative.** The agent must explicitly pass `lite: true` on each call where it wants suppressed scaffolding. There is no session-level toggle. An agent doing 1,000 bulk operations must pass `lite: true` on each of the 1,000 calls.

**Negative.** `lite: true` is extracted from every tool's effective `arguments` before dispatch. Tools that happen to have a top-level `lite` argument in their own schema will have that argument silently stripped before it reaches the implementation. This is unlikely given the convention of using namespaced argument names, but is a known edge case.

---

## Validation

Token savings are proportional to the number of dispatcher orientation calls eliminated. Agents doing 100 provisioning steps with one dispatcher call per step and full sub-tool schemas eliminate 99 schema payloads from the response stream. At the measured 6,000 tokens per full manifest call (30 tools × approximately 200 tokens per tool), a 100-step session saves approximately 594,000 tokens of orientation overhead — the equivalent of multiple full context windows of working capacity returned to the agent.

Verification: a `tools/call` with `lite: true` on a dispatcher returns action names without `inputSchema` or descriptions. A `tools/call` with `lite: true` that fails due to bad or unknown parameters returns the tool's `inputSchema` in the error response. A `tools/call` with `lite: true` that succeeds returns the full operation result unchanged. All of these are verifiable against a running server without integration test infrastructure.

---

*ADR maintained alongside the frisian-mcp source. Architecture decision records capture the reasoning behind durable design choices for future maintainers and adopters.*
