# Installing frisian-mcp with Open edX (LMS)

**Audience:** Open edX platform engineers adding MCP gateway support  
**Platform:** Open edX **Ulmo or later** · Django 5.0+ · Python 3.11+

> ## ⛔ Do not follow this guide on Sumac, Teak, or Redwood
>
> **This directory is named `sumac/` for historical reasons, and Sumac cannot
> run frisian-mcp.** The path is kept only so existing links do not break.
>
> frisian-mcp requires `django>=5.0`. Open edX pins Django per release — read
> from each release branch's `requirements/edx/base.txt`:
>
> | release | Tutor | Django | takes frisian-mcp? |
> |---|---|---|---|
> | Redwood | — | 4.2 | ❌ no |
> | Sumac | 19 | 4.2.20 | ❌ no |
> | Teak | 20 | 4.2.20 | ❌ no |
> | Ulmo | 21 | 5.2.7 | ✅ yes |
> | Verawood | 22 | 5.2.13 | ✅ yes |
>
> On a 4.2 release `pip` either refuses to resolve, **or force-upgrades Django
> to 5.x underneath a platform that pins 4.2.20** — the second outcome is worse,
> because it looks like it worked. The supported target is **Ulmo or later**.

<!-- Separates two adjacent callouts; without it markdownlint reads the blank
     line as a gap inside a single blockquote (MD028). -->

> ## ⚠️ Integration status — read before deploying
>
> **What is validated:** the plugin-app wiring (`PluginURLs` registration, URL
> mounting, settings propagation) and the settings template in
> [`lms/envs/mcp_prod.py`](lms/envs/mcp_prod.py).
>
> **What is NOT validated:** end-to-end OAuth and real tool calls have never
> been completed on a real Open edX deployment. An earlier claim of "validated
> through migrations and server startup" was obtained on **SQLite**, through
> monkeypatches to Django's schema editor. Open edX runs on **MySQL**, and the
> difference is not academic — see the migration blocker in Step 5.
>
> **The tool and dispatch-group counts in this document are inherited from that
> SQLite run and have not been re-measured on a real deployment.** Treat them as
> indicative, not as a specification.
>
> Treat this document as a working starting point, not a fully validated
> production guide.

---

## Overview

frisian-mcp is a Django package that turns your existing Django REST Framework ViewSets into Model Context Protocol (MCP) tools with zero boilerplate. When installed in the Open edX LMS, the platform's user, enrollment, assessment, organization, and LTI surfaces automatically become callable by any MCP-compatible AI client.

A default Open edX LMS installation exposed 78 auto-discoverable ViewSet actions when this document was written, bundled by frisian-mcp's dispatch-group system into 9 topic-level tools — users, enrollments, LTI, organizations, assessment, auth, retirement, data, and xqueue. *(Measured on the SQLite scaffold described in the integration-status note above; not re-confirmed on a real deployment.)*

---

## Prerequisites

| Requirement | Version |
|---|---|
| Open edX | **Ulmo (Tutor 21) or later** — Verawood (Tutor 22) is current |
| Django | 5.0 or newer — **excludes Sumac, Teak, and Redwood**, which pin 4.2 |
| Python | 3.11 or newer |
| Django REST Framework | 3.14+ (bundled with Open edX) |
| Database | MySQL — ⚠️ see the migration blocker in Step 5 before deploying |
| Redis | Required — OAuth PKCE stores authorization codes in the default cache |

> **Cache requirement:** Open edX's test/devstack settings configure `DummyCache` for all backends. frisian-mcp's OAuth PKCE flow stores authorization codes in the default cache, so a real backend (Redis) is required in any environment where OAuth will be used. See Step 3.

---

## Open edX Plugin System — Why a Plugin App Is Required

Open edX uses `edx_django_utils.plugins` for URL injection rather than Django's standard URL routing. Third-party apps that need to add URL patterns must register them via a `PluginURLs.CONFIG` entry in their `AppConfig`. There is no equivalent to the `frisian_mcp.AppConfig.ready()` auto-injection that works in standard Django projects.

