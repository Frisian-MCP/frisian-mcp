# Nautobot + frisian-mcp: Nginx Configuration

**Audience:** Nautobot administrators running Nautobot behind an Nginx reverse proxy  
**Prerequisite:** Complete [install.md](install.md) before configuring Nginx

---

## Overview

When Nautobot runs behind Nginx, the MCP endpoint requires specific proxy configuration that differs from a standard web application setup. MCP clients:

- Send `POST` requests to the exact endpoint URL (they do not follow redirects)
- May hold connections open for long-running tool calls (120+ seconds is normal)
- Rely on `/.well-known/` discovery to locate OAuth endpoints automatically

This guide covers the Nginx configuration for these requirements and the corresponding Django settings that enable secure proxy-aware operation.

---

## Nginx Server Block

The following configuration proxies all Nautobot traffic and adds the MCP-specific location blocks. Adjust `server_name`, the upstream address, and timeout values to match your environment.

```nginx
server {
    listen 80;
    server_name your-nautobot.example.com;

    client_max_body_size 25M;

    # Recommended security headers
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;

    # MCP endpoint — exact match (no trailing slash).
    # MCP clients do not follow 301 redirects, so both the exact URL
    # and the trailing-slash form must be proxied directly.
    location = /api/mcp {
        proxy_pass http://nautobot:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_read_timeout    120s;
        proxy_connect_timeout  10s;
    }

    # MCP endpoint — prefix match (trailing slash and sub-paths).
    location /api/mcp/ {
        proxy_pass http://nautobot:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_read_timeout    120s;
        proxy_connect_timeout  10s;
    }

    # OAuth well-known discovery (RFC 8414 + RFC 9728).
    # Required for Claude.ai, ChatGPT, Grok, and other clients that
    # auto-discover OAuth metadata before connecting.
    location /.well-known/ {
        proxy_pass http://nautobot:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
    }

    # OAuth endpoints auto-registered by frisian_mcp.contrib.oauth.
    location /oauth/ {
        proxy_pass http://nautobot:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_read_timeout 60s;
    }

    # All other traffic — Nautobot UI, REST API, admin.
    location / {
        proxy_pass http://nautobot:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_read_timeout    120s;
        proxy_connect_timeout  10s;
    }
}
```

### TLS Termination at Nginx

When Nginx terminates TLS and forwards plain HTTP to Nautobot, the `$http_x_forwarded_proto` variable carries the original scheme (`https`). If your upstream load balancer or CDN (Cloudflare, ALB) also terminates TLS before Nginx, `$http_x_forwarded_proto` may instead be set by that upstream layer — confirm which header your stack delivers before deploying.

For HTTPS-only deployments, add a redirect server block:

```nginx
server {
    listen 80;
    server_name your-nautobot.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name your-nautobot.example.com;

    ssl_certificate     /etc/ssl/certs/nautobot.crt;
    ssl_certificate_key /etc/ssl/private/nautobot.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # ... same location blocks as above ...
}
```

---

## Django Settings for Proxy-Aware Operation

When Nautobot runs behind Nginx, add the following settings to `nautobot_config.py` so that Django correctly reconstructs request URLs and enforces HTTPS:

```python
# nautobot_config.py

# Tell Django that one reverse proxy sits in front of it.
# Increment to 2 if a CDN or load balancer also sits in front of Nginx.
FRISIAN_MCP_TRUSTED_PROXY_COUNT = 1

# Production TLS hardening — applied only when DEBUG is False.
# Nginx handles the HTTP→HTTPS redirect; Django only needs to know
# the incoming scheme via the forwarded header.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = False          # Nginx handles the redirect
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
```

### Why both locations for /api/mcp?

MCP clients send requests to the exact URL they are configured with. If a client is pointed at `https://your-nautobot.example.com/api/mcp` (no trailing slash), Nautobot's default APPEND_SLASH middleware would issue a 301 redirect to `/api/mcp/`. Most MCP clients do not follow redirects — they treat the 301 as an error. The exact-match `location = /api/mcp` block catches this case without a redirect.

frisian-mcp also installs `McpTrailingSlashMiddleware` automatically to normalize the path internally, but the Nginx exact-match block prevents the redirect from reaching Django at all.

---

## CORS Configuration

AI clients that connect from browser sessions (Claude.ai, ChatGPT web, Grok) require CORS headers. Add the following to `nautobot_config.py` alongside the `django-cors-headers` package:

```python
CORS_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://chatgpt.com",
    "https://grok.com",
    "https://x.ai",
]
CORS_ALLOW_CREDENTIALS = True
SESSION_COOKIE_SAMESITE = None
```

Nautobot bundles `django-cors-headers` — no separate installation is required.

---

## Upstream Block (Multi-Worker Deployments)

For Nautobot deployments with multiple gunicorn workers, define a named upstream block to enable connection pooling:

```nginx
upstream nautobot {
    server 127.0.0.1:8080;
    # Add additional workers here if running multiple gunicorn instances
    # server 127.0.0.1:8081;
    keepalive 32;
}
```

Then reference it in the `proxy_pass` directives:

```nginx
proxy_pass http://nautobot;
```

---

## Read Timeout Guidance

MCP tool calls are synchronous HTTP requests. A single tool call that triggers a complex Nautobot query — filtering 10,000 devices, resolving relationships, or running a job — can take 30–90 seconds. The `proxy_read_timeout 120s` value in the location blocks above gives those calls headroom without holding connections open unnecessarily.

If you observe 504 Gateway Timeout errors on large list operations:

1. Increase `proxy_read_timeout` incrementally (180s, then 300s).
2. Add `FRISIAN_MCP_DISPATCH_GROUPS` to your config — group dispatchers reduce per-call data volume, which reduces response time (see [install.md](install.md) Step 7).
3. Consider adding `@mcp_heavy` to the ViewSet actions that return large result sets. This enforces pagination-first behavior, where the agent receives the count and first page rather than the full result set.

---

## Health Check Endpoint

Some MCP clients poll a health check URL before issuing tool calls. frisian-mcp registers `GET /backend/healthcheck/` automatically (returns `{"status": "ok"}`). No Nginx configuration is required for this path — it is handled by the catch-all `location /` block.

To verify:

```bash
curl https://your-nautobot.example.com/backend/healthcheck/
# Expected: {"status": "ok"}
```

---

## Verifying the Configuration

After restarting Nginx and Nautobot, run these checks:

```bash
# 1. MCP endpoint responds (unauthenticated ping)
curl -X POST https://your-nautobot.example.com/api/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"ping","id":1}'

# 2. Exact-match URL (no trailing slash) does not redirect
curl -I https://your-nautobot.example.com/api/mcp
# Expect: HTTP/2 200 (not 301)

# 3. OAuth well-known discovery (if contrib.oauth is installed)
curl https://your-nautobot.example.com/.well-known/oauth-authorization-server
# Expect: JSON document with issuer, authorization_endpoint, token_endpoint

# 4. Health check
curl https://your-nautobot.example.com/backend/healthcheck/
# Expect: {"status": "ok"}
```

---

See [Troubleshooting](../../../../troubleshooting/Django/nautobot/3.x/troubleshooting.md) for common problems and solutions including proxy-specific issues.

---

*Document written: 2026-05-21*
