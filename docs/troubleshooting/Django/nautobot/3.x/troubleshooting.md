# Troubleshooting: frisian-mcp with Nautobot 3.x

**Audience:** Nautobot administrators diagnosing problems with the MCP gateway  
**Related:** [install.md](../../../../installs/Django/nautobot/3.x/install.md) · [nginx.md](../../../../installs/Django/nautobot/3.x/nginx.md)

---

## Installation

### `[frisian-mcp] registered 0 tools` at startup

frisian-mcp defers URL-tree scanning to the first incoming HTTP request, not to startup. If no request has been sent yet, the tool count will be zero and the startup line will not appear.

**Steps:**

1. Send any request to `/api/mcp/` — even a `ping` — to trigger discovery.
2. Check the server output for `[frisian-mcp] registered N tools at /api/mcp/`.
3. If still 0, verify `FRISIAN_MCP_AUTODISCOVER` is not set to `False` in `nautobot_config.py`.
4. Verify `frisian_mcp` appears in `INSTALLED_APPS`.

If your Nautobot uses plugin apps, confirm those plugins have completed their own `AppConfig.ready()` before the first request is processed. frisian-mcp's deferred discovery is specifically designed to catch late-loading plugin ViewSets — the first-request trigger ensures all plugins are registered before the URL-tree scan runs.

---

### `urls.W002` warning in system checks

This warning — *"Your URL pattern has a route beginning with a '/'..."* — was caused by a leading slash in an earlier version of the package.

It is resolved in the current version. If you see it, reinstall from the repository to pick up the fix:

```bash
pip install --force-reinstall frisian-mcp
```

---

### Dispatch group registers 0 members

The startup log shows a warning such as:

```
[frisian-mcp] WARNING: dispatch group 'dns' has 0 matching tools
```

**Cause:** The basenames listed in `FRISIAN_MCP_DISPATCH_GROUPS` do not match any registered ViewSet.

Basenames are always derived from `Model._meta.object_name.lower()` — the lowercase Django model class name. They are **not** URL slugs.

| URL slug | Correct basename |
|---|---|
| `ip-addresses` | `ipaddress` |
| `dns-views` | `dnsview` |
| `a-records` | `arecord` |
| `rack-groups` | `rackgroup` |

The warning log includes a "did you mean" hint with similar registered names. Use those suggestions to correct the basename list.

To list every registered basename at runtime, connect an MCP client and call any group dispatcher with `action="help"`. The response includes all resource names within that group.

---

## Authentication

### Requests rejected with 401

**Check 1 — Token exists and is active:**  
In the Nautobot admin under **Admin → API Tokens**, confirm the token is present, not expired, and associated with the correct user.

**Check 2 — Authentication class order:**  
When using static tokens alongside OAuth, **list `FrisianMcpTokenAuthentication` and / or `FrisianMcpApiKeyAuthentication` BEFORE `OAuthTokenAuthentication`** — the first authenticator emits the 401 WWW-Authenticate challenge, and tokens-first emits a bare `Bearer` that static-token MCP clients (Claude Code, Codex, Gemini CLI) fall back to cleanly:

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

If you need Nautobot-native API tokens to authenticate the MCP endpoint as well, add `nautobot.core.api.authentication.TokenAuthentication` to the chain — but be aware that NTC `TokenAuthentication` *also* eats `Bearer` headers and raises on unknown values, which can conflict with the frisian-mcp classes. The cleanest production posture is frisian-mcp's own token model + OAuth, not NTC tokens.

**Check 3 — Intentional behavior:**  
If `FRISIAN_MCP_PERMISSION_CLASSES = ["rest_framework.permissions.IsAuthenticated"]` is set, unauthenticated requests receive 401 by design. This is the correct production configuration.

---

### Nautobot superuser token limited to read-only tools

**Symptom:** An agent authenticating with a Nautobot superuser API token can call `list` and `retrieve` tools but receives `"caller has 'read'"` when attempting write operations, even though the Nautobot user is a superuser.

**Cause:** Nautobot API tokens do not carry a `.permission` attribute. Without additional configuration, frisian-mcp cannot determine the token's privilege level and safely defaults to the lowest tier (`read`).

**Fix:** Add `FRISIAN_MCP_TOKEN_TIER_MAP` to `nautobot_config.py` to map Nautobot user roles to MCP tiers:

```python
FRISIAN_MCP_TOKEN_TIER_MAP = {
    "superuser": "read_write",
    "staff": "read_write",
    "default": "read",
}
```

This was encountered during integration testing with a superuser token. Without this map, the superuser token behaved identically to an anonymous read-only caller.

---

### Claude.ai completes OAuth but tool calls arrive unauthenticated

**Symptom:** Claude.ai successfully completes the OAuth PKCE flow and shows the MCP server as connected. Tool calls then fail or return read-only results. The same OAuth credentials work correctly in ChatGPT or Claude Code.

**Cause:** Claude.ai's web MCP connector has a known bug: it obtains a Bearer token during the OAuth flow but does **not** include the `Authorization: Bearer <token>` header in subsequent MCP POST requests (tools/list, tools/call). Requests arrive at the server as unauthenticated, and frisian-mcp falls back to `FRISIAN_MCP_UNAUTHENTICATED_TIER`.

**This is a Claude.ai client-side bug.** ChatGPT and Claude Code (CLI) correctly send the Bearer header on every call and are not affected.

**Workaround for development environments only:**