Additionally, `admin/login/` in the LMS is hard-wired to redirect to a React login page. A thin URL override is needed to expose Django admin's plain HTML login view (required for frisian-mcp's OAuth admin interface).

For these reasons, installing frisian-mcp into Open edX requires a small plugin app.

> ### ⚠️ This repository does not ship that plugin app
>
> Earlier revisions of this document told you to install a bundled
> `openedx_frisian_mcp/` directory. **No such directory exists in this
> repository, and none is published as a package.** You must supply the plugin
> app yourself.
>
> **Do not go looking for an older copy.** Pre-rename scaffolds of this app
> exist in archived checkouts, and they carry a development authentication
> shim (`dev_auth.py`) that authenticates any request arriving with **no
> `Authorization` header** as the first superuser, alongside
> `UNAUTHENTICATED_TIER = "admin"` and `ALLOW_UNAUTHENTICATED = True`.
> Composed, those grant an anonymous caller superuser identity at the admin
> tier — an open admin door onto the entire LMS. They are labelled dev-only and
> they are not safe to adapt. See **What the plugin app must contain** below for
> the whole of what is required, and **Plugin App Reference** at the end of this
> document for what must never be in it.

### What the plugin app must contain

Two modules, and nothing else:

| File | Purpose |
|---|---|
| `apps.py` | An `AppConfig` carrying a `plugin_app` dict that registers the MCP, OAuth, and well-known URLs for `ProjectType.LMS` via `PluginURLs` |
| `urls.py` | The URL patterns that `PluginURLs.RELATIVE_PATH` points at |

The `AppConfig` needs an **empty** `PluginURLs.NAMESPACE`. frisian-mcp's own URL
names are already prefixed, and adding a namespace here breaks the `reverse()`
calls the OAuth views make against them.

A worked, minimal example of exactly these two modules — with the unsafe
modules named and deliberately excluded — is in the `frisian-mcp-demo`
repository on branch `feat/openedx-server-test`, at
`server-tests/openedx/plugin/openedx_frisian_mcp/`. It is a test harness, not a
supported deliverable; read it as a reference, not as something to depend on.

No Open edX source files are modified.

---

## Step 1 — Install the Packages

```bash
pip install "frisian-mcp[usage]"
```

**Install the `[usage]` extra.** Without it frisian-mcp cannot load a real
tokenizer and silently falls back to a characters-divided-by-four estimate:
every tool result then reports `"encoding": "approx-char4"` rather than
`"encoding": "cl100k_base"`. Nothing announces the downgrade — the `_usage`
block looks equally authoritative either way — so token accounting, and any
budgeting built on it, becomes quietly approximate. Verify after installing:

```bash
python -c "from frisian_mcp.usage import encoding_name; print(encoding_name())"
# expect: cl100k_base
```

Then install your plugin app — see **What the plugin app must contain** above;
this repository does not ship one:

```bash
pip install -e ./path/to/your_openedx_frisian_mcp/
```

Or copy it into your Open edX platform tree and add it to your requirements.

---

## Step 2 — Add to INSTALLED_APPS

In your LMS settings file (e.g. `lms/envs/private.py` or `lms/envs/production.py`), add the required apps:

```python
INSTALLED_APPS = list(INSTALLED_APPS) + [
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
    "openedx_frisian_mcp",
]
```

Use `list(INSTALLED_APPS) + [...]` rather than `.append()` — Open edX's `common.py` calls `get_plugin_apps()` which may return a tuple; the `+` operator ensures a mutable list.

The `openedx_frisian_mcp` app's `AppConfig` registers the MCP URL patterns automatically via the `PluginURLs` mechanism when the LMS starts.

---

## Step 3 — Ensure a Real Cache Backend

OAuth PKCE stores authorization codes in Django's default cache. Verify your production settings use Redis (Open edX ships with Redis, so this is typically already configured):

```python
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://localhost:6379/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    },
}
```

If you see `OAuth authorization code not found` errors, the cache backend is the first thing to verify.

---

## Step 4 — Configure Settings

