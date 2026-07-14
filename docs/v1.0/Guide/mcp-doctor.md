# `mcp_doctor` — Configuration audit and health check

**Category:** guide  
**Slug:** mcp-doctor  
**Audience:** Operators verifying a frisian-mcp deployment or diagnosing an integration that isn't behaving as expected

---

## What it is

`mcp_doctor` is a Django management command shipped with frisian-mcp that walks the host's configuration end-to-end and reports anything that looks misconfigured, missing, or risky. It's the first command to run after a fresh install, after every config change, and as the first diagnostic step when something stops working.

```bash
python manage.py mcp_doctor                  # standard audit
python manage.py mcp_doctor --security       # standard audit + extended security pass
```

Exit code is `0` when no errors are found (warnings still allowed) and `1` when at least one error condition is detected. Suitable for CI gating.

Each result is prefixed with:

| Mark | Meaning |
|---|---|
| `✓` | Check passed |
| `⚠` | Warning — works, but the operator should review |
| `✗` | Error — integration will not function correctly |

---

## Standard audit (8 checks)

Run on every invocation. No flags required.

### 1. `INSTALLED_APPS` consistency

Verifies `frisian_mcp` is in `INSTALLED_APPS` and reports which contrib apps are installed (`contrib.tokens`, `contrib.oauth`, `contrib.agents`). Errors when `contrib.agents` is installed without `contrib.tokens` (the FK requires it).

### 2. URL mounting

Resolves `frisian_mcp:gateway` and the `frisian_mcp_oauth_wellknown:oauth_authorization_server` named URLs. Warns when either is unreachable and prints the `path(...)` line to add to `ROOT_URLCONF`.

### 3. Authentication wiring

Cross-checks `FRISIAN_MCP_AUTHENTICATION_CLASSES` against installed contrib apps. Warns when `contrib.tokens` or `contrib.oauth` is installed but the corresponding authenticator isn't in the chain — those tokens would silently fail to authenticate.

### 4. Security settings

Audits `DEBUG`, `FRISIAN_MCP_HMAC_KEY`, `FRISIAN_MCP_UNAUTHENTICATED_TIER`, and `FRISIAN_MCP_TRUSTED_PROXY_COUNT`. Flags:

- `DEBUG=True` (not for production)
- Missing dedicated HMAC key (token HMACs fall back to `SECRET_KEY`, coupling token validity to Django's session key)
- `UNAUTHENTICATED_TIER` set to `read_write` or `admin` (anonymous write/admin grants)
- `TRUSTED_PROXY_COUNT=0` in non-debug mode when a reverse proxy is in play

### 5. Cache backend

Warns when `CACHES['default']` is `LocMemCache` and `contrib.oauth` is installed. Per-process caches break OAuth authorization codes in multi-worker deployments (gunicorn/uWSGI workers don't share LocMem).

### 6. Performance hints

Reads the registered tool count and the tier distribution. Warns when:

- Tool count > 80 and `FRISIAN_MCP_TOOLS_PAGE_SIZE` is unset → recommends ~50
- Tool count > 80 and `FRISIAN_MCP_TOOLS_LIST_CACHE_TTL` is unset → recommends enabling the cache

### 7. OAuth registration posture

Reports whether `FRISIAN_MCP_OAUTH_REGISTRATION_OPEN` is `True` (agents can self-register via RFC 7591 DCR) or `False` (operator must pre-register every OAuth client via the Django admin). Both are valid postures; the report names which one is active so it's not silently wrong.

### 8. OAuth authorize URL reachability

When `FRISIAN_MCP_OAUTH_AUTHORIZE_URL` is set, sends an HTTP `GET` to it and reports the response status. Catches URL typos and misconfigured routes before an agent hits them.

---

## Extended security audit (`--security` flag)

Adds six additional checks aimed at OAuth-specific misconfigurations.

### 1. OAuth service user attribution

Warns when `contrib.oauth` is installed but `FRISIAN_MCP_OAUTH_SERVICE_USER` is not set. Without it, OAuth-authenticated requests run as `OAuthServicePrincipal` (no Django `User` row), which breaks host models that require a real `User` FK on audit records.

### 2. Service account user privilege

When `FRISIAN_MCP_SERVICE_ACCOUNT_USER` is set, looks up the named user. Warns if the user is a `superuser` or `is_staff` — anonymous callers would inherit elevated host-app permissions. Recommends a dedicated low-privilege service account instead.

### 3. Request body size limit

Warns when `FRISIAN_MCP_REQUEST_BODY_MAX_SIZE` is not explicitly set. The default is 1 MiB; explicit configuration documents intent and lets the operator tune the limit per deployment.

### 4. PKCE auto-registration

Warns when `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True` outside of DEBUG. With it enabled, any caller can register a new OAuth client by presenting an unknown `client_id` at `/oauth/authorize/`. Acceptable for local dev or an explicitly open MCP platform; surface for review otherwise.

### 5. `registration_endpoint` consistency

Reports whether `registration_endpoint` is advertised in `.well-known` metadata. Useful when diagnosing the cases where a discovery-first OAuth client can't find DCR and bails with *"Incompatible auth server: does not support dynamic client registration."*

### 6. HMAC key rotation safety

Warns when `FRISIAN_MCP_HMAC_KEY` either isn't set or equals `SECRET_KEY`. In both cases, rotating `SECRET_KEY` invalidates every issued frisian-mcp token. Recommends a separate randomly-generated HMAC key for independent rotation.

---

## Example output

```text
$ python manage.py mcp_doctor

  ✓ frisian_mcp in INSTALLED_APPS
  ✓ frisian_mcp.contrib.tokens in INSTALLED_APPS
  ✓ frisian_mcp.contrib.oauth in INSTALLED_APPS
  ✓ MCP gateway mounted at /api/mcp/
  ✓ OAuth .well-known URLs mounted
  ✓ FrisianMcpTokenAuthentication wired in FRISIAN_MCP_AUTHENTICATION_CLASSES
  ✓ OAuthTokenAuthentication wired in FRISIAN_MCP_AUTHENTICATION_CLASSES
  ✓ DEBUG=False
  ✓ FRISIAN_MCP_HMAC_KEY set — token HMACs are independent of SECRET_KEY
  ✓ FRISIAN_MCP_UNAUTHENTICATED_TIER='read' — anonymous callers see only read-tier tools
  ✓ FRISIAN_MCP_TRUSTED_PROXY_COUNT=1
  ✓ 1737 tool(s) registered — tier distribution: read=1244, read_write=348, admin=145
  ⚠ 1737 tools registered and FRISIAN_MCP_TOOLS_PAGE_SIZE is unset — tools/list returns the full manifest in one response. Consider setting FRISIAN_MCP_TOOLS_PAGE_SIZE to ~50 to enable cursor pagination
  ⚠ FRISIAN_MCP_OAUTH_REGISTRATION_OPEN=False — agents cannot self-register. Discovering agents (e.g. Claude.ai) will see no registration_endpoint in the .well-known metadata and must use pre-provisioned credentials. Set to True if you want end-to-end agent autodiscovery.
  ✓ Cache backend: django_redis.cache.RedisCache

No errors. 2 warning(s) to review.
```

---

## When to run it

- **Immediately after install**, before connecting any MCP client. Catches forgotten settings (`INSTALLED_APPS` missing the contrib app, auth class not wired, missing HMAC key) that would otherwise surface as confusing client-side failures.
- **After every config change** to `nautobot_config.py` / `configuration.py` / your Django settings. Verifies the change applied as intended and didn't silently disable an unrelated invariant.
- **Before every release**, as part of your CI smoke. Exits non-zero on errors, so a CI step like `python manage.py mcp_doctor` gates the deploy.
- **First diagnostic step** whenever a client suddenly can't connect or behaviour drifts. Doctor often surfaces the root cause in seconds (wrong auth chain order, cache backend regression, missing setting after an upgrade) that would otherwise take hours of log tailing.

---

## CI integration

```yaml
# .github/workflows/deploy.yml (excerpt)
- name: Verify frisian-mcp configuration
  run: |
    docker exec my-app python manage.py mcp_doctor --security
```

A non-zero exit fails the deploy. No errors → green light. Warnings are surfaced in the run log for the operator to review but don't block.

---

## See also

- [Installation & Configuration Reference](../Reference/installation-configuration-reference.md) — every setting the doctor checks against, in one place.
- [Security-First MCP Architecture](../Security/security.md) — the security posture the `--security` flag audits against.
- [Permission-Aware Discovery](permission-aware-discovery.md) — the discovery model the doctor verifies wiring for.