```python
# nautobot_config.py — DEV ONLY, never in production
FRISIAN_MCP_UNAUTHENTICATED_TIER = "read_write"
```

This gives unauthenticated callers the same access as authenticated read-write users. Do not use this in production.

**For production:** Claude.ai users will be limited to whatever `FRISIAN_MCP_UNAUTHENTICATED_TIER` is set to until Anthropic resolves the client bug. Set `FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"` for read-only access without authentication.

---

## Nginx and Proxy

### 504 Gateway Timeout on tool calls

MCP tool calls are synchronous HTTP requests that can take 30–90 seconds for complex Nautobot queries (large device lists, relationship resolution, job execution).

**Steps:**

1. Increase `proxy_read_timeout` in the `/api/mcp/` location blocks:
   ```nginx
   proxy_read_timeout 180s;  # try 180, then 300 if still timing out
   ```
2. Add `FRISIAN_MCP_DISPATCH_GROUPS` to bundle tools — group dispatchers reduce per-call data volume and response time (see [install.md](../../../../installs/Django/nautobot/3.x/install.md) Step 7).
3. For ViewSet actions returning large result sets, consider `@mcp_heavy` — it enforces pagination-first behavior so agents receive a count and first page rather than the full result set.

---

### 301 redirect instead of 200 on `/api/mcp` (no trailing slash)

**Cause:** MCP clients do not follow HTTP redirects. When pointed at `/api/mcp` (no trailing slash), Nautobot's APPEND_SLASH middleware issues a 301 to `/api/mcp/`. The MCP client treats the 301 as a failure.

**Fix:** Ensure the exact-match location block is present in your Nginx config and appears before the prefix block:

```nginx
location = /api/mcp {
    proxy_pass http://nautobot:8080;
    # ... headers ...
}

location /api/mcp/ {
    proxy_pass http://nautobot:8080;
    # ... headers ...
}
```

frisian-mcp also installs `McpTrailingSlashMiddleware` automatically to handle trailing-slash normalization internally, but the Nginx exact-match block prevents the redirect from reaching Django at all — which is more reliable.

---

### OAuth discovery returns 404

**Check 1 — Well-known location block present:**

```nginx
location /.well-known/ {
    proxy_pass http://nautobot:8080;
    # ... headers ...
}
```

**Check 2 — contrib.oauth installed:**

```python
INSTALLED_APPS.append("frisian_mcp.contrib.oauth")
```

**Check 3 — FRISIAN_MCP_OAUTH_ISSUER set:**

```python
FRISIAN_MCP_OAUTH_ISSUER = "https://your-nautobot.example.com"
```

All three are required. The well-known endpoint is auto-registered by `contrib.oauth` — if the app is not in `INSTALLED_APPS`, the URL does not exist regardless of the Nginx config.

---

### HTTPS shows as HTTP inside Django (SECURE_PROXY_SSL_HEADER not working)

**Symptom:** OAuth token endpoint URLs in the well-known document use `http://` instead of `https://`. Session cookies are not marked Secure. Django's CSRF checks fail on HTTPS requests.

**Check 1 — Setting is present and correct:**

```python
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
```

**Check 2 — Nginx is forwarding the header:**

```nginx
proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
```

If TLS terminates at a CDN or load balancer upstream of Nginx (Cloudflare, ALB), the `X-Forwarded-Proto` header may arrive already set by that layer. In that case, Nginx should forward it as-is. If Nginx is the TLS termination point, use `$scheme` instead:

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
```

**Check 3 — FRISIAN_MCP_TRUSTED_PROXY_COUNT:**

```python
FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1  # increment if a CDN sits in front of Nginx
```

---

## Performance and Stability

### DB connection exhaustion under MCP polling

**Symptom:** Nautobot workers exhaust the PostgreSQL `max_connections` limit when an MCP client polls repeatedly (tools/list, multiple sequential tool calls). The server logs show connection timeout errors or `OperationalError: FATAL: sorry, too many clients already`.

**Cause:** Nautobot's default `CONN_MAX_AGE = 300` (5 minutes) keeps one persistent DB connection per thread. With `multithread=True` in gunicorn and an MCP client issuing continuous requests, connections accumulate faster than they expire.

**Fix for development:**

```python
# nautobot_config.py
for _alias in DATABASES:
    DATABASES[_alias]["CONN_MAX_AGE"] = 0
```

This disables persistent connections so each request opens and closes cleanly. The performance cost at development request rates is negligible.

**For production:** Use a connection pooler (PgBouncer) in transaction mode, or set `CONN_MAX_AGE` to a lower value (30–60 seconds) with a matching PostgreSQL `max_connections` headroom for the number of gunicorn workers × threads.

---

### OAuth auth codes fail intermittently in multi-worker deployments

**Symptom:** Some OAuth PKCE flows complete successfully while others fail at the token exchange step with `invalid_grant`. The failure rate correlates with the number of gunicorn workers.

**Cause:** Django's default `LocMemCache` stores data in-process. With multiple gunicorn workers, an auth code created during the `/oauth/authorize` request (handled by worker A) is invisible to worker B when the client POSTs to `/oauth/token`. The token exchange fails because the code lookup returns nothing.

**Fix:** Configure a shared cache backend. Redis is the standard choice for Nautobot deployments:

```python
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}
```

frisian-mcp logs a startup warning when `LocMemCache` is detected in a non-DEBUG environment as a reminder to configure a shared backend before enabling OAuth in production.

---

*Document written: 2026-05-21*