All `FRISIAN_MCP_*` settings go in your LMS settings file. See `lms/envs/mcp_prod.py` in this repository for the full production template.

### Minimum configuration

Mounts frisian-mcp at `/mcp/` and requires authentication for every request —
the default secure posture. Add an MCP token via the Django admin
(**Plugins → frisian-mcp → MCP Tokens**) once the plugin app is installed.

```python
# lms/envs/private.py (or production.py override)

INSTALLED_APPS = list(INSTALLED_APPS) + [
    "frisian_mcp",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.tokens",
    "openedx_frisian_mcp",
]

FRISIAN_MCP_PATH = "mcp"

# Deny unauthenticated callers entirely: no tool is listed and none can be
# invoked without credentials.
FRISIAN_MCP_UNAUTHENTICATED_TIER = None

FRISIAN_MCP_PERMISSION_CLASSES = [
    "rest_framework.permissions.IsAuthenticated",
]
```

For an anonymous-readable surface, set
`FRISIAN_MCP_UNAUTHENTICATED_TIER = "read"` and drop the `IsAuthenticated`
permission class. On an LMS this exposes the user, enrollment, and organization
surfaces to unauthenticated callers — it is **not** what a production deployment
wants. The hardened posture below is.

> **If you are upgrading from a copy of this config older than frisian-mcp
> 1.1.0:** `FRISIAN_MCP_UNAUTHENTICATED_TIER = None` did not have the effect its
> name implies. `None` and `"none"` were unrecognised tier values that ranked
> equal to `"read"`, so anonymous callers kept the full read surface. If you
> deployed an earlier copy believing it required authentication, it did not —
> check what was mounted and reachable.

### Recommended production configuration

See [`lms/envs/mcp_prod.py`](lms/envs/mcp_prod.py) for the full settings template including OAuth, dispatch groups, authentication chain, and reverse proxy support.

### Authentication class order

**ALWAYS list `FrisianMcpTokenAuthentication` BEFORE `OAuthTokenAuthentication` when both are present.** As of frisian-mcp 1.0.11 both classes return `None` on lookup-miss (so either order works for correctness), but the FIRST authenticator in the chain emits the WWW-Authenticate challenge on 401 responses. Tokens-first emits a bare `Bearer` challenge, which static-token MCP clients (Claude Code, Codex, Gemini CLI) accept and fall back to their configured Bearer cleanly. OAuth-first emits `Bearer realm="...", resource_metadata="..."`, which nudges discovery-first clients into the OAuth cascade — fine if every client is an OAuth client, but a footgun the moment you add a static-token coding agent.

```python
FRISIAN_MCP_AUTHENTICATION_CLASSES = [
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpApiKeyAuthentication",
    "frisian_mcp.contrib.tokens.authentication.FrisianMcpTokenAuthentication",
    "frisian_mcp.contrib.oauth.authentication.OAuthTokenAuthentication",
]
```

> **Historical note.** Earlier versions of frisian-mcp (pre-1.0.11) raised `AuthenticationFailed` on lookup-miss in both classes, which DID make ordering load-bearing for correctness in the *opposite* direction — token-first would reject OAuth tokens. Docs from that era recommended OAuth-first. The 1.0.11 chain fix removed that constraint; the new convention is tokens-first for the WWW-Authenticate-shape reason above.

---

## Step 5 — Run Migrations

frisian-mcp adds database tables for OAuth clients, tokens, and access tokens:

```bash
python manage.py lms migrate
```

