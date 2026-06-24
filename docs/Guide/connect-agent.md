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
https://mcp.frisian-mcp.com/mcp
```

For self-hosted deployments, the endpoint path is controlled by the `FRISIAN_MCP_PATH` setting in your Django config. The default for Nautobot installations is `api/mcp`, giving an endpoint of `https://your-domain.example/api/mcp`.

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

Claude.ai, ChatGPT, and Grok use OAuth 2.0 Authorization Code + PKCE. The `frisian_mcp.contrib.oauth` contrib app implements the full OAuth discovery and authorization flow. Operators set `FRISIAN_MCP_OAUTH_ISSUER` in their Django config and the rest works automatically.

When connecting via any of these clients, follow the in-product OAuth prompt. Your browser will redirect to the Django authorization endpoint, you'll approve access, and the client receives a scoped token automatically.

---

## Claude Code

Add frisian-mcp to your Claude Code MCP config. The config file is at `~/.claude/mcp.json` (global) or `.claude/mcp.json` in a project directory.

```json
{
  "mcpServers": {
    "frisian-mcp": {
      "type": "http",
      "url": "https://mcp.frisian-mcp.com/mcp",
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
  https://mcp.frisian-mcp.com/mcp
```

Verify the connection with `/mcp` in the Claude Code prompt — this lists all connected servers and their tool counts.

<!-- Screenshot: connect-agent/claude/ -->

---

## Claude.ai

In Claude.ai, go to **Settings → Integrations → Add MCP server** and enter the endpoint URL:

```text
https://mcp.frisian-mcp.com/mcp
```

Claude.ai will initiate the OAuth flow. Approve access when prompted and the integration will appear as active in your settings.

<!-- Screenshot: connect-agent/claude/ -->

---

## ChatGPT

In ChatGPT, go to **Settings → Connectors → Add connector** and enter the endpoint URL. ChatGPT uses OAuth 2.0 — follow the authorization prompt to complete the connection.

```text
https://mcp.frisian-mcp.com/mcp
```

<!-- Screenshot: connect-agent/chatgpt/ -->

---

## Grok

In Grok, navigate to **Tools → Add MCP server** and enter the endpoint URL. Grok uses OAuth 2.0 — follow the authorization prompt to complete the connection.

```text
https://mcp.frisian-mcp.com/mcp
```

<!-- Screenshot: connect-agent/grok/ -->

---

## Example tool call

Once connected, the agent discovers available tools via `tools/list`. frisian-mcp uses the dispatcher pattern — the initial tool list stays small regardless of how many ViewSet actions the server exposes.

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
      "action": "devices_list",
      "params": { "site": "nyc-01", "limit": 10 }
    }
  }
}
```

The server routes this to the DRF ViewSet and returns the result.

---

## Troubleshooting

**401 Unauthorized** — The token is missing, expired, or does not have sufficient permissions. Verify the `Authorization` header is present and the token value is correct. Tokens can be inspected in the Django admin under **Frisian MCP → Tokens**.

**`tools/list` returns an empty array** — The server is reachable but no dispatch groups are registered. Confirm that `frisian_mcp` is in `INSTALLED_APPS` and that the application has been restarted after installation.

**Connection refused / timeout** — Check that your firewall allows outbound HTTPS to `mcp.frisian-mcp.com` on port 443. For self-hosted instances, verify the Django process is running and the reverse proxy is forwarding requests to the correct port.

**OAuth redirect loop** — Confirm `FRISIAN_MCP_OAUTH_ISSUER` in Django settings matches the public-facing domain of your deployment. A mismatch between the issuer URL and the actual domain causes authorization server metadata discovery to fail.

---

*Document maintained alongside the frisian-mcp source.*
