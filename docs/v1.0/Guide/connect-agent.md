# Connect an Agent

**Category:** guide  
**Slug:** connect-agent  
**Audience:** Developers connecting an AI agent to the frisian-mcp MCP server

---

## Overview

frisian-mcp exposes a Streamable HTTP MCP endpoint. Any MCP-compatible AI client — Claude Code, Claude.ai, ChatGPT, Grok, Cursor, Windsurf, and others — can connect to it. This guide covers the endpoint URL, auth options, and step-by-step config for each client.

Screenshots for each client are in `connect-agent/` (sibling of this Guide directory, under the docs root) alongside this file.

---

## Endpoint

The live MCP endpoint for the hosted demo instance is:

```text
https://mcp.frisian-mcp.com/mcp/
```

For self-hosted deployments, the endpoint path is controlled by the `FRISIAN_MCP_PATH` setting in your Django config. The default is `mcp`, giving an endpoint at `https://your-domain.example/mcp/`. The gateway is mounted to accept the path **with or without** a trailing slash (the pattern is `^mcp/?`), so `…/mcp` and `…/mcp/` both reach it directly — there is no redirect involved. Use whichever form your client produces; the examples below use the trailing-slash form for consistency. Some deployments override the path — Nautobot installations, for example, commonly mount it at `api/mcp/`.

---

## Authentication

frisian-mcp supports two authentication modes.

### Bearer token (recommended for coding agents)

Claude Code, Cursor, Windsurf, and similar coding agents use Bearer token auth. Tokens are issued by the Django admin or via the `frisian_mcp.contrib.tokens` management command.

Obtain a token from your instance admin, then include it in the `Authorization` header on every request:

```http
Authorization: Bearer <your-token>
```

### OAuth 2.0 (for Claude.ai, ChatGPT, Grok)

Claude.ai, ChatGPT, and Grok use OAuth 2.0 Authorization Code + PKCE. The `frisian_mcp.contrib.oauth` contrib app implements the discovery and authorization-code flow. Operators set `FRISIAN_MCP_OAUTH_ISSUER` to the public-facing origin.

**Client registration is closed by default.** `FRISIAN_MCP_OAUTH_REGISTRATION_OPEN` and `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER` both default to `False`, so a new client cannot self-register out of the box. The operator either pre-registers each client in the Django admin or opts into dynamic registration explicitly (see the OAuth security guidance). Once the client is registered, connecting is automatic: follow the in-product OAuth prompt, approve access in the browser, and the client receives a scoped token.

---

## Claude Code

Add frisian-mcp to your Claude Code MCP config. User-scope servers live in `~/.claude.json` (the file `~/.claude.json` — **not** a `mcp.json` inside a `~/.claude/` directory); project-scope servers live in `.mcp.json` at the project root. The `claude mcp add` CLI below writes the correct file for you.

```json
{
  "mcpServers": {
    "frisian-mcp": {
      "type": "http",
      "url": "https://mcp.frisian-mcp.com/mcp/",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

Or add it via the CLI:

```bash
claude mcp add frisian-mcp \
  --transport http \
  --header "Authorization: Bearer <your-token>" \
  https://mcp.frisian-mcp.com/mcp/
