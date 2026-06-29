# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

- **Issue #12 — custom detail actions:** Auto-discovered DRF `@action(detail=True)` handlers
  now require an `id` in their MCP schema and pass it through as `pk` during invocation,
  preventing missing-`pk` tracebacks for integrations such as Nautobot `napalm`.

### Removed

- **`McpEndpointView` alias dropped.** The `McpEndpointView` name (a backward-compatible alias
  for `McpView` introduced during the rename) has been removed before the 1.0 release. Update
  any imports: `from frisian_mcp.views import McpView`.

### Security

- **AUTH-4 — Token hashing:** `FrisianMcpToken.token` and `OAuthClient.client_secret` are
  now stored as HMAC-SHA256 keyed by `SECRET_KEY` instead of plaintext.  The raw value is
  shown exactly once at creation time via the `_raw_token` / `_raw_client_secret` instance
  attribute (available immediately after `save()`, absent after a DB reload).

  **Migration note:** Existing tokens and OAuth clients created before this release are
  automatically invalidated — the stored plaintext no longer matches any HMAC lookup.
  Regenerate all `FrisianMcpToken` and `OAuthClient` records after deploying this version.
  No data migration is provided because the original plaintext values cannot be recovered
  from the database.

  Rotating `SECRET_KEY` also invalidates all tokens; this is intentional and desirable.

  `OAuthAccessToken.token` follows the same pattern — stored as HMAC-SHA256 of the raw
  Bearer value, never plaintext.  The raw token is exposed exactly once via
  ``plaintext_token`` on the freshly-saved instance (see ``TokenView``).  Existing access
  tokens are also invalidated on upgrade and will need to be re-issued.