> ### 🔴 This step currently fails on MySQL
>
> Open edX runs on MySQL. The `frisian_mcp_oauth.0003` migration declares a
> `UniqueConstraint` whose combined column length exceeds MySQL's 3,072-byte
> index limit, so `migrate` aborts partway through — which can leave an orphaned
> consent table behind, as the upgrade note below explains. Every OAuth-dependent
> step in this document is blocked behind it.
>
> This is tracked as
> [frisian-mcp #72](https://github.com/Frisian-MCP/frisian-mcp/issues/72).
> **It is not reproducible on SQLite**, which has no index-length limit — which
> is why it reached a release. Check the issue for the fix status before
> planning a deployment.
>
> **If you have already run `migrate` on MySQL, upgrading is not enough.**
> MySQL cannot roll back DDL (`can_rollback_ddl` is `False`, so `Migration.atomic`
> has no effect there). The failed run therefore leaves the table
> `frisian_mcp_oauth_oauthauthorizeconsent` **committed but with no migration
> record**, and the next `migrate` fails earlier and differently:
>
> ```text
> django.db.utils.OperationalError: (1050, "Table
> 'frisian_mcp_oauth_oauthauthorizeconsent' already exists")
> ```
>
> That orphaned table has to be dealt with before the corrected migration can
> apply. Follow the remediation in #72 rather than improvising — the exact steps
> depend on which fix shape ships.

---

## Step 6 — No Open edX Source Files Modified

frisian-mcp does not modify any Open edX source files.

The `openedx_frisian_mcp` plugin app wires URLs and settings entirely through Open edX's own extension points (`PluginURLs`, `AppConfig.ready()`). Open edX core code, models, serializers, views, and URL configurations are untouched.

The gateway will be available at:

```text
https://your-lms.example.com/mcp/
```

---

## Step 7 — Verify Startup

Start the LMS normally. On the first incoming request, frisian-mcp scans the URL tree and registers all discovered tools:

```text
[frisian-mcp] registered 78 tools at /mcp/
[frisian-mcp] 9 dispatch group(s) bundling 78 tools
```

If you see `registered 0 tools`, verify that `openedx_frisian_mcp` is in `INSTALLED_APPS` and that the app appears **after** `frisian_mcp` in the list.

---

## Step 8 — Configure Dispatch Groups (Recommended)

Open edX exposes 78 tools across user, enrollment, LTI, and operational surfaces. The dispatch group configuration below was derived from the full auto-discovered surface during integration testing:

```python
FRISIAN_MCP_DISPATCH_GROUPS = {
    # User accounts, preferences, agreements, name changes
    "users": [
        "accounts", "me", "user", "user_agreements", "userpreference", "name_change",
    ],
    # Course enrollments, entitlements, and credit
    "enrollments": [
        "enrollments", "entitlements", "creditcourse", "creditprovider",
    ],
    # LTI (Learning Tools Interoperability) — AGS grades + NRPS memberships
    "lti": [
        "lti_ags_view", "lti_nrps_memberships_view",
    ],
    # Generic data store and key-value pairs
    "data": [
        "data", "key_value",
    ],
    # Organizations and SAML SSO configuration
    "organizations": [
        "organization", "saml_configuration",
    ],
    # Peer assessment feedback
    "assessment": [
        "assessment_feedback",
    ],
    # Auth — token creation, account confirmation, email lookup
    "auth": [
        "create_token", "confirm", "search_emails",
    ],
    # User retirement / GDPR erasure pipeline
    "retirement": [
        "cancel_retirement", "retire", "retire_misc",
        "retirement_cleanup", "retirement_partner_report",
        "retirement_queue", "retirement_status",
        "retirements_by_status_and_date", "update_retirement_status",
    ],
    # Grade submission queue
    "xqueue": [
        "xqueue",
    ],
}
```

### Large response negotiation

Platform ViewSets cannot be decorated with `@mcp_heavy` without modifying platform source files. `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` negotiates large responses only for schema-disclosing heavy tools and dispatchers. Plain non-disclosing actions over the threshold return complete inline JSON, with no continuation token:

```python
FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = 8_000  # bytes
```

---

## Step 9 — Connect an MCP Client

### Using a frisian-mcp Static API Key

```python
# FRISIAN_MCP_API_KEYS is keyed by the HMAC-SHA256 DIGEST of each raw key,
# not the raw key itself — so a leaked settings file exposes no usable
# credential. Generate a digest for each key:
#     python manage.py lms mcp_hash_api_key <raw-key>
FRISIAN_MCP_API_KEYS = {
    "<64-hex HMAC-SHA256 digest of your read-write key>": "read_write",
    "<64-hex HMAC-SHA256 digest of your read-only key>":  "read",
}
```

```json
{
  "mcpServers": {
    "openedx": {
      "type": "http",
      "url": "https://your-lms.example.com/mcp/",
      "headers": {
        "Authorization": "Bearer <your-raw-key>"
      }
    }
  }
}
```

### Using OAuth (Claude.ai, ChatGPT, Grok)

AI clients connect via the OAuth 2.1 PKCE authorization-code flow, with
`FRISIAN_MCP_OAUTH_ISSUER` set and `frisian_mcp.contrib.oauth` installed. Point
the client at:

```text
https://your-lms.example.com/mcp/
```

> **Self-registration is closed in the recommended posture, and should stay
> closed.** [`lms/envs/mcp_prod.py`](lms/envs/mcp_prod.py) sets
> `FRISIAN_MCP_OAUTH_REGISTRATION_OPEN`, `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER`,
> and `FRISIAN_MCP_OAUTH_AUTO_APPROVE` all to `False`. Leaving **any** of the
> three set to `True` re-opens an anonymous walk-up path: any caller can POST to
> `/oauth/register/`, complete PKCE, and receive a Bearer token with no operator
> involvement. On an LMS that is an unauthenticated stranger holding a token
> against your student data.

The operator pre-registers each client instead:

1. Sign in to the Django admin and go to **Plugins → frisian-mcp → OAuth
   Clients → Add**.
2. Pick a permission tier (`read`, `read_write`, or `admin`) and attach the
   Django user the client's MCP requests will run as. Use a dedicated service
   account scoped to what you want exposed — **not** the `edx` superuser. A
   `client_id` and a one-time `client_secret` are shown on the success page.
3. Paste those into the AI client's connector settings (Claude.ai: **Connect MCP
   Server → Advanced**; ChatGPT and Grok have equivalent forms).