```

Verify the connection with `/mcp` in the Claude Code prompt — this lists all connected servers and their tool counts.

<!-- Screenshot: connect-agent/claude/ -->

---

## Claude.ai

In Claude.ai, add a custom connector for the endpoint URL — look under **Settings → Connectors** (labeled *Integrations* in some versions):

```text
https://mcp.frisian-mcp.com/mcp/
```

Claude.ai will initiate the OAuth flow. Approve access when prompted and the integration will appear as active in your settings.

<!-- Screenshot: connect-agent/claude/ -->

---

## ChatGPT

In ChatGPT, add a custom connector pointing at the endpoint URL. The exact location varies by ChatGPT plan and version — look under **Settings → Connectors** (or the custom/developer-connector area), and consult ChatGPT's current connector documentation if the menu differs. ChatGPT uses OAuth 2.0 — follow the authorization prompt to complete the connection.

```text
https://mcp.frisian-mcp.com/mcp/
```

<!-- Screenshot: connect-agent/chatgpt/ -->

---

## Grok

In Grok, add a custom MCP connector pointing at the endpoint URL (typically under **Settings → Connectors**, or the equivalent in your Grok client — the exact path varies by version). Grok uses OAuth 2.0 — follow the authorization prompt to complete the connection.

```text
https://mcp.frisian-mcp.com/mcp/
```

<!-- Screenshot: connect-agent/grok/ -->

---

## Example tool call

Once connected, the agent discovers available tools via `tools/list`. When the dispatcher pattern is configured (`FRISIAN_MCP_DISPATCH_GROUPS`, or an explicit `@mcp_dispatcher`), the initial tool list stays small regardless of how many ViewSet actions the server exposes. With auto-discovery alone, each ViewSet action is a separate flat tool — see the [dispatcher pattern guide](dispatcher-pattern.md).

A `tools/list` response from the demo instance looks like:

```json
{
  "tools": [
    {
      "name": "dcim",
      "description": "Dispatch DCIM operations: devices, interfaces, racks, sites...",
      "inputSchema": { "..." : "..." }
    },
    {
      "name": "ipam",
      "description": "Dispatch IPAM operations: prefixes, IP addresses, VRFs...",
      "inputSchema": { "..." : "..." }
    }
  ]
}
```

Calling a tool routes to the underlying ViewSet action. For example, listing devices via the `dcim` dispatcher:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "dcim",
    "arguments": {
      "resource": "device",
      "action": "list",
      "params": { "site": "nyc-01", "limit": 10 }
    }
  }
}
```

The server routes this to the DRF ViewSet and returns the result.

---

## Troubleshooting

**401 Unauthorized** — The token is missing, expired, or not recognized (an authentication failure). Verify the `Authorization` header is present and the token value is correct. Tokens can be inspected in the Django admin under **Frisian MCP → Tokens**.

**403 Forbidden** — Returned at the **gateway** level when the endpoint's DRF permission classes reject the request (for example an authenticated caller that fails a `FRISIAN_MCP_PERMISSION_CLASSES` check). This is distinct from a *tool-call* denial: a tool the caller's tier cannot see is **absent** from `tools/list` (see the empty-array case below), and a permission failure when invoking a visible tool comes back as a JSON-RPC error result over HTTP `200` (`isError`), not a `403`. So a `403` points at gateway/endpoint permissions, not at an individual tool.

**`tools/list` returns an empty array** — The server is reachable but the caller sees no tools. Work through the common causes with `python manage.py mcp_doctor`:

- **No ViewSets were discovered.** Confirm `frisian_mcp` and your API app are in `INSTALLED_APPS`, `FRISIAN_MCP_AUTODISCOVER = True`, and your DRF router has ViewSets registered. Discovery is deferred to the first request rather than run at process start, so a restart is not required — but the router must be populated before that first request.
- **The caller's tier hides everything.** With permission-aware discovery (or a per-route tier ceiling), a credential that maps to a tier below every available tool sees an empty list rather than a 403 — a read-tier token on a surface that only exposes write tools returns `[]`.
- **The route exposes nothing.** On a per-route deployment (`FRISIAN_MCP_ROUTES`), a route whose `allow_list` is empty serves zero tools by design; check the route's allow/deny lists and the startup audit.

**Connection refused / timeout** — Check that your firewall allows outbound HTTPS to `mcp.frisian-mcp.com` on port 443. For self-hosted instances, verify the Django process is running and the reverse proxy is forwarding requests to the correct port.

**OAuth redirect loop** — Confirm `FRISIAN_MCP_OAUTH_ISSUER` in Django settings matches the public-facing domain of your deployment. A mismatch between the issuer URL and the actual domain causes authorization server metadata discovery to fail.

---

*Document maintained alongside the frisian-mcp source.*
