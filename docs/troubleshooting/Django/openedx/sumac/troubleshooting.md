# Troubleshooting: frisian-mcp with Open edX (Sumac)

**Audience:** Open edX platform engineers diagnosing problems with the MCP gateway  
**Related:** [install.md](../../../../installs/Django/openedx/sumac/install.md)

> **Integration status:** The frisian-mcp Open edX plugin was validated through startup and migrations. Full end-to-end OAuth and `tools/call` testing was not completed at the time this document was written. Issues discovered during full validation should be added here.

---

## Installation

### MCP endpoint not reachable — `404` at `/mcp/`

**Cause:** The `openedx_frisian_mcp` plugin app is not in `INSTALLED_APPS`, or the `PluginURLs` registration did not fire.

Open edX uses `edx_django_utils.plugins` for URL injection rather than the standard Django URL routing. `frisian_mcp.AppConfig.ready()` registers the MCP URL automatically in standard Django projects — but this mechanism is overridden by Open edX's plugin URL injection layer. The `openedx_frisian_mcp` app's `AppConfig` must be present for URL registration to happen.

**Fix:**

1. Verify `openedx_frisian_mcp` is in `INSTALLED_APPS` and appears **after** `frisian_mcp`:

```python
INSTALLED_APPS = list(INSTALLED_APPS) + [
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
    "openedx_frisian_mcp",
]
```

1. Check that the `PluginURLs.CONFIG` in `openedx_frisian_mcp/apps.py` is present and correct.

2. Restart the LMS and look for this startup log line:

```text
frisian_mcp: auto-discovery registered N tools
```

If the line does not appear, `AppConfig.ready()` did not execute.

---

### `registered 0 tools` at startup

**Cause A:** `openedx_frisian_mcp` is not in `INSTALLED_APPS` after `frisian_mcp`.

**Cause B:** `frisian_MCP_AUTODISCOVER` is set to `False`.

**Cause C:** The LMS `INSTALLED_APPS` is being built from a tuple (from `get_plugin_apps()`). Using `INSTALLED_APPS += [...]` on a tuple raises a `TypeError`. Use `list(INSTALLED_APPS) + [...]` instead:

```python
# Wrong — may fail if INSTALLED_APPS is a tuple
INSTALLED_APPS += ["frisian_mcp", "openedx_frisian_mcp"]

# Correct
INSTALLED_APPS = list(INSTALLED_APPS) + ["frisian_mcp", "openedx_frisian_mcp"]
```

---

### Django admin login redirects to React page

**Symptom:** Navigating to `/admin/login/` redirects to the Open edX React login UI instead of Django's HTML form. The frisian-mcp OAuth admin interface requires the standard Django admin login.

**Cause:** Open edX hard-wires `admin/login/` to redirect to its React login page.

**Fix (development only):** Use `mcp_dev_urls.py` from the `openedx_frisian_mcp` plugin as your `ROOT_URLCONF`. This overrides the admin login URL to serve Django's plain HTML form. Do not use this in production.

---

## Authentication

### OAuth authorization code not found — `400` during token exchange

**Cause:** The default Open edX devstack settings configure `DummyCache` for all cache backends. frisian-mcp's OAuth PKCE flow stores authorization codes in the default cache (`django.core.cache.cache`). With `DummyCache`, the code is discarded immediately after being written — the token exchange step always fails with "authorization code not found."

**Fix:** Configure a real cache backend (Redis) for the default cache:

```python
# lms/envs/private.py

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://localhost:6379/1",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    },
}
```

Open edX ships with Redis — it is already running in standard devstack and production deployments. Pointing the default cache at Redis resolves the issue.

---

### `401 Unauthorized` on static-token clients despite valid Bearer — authentication class ordering

**Cause:** OAuth-first chain ordering. When `OAuthTokenAuthentication` is listed before `FrisianMcpTokenAuthentication`, the 401 WWW-Authenticate challenge emitted on unauthenticated requests is `Bearer realm="...", resource_metadata="..."`. Discovery-first MCP clients (Claude Code, Codex, Gemini CLI) interpret the `realm` parameter as a directive to probe `.well-known/` and run the OAuth discovery cascade — which dead-ends when DCR is closed, even though the operator's `mcp.json` carries a valid static Bearer.

**Fix:** `FrisianMcpTokenAuthentication` must come **before** `OAuthTokenAuthentication`. Tokens-first emits a bare `Bearer` challenge that static-token clients accept, falling back cleanly to the Bearer in their `mcp.json`.

```python
# settings.py — correct ordering

FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

`FrisianMcpApiKeyAuthentication` (settings-backed static keys from `FRISIAN_MCP_API_KEYS`) is listed first because it is a cheap dict lookup that returns `None` for non-matching tokens without touching the DB.

> **Historical note.** Pre-1.0.11 packages required the opposite ordering for chain correctness (token classes used to raise on lookup-miss). The 1.0.11 chain fix made both classes return `None` on miss, so OAuth tokens now reach the OAuth authenticator regardless of order. Tokens-first is the new convention because the chain-order issue that remains is WWW-Authenticate shape, not authentication correctness.

---

### Anthropic MCP client drops Bearer token intermittently

**Symptom:** MCP connection succeeds, dispatcher discovery calls work, but resource operations return `403 You do not have permission`.

**Cause:** Intermittent bug in Anthropic's MCP client — the `Authorization` header is not forwarded on `tools/call` requests after initial session establishment. This is an Anthropic platform issue.

**Workaround:** Re-save the MCP connector configuration in the AI client to force a fresh connection. frisian-mcp's `WWW-Authenticate` response header includes `resource_metadata` pointing to the OAuth discovery endpoint, enabling the client to re-authenticate automatically.

---

## Tools and discovery

### `@mcp_heavy` cannot be applied to Open edX ViewSets

**Cause:** `@mcp_heavy` is a frisian-mcp decorator applied to functions you control. Open edX ViewSets are part of the platform source code and cannot be decorated without forking or patching.

**Fix:** Use `frisian_MCP_AUTO_NEGOTIATE_THRESHOLD` as a backstop instead:

```python
# lms/envs/private.py

# Auto-negotiate responses larger than 8 KB
frisian_MCP_AUTO_NEGOTIATE_THRESHOLD = 8_000
```

This applies to all tool responses. Large list responses are automatically cached and returned as a continuation token rather than inline JSON.

---

### Dispatch group registers 0 members

**Cause:** Basenames in `FRISIAN_MCP_DISPATCH_GROUPS` do not match the Open edX ViewSet basenames.

Open edX ViewSet basenames are set explicitly in its router registrations, not always derived from `Model._meta.object_name`. Check the LMS URL configuration for the router registration to find the correct basename.

The startup log includes "did you mean" suggestions when a group registers with 0 matching tools.

---

### URL conflict with existing Open edX `/mcp/` path

**Symptom:** The MCP endpoint returns Open edX content rather than JSON-RPC responses.

**Cause:** Open edX has another URL registered at `/mcp/` that takes priority over the frisian-mcp registration.

**Fix:** Change `frisian_MCP_PATH` to a different path:

```python
frisian_MCP_PATH = "api/mcp"
```

Then update your MCP client's server URL to `https://your-lms.example.com/api/mcp/`.

---

## Cross-references

See `installs/Django/frisian-mcp/testing/` for verification tests to confirm the installation is working correctly after setup.
