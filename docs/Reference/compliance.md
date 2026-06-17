# Compliance & Standards

frisian-mcp is built on published, widely-adopted specifications. The tables below list every protocol, RFC, and standard implemented by the system. Security-sensitive flows (Bearer token transmission, PKCE, the full OAuth 2.0 authorization suite) follow the relevant RFCs exactly — no shortcuts, no proprietary extensions. This is deliberate: AI agents operate across a wide range of infrastructure and clients, and strict standards compliance is the foundation that makes interoperability reliable.

---

## Model Context Protocol

| Spec | Title | Where Used |
|------|-------|------------|
| [MCP Specification](https://spec.modelcontextprotocol.io) | Model Context Protocol — Anthropic | Core protocol implemented by `McpView`. Defines tool discovery (`tools/list`), tool invocation (`tools/call`), and the `initialize` handshake. |
| [JSON-RPC 2.0](https://www.jsonrpc.org/specification) | JSON-RPC 2.0 Specification | Transport format for all MCP traffic. Every MCP request and response is a JSON-RPC 2.0 message (`jsonrpc`, `id`, `method`, `result`, `error`). |
| [SSE (Server-Sent Events)](https://html.spec.whatwg.org/multipage/server-sent-events.html) | W3C/WHATWG Living Standard | Optional streaming transport. When the client sends `Accept: text/event-stream`, `McpView` wraps JSON-RPC responses in an SSE stream with `Content-Type: text/event-stream`. |

---

## OAuth 2.0

| RFC | Title | Where Used |
|-----|-------|------------|
| [RFC 6749](https://www.rfc-editor.org/rfc/rfc6749) | The OAuth 2.0 Authorization Framework | Foundation for all OAuth flows. §4.4 client credentials grant, §4.1 authorization code grant, §3.1.2 redirect URI validation, §3.3 scope strings. |
| [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750) | The OAuth 2.0 Authorization Framework: Bearer Token Usage | §2.1 defines how Bearer tokens are transmitted in the `Authorization` header. Implemented in `OAuthTokenAuthentication` and `FrisianMcpApiKeyAuthentication`. |
| [RFC 7235](https://www.rfc-editor.org/rfc/rfc7235) | Hypertext Transfer Protocol (HTTP/1.1): Authentication | §2.1 specifies that authentication scheme names (e.g. `Bearer`) are case-insensitive. |
| [RFC 7591](https://www.rfc-editor.org/rfc/rfc7591) | OAuth 2.0 Dynamic Client Registration Protocol | Implemented by `RegistrationView` (`/oauth/register/`). §2 defines the `grant_types` field enforced on `OAuthClient`. Disabled by default; enabled via `FRISIAN_MCP_OAUTH_DCR`. |
| [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636) | Proof Key for Code Exchange by OAuth Public Clients (PKCE) | PKCE S256 challenge/verifier. Used by native and public clients. Enabled via `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER`. |
| [RFC 8252](https://www.rfc-editor.org/rfc/rfc8252) | OAuth 2.0 for Native Apps | §7.1 custom URI scheme convention and §7.3 loopback redirect (`127.0.0.1`) used in redirect URI validation. |
| [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) | OAuth 2.0 Authorization Server Metadata | Implemented by `WellKnownView` at `/.well-known/oauth-authorization-server`. Advertises `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `scopes_supported`, and related fields. |
| [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) | Resource Indicators for OAuth 2.0 | Used for per-resource metadata URL construction (appending resource path to `.well-known/` base). |
| [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) | OAuth 2.0 Protected Resource Metadata | `/.well-known/oauth-protected-resource` endpoint. Advertised in `WWW-Authenticate` 401 responses via `resource_metadata=` link. |

---

## General Web Standards

| RFC | Title | Where Used |
|-----|-------|------------|
| [RFC 4122](https://www.rfc-editor.org/rfc/rfc4122) | A Universally Unique IDentifier (UUID) URN Namespace | UUID format pattern used in `invocation.py` to distinguish UUID-style tool arguments from plain strings. |
| [RFC 6570](https://www.rfc-editor.org/rfc/rfc6570) | URI Template | Level-1 URI template matching used in `resources.py` for static resource registry lookup. |

---

*Document maintained alongside the frisian-mcp source.*
