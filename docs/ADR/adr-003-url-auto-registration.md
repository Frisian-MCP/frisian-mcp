# ADR 003: URL Auto-Registration via AppConfig.ready()

**Category:** reference  
**Slug:** adr-003-url-auto-registration  
**Status:** Accepted  
**Date:** 2026-05-05

---

## Context

Standard Django package installation requires the operator to add URL patterns to the project's `urls.py`:

```python
# project/urls.py
from django.urls import include, path

urlpatterns = [
    ...
    path('mcp/', include('frisian_mcp.urls')),
    path('.well-known/', include('frisian_mcp.contrib.oauth.wellknown_urls')),
    path('oauth/', include('frisian_mcp.contrib.oauth.urls')),
]
```

This is the conventional Django way, and it is fine for projects where the operator has direct control over `urls.py`. It becomes a problem in two specific cases that surfaced during integration testing.

**Operators who cannot edit `urls.py`.** Large multi-app frameworks have their own URL configuration conventions. Network automation platforms, content management systems, and other established Django frameworks provide hooks for plugins to register URLs but do not expect plugins to require root URL configuration changes. An operator installing frisian-mcp into one of these systems may not have a clean place to put the `include()` calls, or may not have permission to modify the root URL configuration at all.

**Multiple URL surfaces that must agree.** frisian-mcp's OAuth implementation requires several URLs to work together:

- `/.well-known/oauth-protected-resource` (RFC 9728)
- `/.well-known/oauth-authorization-server` (RFC 8414)
- `/oauth/authorize/` (PKCE authorization endpoint)
- `/oauth/token/` (token exchange)
- `/oauth/register/` (RFC 7591 dynamic client registration)
- The MCP endpoint itself, whose `WWW-Authenticate` header must reference the well-known metadata URL

If any one of these is missing or misconfigured, the OAuth discovery chain breaks silently. Claude or GPT connecting to the server fails to trigger the auth flow at all, with no obvious error message — just an MCP connection that returns only public read-only tools.

During v0.2.0 development, the OAuth integration broke repeatedly because operators forgot one of the `include()` calls, configured the paths inconsistently, or had subtle URL prefix mismatches. The fix-attempt-fail loop took multiple sessions to diagnose each time.

The conventional Django answer — write better documentation — addresses the symptom, not the cause. The cause is that a working configuration requires the operator to know about and correctly wire five or six related URL patterns, in the right order, with consistent prefixes.

## Decision

frisian-mcp registers its URL patterns automatically at startup via `AppConfig.ready()`. The operator adds entries to `INSTALLED_APPS` and configures relevant settings; URL registration happens without `urls.py` changes.

```python
INSTALLED_APPS = [
    ...
    'frisian_mcp',
    'frisian_mcp.contrib.oauth',
]

FRISIAN_MCP_PATH = 'api/mcp'           # mounts at /api/mcp/
FRISIAN_MCP_OAUTH_ISSUER = 'https://your-domain.com'
```

At startup, `FrisianMcpConfig.ready()` runs three URL injection routines:

1. `_install_mcp_url()` — mounts the `McpView` at `FRISIAN_MCP_PATH`
2. `_install_wellknown_urls()` — mounts the `/.well-known/oauth-*` discovery endpoints
3. `_install_oauth_urls()` — mounts the OAuth authorize/token/register endpoints

Each routine resolves the project's root URL configuration, locates the appropriate insertion point, and adds the URL patterns. The operator's existing `urls.py` is not modified on disk; URL patterns are added to the in-memory URLconf at process startup.

## Why This Is Not the Standard Django Pattern

Django's documentation explicitly recommends explicit URL inclusion. The convention is "explicit is better than implicit" — operators should know what URLs their project serves, and that knowledge should be visible in `urls.py`.

For most Django packages, this is the right call. A package adding two or three URLs that the operator can reasonably understand and verify benefits from explicit inclusion.

frisian-mcp's case is different in three ways:

**The URLs must agree with each other.** The OAuth discovery chain only works when six related URLs are configured consistently. A package that auto-registers all of them as a unit eliminates the configuration class of the problem. A partial install — the operator added some URLs but not others — is impossible.

**The URLs are protocol artifacts, not application surface.** The MCP endpoint and OAuth endpoints are not part of the operator's API design. They are infrastructure required by the MCP specification and the OAuth RFCs. Forcing the operator to wire them manually is exposing implementation detail that has no domain meaning to the host application.

**The cost of misconfiguration is silent failure.** A wrong `path()` call in a normal Django app produces a 404 the developer notices immediately. A wrong OAuth metadata URL produces an MCP server that appears to work but cannot authenticate. The agent connects, calls `tools/list`, sees only public tools, and the developer has no obvious signal that something is wrong.

The trade-off is correct for frisian-mcp specifically. It would not be correct for a general-purpose Django package.

## What Operators Can Still Override

Auto-registration provides defaults. Operators retain control:

- `FRISIAN_MCP_PATH` controls the MCP endpoint mount path
- `FRISIAN_MCP_OAUTH_ISSUER` controls the issuer base URL used for all generated OAuth metadata URLs
- `FRISIAN_MCP_AUTOREGISTER_URLS = False` disables auto-registration entirely for operators who want manual control

The escape hatch matters. Projects with unusual URL configuration requirements, or operators who prefer explicit `urls.py` inclusion for visibility, can opt out. The default behavior is automatic; the opt-out is one setting.

## Consequences

**Positive.** Adopters of frisian-mcp `INSTALLED_APPS` + settings, no `urls.py` changes for the standard case. The OAuth discovery chain works on first install without manual URL wiring.

**Positive.** Multi-app frameworks become viable hosts. The integration with the network automation platform succeeded in part because frisian-mcp did not require root `urls.py` modification — the operator added `INSTALLED_APPS` entries and the URL patterns appeared automatically.

**Positive.** Configuration drift is harder to introduce. The OAuth URL prefixes cannot become inconsistent because the same code path generates all of them.

**Negative.** Less visible. An operator looking at `urls.py` does not see the MCP or OAuth URLs. Verifying which URLs are mounted requires checking the running server (`./manage.py show_urls` or equivalent) rather than reading the source.

**Negative.** Diverges from Django convention. Developers familiar with the standard explicit pattern may find the auto-registration unexpected at first. Documentation must call this out.

**Negative.** URL conflicts are detected at startup rather than statically. If the host project already has a path at `/mcp/`, frisian-mcp's auto-registration may collide. The package logs a warning and refuses to mount when this is detected, but the operator must understand the warning to act on it.

The visibility cost is paid by every operator. The configuration correctness benefit is paid every time someone configures OAuth — which used to be every time, before auto-registration. The trade-off favored auto-registration.

## Validation

Auto-registration was implemented during v0.2.0 OAuth work. Before the change, OAuth integration took multiple sessions per host application to diagnose configuration issues. After the change, OAuth flows worked on first install across:

- The `frisian-mcp-api` demo server (this server)
- The network automation platform integration (1,967-tool deployment)
- The consumer iOS application Django backend
- The multi-agent orchestration platform

In each case, the operator added `INSTALLED_APPS` entries and configured `FRISIAN_MCP_OAUTH_ISSUER`. No `urls.py` changes were required. Claude.ai and GPT both completed the OAuth flow against each deployment without manual intervention.

The configuration class of OAuth bug — "operator forgot to wire one of the URL patterns" — has not recurred since auto-registration shipped.

---

*ADR maintained alongside the frisian-mcp source.*
