# Troubleshooting: frisian-mcp with Paperless-ngx 2.x

**Audience:** Paperless-ngx administrators diagnosing problems with the MCP gateway  
**Related:** [install.md](../../../../installs/Django/paperless/2.x/install.md)

---

## Installation

### `registered 0 tools` at startup

**Cause A:** `PAPERLESS_APPS` environment variable is not set or is not being read. Verify it is set in `paperless.conf` or in the Docker environment before the Paperless-ngx process starts:

```bash
PAPERLESS_APPS=frisian_mcp
```

**Cause B:** `frisian_MCP_AUTODISCOVER` was set to `False` in your settings override.

**Cause C:** The first request has not been sent yet. frisian-mcp runs auto-discovery on startup in `AppConfig.ready()` — but confirm by sending a `ping`:

```bash
curl -s -X POST https://your-paperless.example.com/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
```

A valid JSON-RPC `{}` result confirms the endpoint is live. If `tools/list` then returns an empty array, the issue is in auto-discovery, not the endpoint.

---

### `FRISIAN_MCP_*` settings ignored when set in `paperless.conf`

**Cause:** `paperless.conf` sets environment variables that Paperless-ngx reads into its own settings. Standard `FRISIAN_MCP_*` prefixed settings are not part of Paperless-ngx's known settings list and are not propagated to `django.conf.settings` automatically.

**Fix:** Use a settings override file that imports Paperless-ngx's base settings and adds frisian-mcp settings on top:

```python
# paperless_frisian_mcp.py

from paperless.settings import *  # noqa: F401, F403

INSTALLED_APPS.append("frisian_mcp")  # noqa: F405
FRISIAN_MCP_PATH = "mcp"
FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"
```

Then point Paperless-ngx at this file:

```bash
DJANGO_SETTINGS_MODULE=paperless_frisian_mcp
```

---

## Authentication

### `403 You do not have permission` on resource calls

**Cause A:** The Bearer token is a Paperless-ngx user token but no DRF authentication class recognises it for the MCP surface. Paperless-ngx's `TokenAuthentication` must be in `FRISIAN_MCP_AUTHENTICATION_CLASSES`.

**Fix:**

```python
# paperless_frisian_mcp.py

FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "rest_framework.authentication.TokenAuthentication",
]
```

**Cause B:** The token exists but the user account does not have permission on the requested resource. Verify the user has the correct Paperless-ngx object-level permissions.

**Cause C:** The Anthropic token-forwarding bug (see below).

---

### Anthropic MCP client drops Bearer token — all resource calls return `403`

**Symptom:** MCP connection succeeds (`initialize` handshake works), dispatcher `action="help"` calls succeed, but any actual resource operation (`list`, `create`, `retrieve`) returns `403 You do not have permission`. The issue is intermittent — it appears at the start of some sessions and not others.

**Cause:** This is a confirmed intermittent bug in Anthropic's MCP client. The `Authorization: Bearer <token>` header is not forwarded on `tools/call` requests after the initial session establishment. The Paperless-ngx server correctly rejects unauthenticated requests with 403. The MCP layer itself is not the source of the error.

**Evidence from live testing:** The connector path changed between sessions (`link_69ff151e` → `link_69ff3a25`), consistent with the client re-establishing connections and losing token state. Re-issuing the token and updating the connector configuration resolved the issue in most cases.

**Workaround:** Re-save the MCP connector configuration in the AI client (Claude.ai: Settings → Integrations → your connector → Save). This forces a fresh connection that correctly attaches the token.

frisian-mcp's `WWW-Authenticate` response header includes the `resource_metadata` parameter pointing to the OAuth discovery endpoint. When correctly formed, this allows the client to re-discover and re-authenticate automatically rather than failing silently.

**This is an Anthropic platform issue. It does not affect ChatGPT or Claude Code connections.**

---

### OAuth authorization code not found

**Cause:** The cache backend is not configured or is using `DummyCache`. frisian-mcp's OAuth PKCE flow stores authorization codes in Django's default cache. If the cache discards entries immediately, the token exchange step always fails.

**Fix:** Configure a real cache backend. Redis is recommended and is often already deployed alongside Paperless-ngx:

```python
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://localhost:6379/1",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    },
}
```

---

## Tool calls

### `Invalid arguments: 1 is not of type 'string'` on classification create

**Symptom:** Creating a tag, correspondent, or document type with `matching_algorithm` set to an integer returns a schema validation error.

**Cause:** The `matching_algorithm` field on Paperless-ngx classification resources requires a string value even though it represents a numeric enum. This is a type enforcement issue in the Paperless-ngx serializer as exposed through frisian-mcp's schema.

**Fix:** Pass integer-like enum values as strings:

```json
{
  "action": "create",
  "params": {
    "name": "Finance",
    "matching_algorithm": "1",
    "match": "finance invoice statement"
  }
}
```

Not:

```json
{
  "matching_algorithm": 1
}
```

Affected fields: `matching_algorithm` on tags, correspondents, document types, and storage paths.

---

### Agent cannot upload documents

**Symptom:** Agent requests a "create document" or "upload document" action and finds no such action in the dispatcher surface.

**Cause:** Paperless-ngx's document upload is a separate file upload endpoint, not a standard DRF create action. The frisian-mcp dispatcher surface exposes the ViewSet layer; file upload is outside the ViewSet surface.

**This is by design.** MCP agents can read, annotate, classify, tag, and manage existing documents. Uploading new documents requires a separate call to the Paperless-ngx API outside the MCP surface.

---

## Dispatcher configuration

### Dispatch group registers 0 members

**Cause:** Basenames in `FRISIAN_MCP_DISPATCH_GROUPS` do not match the registered ViewSet basenames. Basenames are always `Model._meta.object_name.lower()`:

| URL path | Correct basename |
|----------|-----------------|
| `/api/document_types/` | `documenttype` |
| `/api/storage_paths/` | `storagepath` |
| `/api/share_links/` | `sharelink` |
| `/api/mail_rules/` | `mailrule` |
| `/api/workflow_triggers/` | `workflowtrigger` |

The startup log includes "did you mean" suggestions from the registered basename list.

---

### ngrok URL becomes invalid after server restart

**Symptom:** MCP client cannot connect — the server URL is unreachable.

**Cause:** Free-tier ngrok tunnels reset with a new URL on every process restart. Any MCP connector configured with the old ngrok URL will fail.

**Fix:** Update the MCP connector URL in your AI client to the new ngrok URL. For persistent demo environments, use a stable URL (paid ngrok tunnel, Cloudflare Tunnel, or direct exposure) rather than a free-tier ngrok tunnel.

---

## Cross-references

See `installs/Django/frisian-mcp/testing/` for verification tests to confirm the installation is working correctly after setup.
