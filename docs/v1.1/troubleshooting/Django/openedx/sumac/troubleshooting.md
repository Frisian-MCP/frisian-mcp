# Troubleshooting: frisian-mcp with Open edX (Sumac)

**Audience:** Open edX platform engineers diagnosing problems with the MCP gateway  
**Related:** [install.md](../../../../../installs/Django/openedx/sumac/install.md)

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

**Cause B:** `FRISIAN_MCP_AUTODISCOVER` is set to `False`.

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

Open edX ships with Redis — it is already running in standard devstack and production deployments, so pointing the default cache at Redis is the expected fix.

> **Diagnosis, not yet end-to-end verified.** Per the integration-status note at the top of this page, the full OAuth flow was not exercised end to end. That `DummyCache` discards writes is well-understood Django behavior, but the specific chain above — `DummyCache` → discarded authorization code → `400` at token exchange — is inferred from the devstack cache defaults rather than a reproduced-and-fixed case. Confirm your deployment's actual `CACHES["default"]` backend before assuming this is the cause.

---

### `401 Unauthorized` on static-token clients despite valid Bearer — authentication class ordering

> **Applies to frisian-mcp ≥ 1.0.11; behavior verified on 1.1.0.** The challenge-shape behavior below depends on how discovery-first MCP clients interpret the `WWW-Authenticate` `realm` parameter, which is client- and version-specific. Confirm against your frisian-mcp and client versions rather than applying the ordering blindly; see the [install guide](../../../../../installs/Django/openedx/sumac/install.md) for the current authentication-chain configuration.

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

### Bearer token intermittently missing on `tools/call` (observed with some hosted MCP clients)

**Symptom:** MCP connection succeeds, dispatcher discovery calls work, but resource operations return `403 You do not have permission`.

**Cause (observed, not confirmed upstream):** On some hosted MCP web connectors the `Authorization` header has been seen to drop on `tools/call` requests after the initial session handshake, even though discovery calls still carry it. This has been observed intermittently with the Claude.ai web connector; it has not been reproduced deterministically or tied to a specific client version, so treat it as an environment-specific symptom rather than a settled client defect, and re-check against your client's current version.

**Workaround:** Re-save the MCP connector configuration in the client to force a fresh connection. frisian-mcp's 401 `WWW-Authenticate` response header includes a `resource_metadata` link to the OAuth discovery endpoint, so a client that re-runs discovery can re-authenticate.

---

## Tools and discovery

### `@mcp_heavy` cannot be applied to Open edX ViewSets

**Cause:** `@mcp_heavy` is a frisian-mcp decorator applied to functions you control. Open edX ViewSets are part of the platform source code and cannot be decorated without forking or patching.

**Fix:** Use `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` as a backstop instead:

```python
# lms/envs/private.py

# Auto-negotiate responses larger than 8 KB
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 8_000
```

**Scope and response shape.** This backstop is a byte-size threshold, so it applies to *any* tool response — read, write, or retrieve — whose serialized size exceeds the limit, not only large lists. When it triggers, frisian-mcp does not return the inline result: it caches the full result and returns the same negotiation **probe envelope** that `@mcp_heavy` produces, carrying `total_size`, `available_modes` (`summary`, `paginated`, `filtered`, `full`), and a `continuation_token`. The agent re-invokes the same tool with that token and a chosen mode to fetch the data; responses are not silently replaced with a bare token.

---

### Dispatch group registers 0 members

**Cause:** Basenames in `FRISIAN_MCP_DISPATCH_GROUPS` do not match the Open edX ViewSet basenames.

Open edX ViewSet basenames are set explicitly in its router registrations, not always derived from `Model._meta.object_name`. Check the LMS URL configuration for the router registration to find the correct basename.

The startup log includes "did you mean" suggestions when a group registers with 0 matching tools.

---

### URL conflict with existing Open edX `/mcp/` path

**Symptom:** The MCP endpoint returns Open edX content rather than JSON-RPC responses.

**Cause:** Open edX has another URL registered at `/mcp/` that takes priority over the frisian-mcp registration.

**Fix:** Change `FRISIAN_MCP_PATH` to a different path:

```python
FRISIAN_MCP_PATH = "api/mcp"
```

Then update your MCP client's server URL to `https://your-lms.example.com/api/mcp/`.

---

## Cross-references

See [verification tests](../../../../../installs/Django/frisian-mcp/greenfield/testing/) to confirm the installation is working correctly after setup.