4. The client completes PKCE against `/oauth/authorize/`, you approve the
   consent screen, and it receives an access token.

`FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION` should be `"read"`. A `read_write`
default combined with any of the three registration flags above means a walk-up
client gets write access without anyone approving it.

---

## Plugin App Reference

**This repository ships no plugin app.** The tables below describe what yours
should and should not contain.

### Required

| File | Purpose |
|---|---|
| `apps.py` | `AppConfig` with `PluginURLs` registration, empty namespace |
| `urls.py` | Explicit MCP, OAuth, and well-known URL patterns |

### ⛔ Must not be present

Archived pre-rename scaffolds of this app carry the modules below. They were
honest development affordances, labelled as such. They are listed here because
the natural recovery from the missing-directory instruction in earlier
revisions of this document was to find one of those scaffolds and copy it
whole.

| File | Why it must not ship |
|---|---|
| `dev_auth.py` | `DevServiceUserAuthentication` returns the first superuser for **any request with no `Authorization` header**. Paired with the `UNAUTHENTICATED_TIER = "admin"` / `ALLOW_UNAUTHENTICATED = True` settings it shipped beside, an anonymous request becomes a superuser at the admin tier. `request.auth` is left `None` deliberately so the tier resolver falls through to the unauthenticated tier — the two halves are built to work together. **Never adapt this.** |
| `mcp_dev_urls.py` | Overrides `admin/login/` to serve Django's plain admin login instead of the LMS React redirect. Genuinely useful when driving OAuth consent in a browser, but it is a dev affordance and belongs behind an explicit, loudly-named setting in its own module — not beside the production wiring. |
| `mcp_request_log_middleware.py` | Logs `Authorization` header contents on MCP requests. Purpose-built forensics for a client bug; logging credentials is not something to leave switched on. |

If you want a development bypass, put it behind an explicit setting in a module
of its own, and make it impossible to enable by accident. Do not place it in the
module that performs the production wiring.

---

## Next Steps

- [Troubleshooting](../../../../v1.1/troubleshooting/Django/openedx/sumac/troubleshooting.md) — common problems and solutions
- [Installation & Configuration Reference](../../../../v1.1/Reference/installation-configuration-reference.md) — complete settings reference

---

*Document written: 2026-05-22*
