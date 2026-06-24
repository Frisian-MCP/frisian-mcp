# Troubleshooting: frisian-mcp with NetBox 4.x

**Audience:** NetBox administrators diagnosing problems with the MCP gateway  
**Related:** [install.md](../../../../installs/Django/netbox/4.x/install.md)

---

## First step on any unexpected behaviour: run `mcp_doctor`

Before tailing logs or pasting curl output, run the configuration audit. It surfaces the most common integration issues — missing contrib app, auth class not wired, missing HMAC key, cache backend regression, OAuth posture mismatch — in seconds.

```bash
docker exec <container> python manage.py mcp_doctor
```

Exits non-zero on errors. Warnings are flagged with `⚠`; errors with `✗`. See [Guide → mcp_doctor](../../../../Guide/mcp-doctor.md) for what each check looks at. If a symptom below isn't an exact match, doctor output usually narrows it down before any other diagnostic.

If your deployment uses OAuth (the `frisian_mcp.contrib.oauth` app), follow up with the OAuth-specific audit. Skip this on token-only deployments — it will report noisy, irrelevant failures for OAuth checks that don't apply:

```bash
docker exec <container> python manage.py mcp_doctor --security
```

---

## Installation

### `pip: command not found` or `No module named ensurepip` (official netbox-docker image)

The official `netboxcommunity/netbox` Docker image uses Python 3.14 and ships `uv` as the package manager. The Python venv does not include `pip` or `ensurepip`.

**Fix:** Use `uv pip install` instead of `pip install`. The venv is owned by root, so you must run as root:

```bash
docker exec -u root <netbox-container> bash -c "
  export VIRTUAL_ENV=/opt/netbox/venv
  /usr/local/bin/uv pip install setuptools
  /usr/local/bin/uv pip install --no-build-isolation -e /opt/frisian-mcp
"
```

Or set `user: "0:0"` in `docker-compose.override.yml` and run the install as part of the container startup command. See `development/docker-entrypoint.frisian-mcp.sh` for a complete example that handles both `pip` and `uv` automatically.

---

### `ModuleNotFoundError: No module named 'setuptools'` during install

frisian-mcp uses the setuptools build backend. When installing with `--no-build-isolation`, setuptools must already be present in the venv.

**Fix:** Install setuptools before frisian-mcp:

```bash
/usr/local/bin/uv pip install setuptools
/usr/local/bin/uv pip install --no-build-isolation -e /opt/frisian-mcp
```

---

### `Required parameter ALLOWED_HOSTS is missing from configuration`

**Cause:** The `NETBOX_CONFIGURATION` environment variable was set to a frisian-mcp config file. NetBox's own `settings.py` reads `NETBOX_CONFIGURATION` to locate its primary configuration module. If it points to a file that does not contain `ALLOWED_HOSTS`, `SECRET_KEY`, `DATABASES`, and `REDIS`, NetBox fails at startup.

**Fix:** Do not set `NETBOX_CONFIGURATION` to a frisian-mcp config file. Pass `FRISIAN_MCP_*` settings as environment variables in `docker-compose.override.yml` instead:

```yaml
services:
  netbox:
    environment:
      FRISIAN_MCP_HMAC_KEY: "your-secret"
      FRISIAN_MCP_OAUTH_ISSUER: "https://your-netbox.example.com"
```

The plugin wrapper reads `os.environ` first and applies any `FRISIAN_MCP_*` variable it finds.

---

### Plugin wrapper missing — `ImportError: No module named 'frisian_mcp_netbox'`

NetBox does not use Django's standard `INSTALLED_APPS` for third-party apps. `frisian_mcp` listed directly in `configuration.py` is ignored. The plugin wrapper (`frisian_mcp_netbox`) is required.

**Fix:** Install the plugin wrapper from the repository:

```bash
pip install ./development/plugin_wrapper/
```

Then add it to `PLUGINS` in `configuration.py`:

```python
PLUGINS = ["frisian_mcp_netbox"]
```

---

### `FRISIAN_MCP_*` settings have no effect

NetBox's `settings.py` only reads known NetBox configuration keys. Settings placed directly in `configuration.py` without propagation are silently ignored by the MCP package.

**Cause:** The plugin wrapper is responsible for copying `FRISIAN_MCP_*` settings from `configuration.py` into `django.conf.settings`. If the wrapper is not installed or not listed in `PLUGINS`, settings propagation does not happen.

**Fix:** Verify the plugin wrapper is in `PLUGINS` and that the wrapper's `AppConfig.ready()` is executing. Confirm settings are active at runtime:

```bash
docker exec <netbox-container> bash -c "
  source /opt/netbox/venv/bin/activate
  cd /opt/netbox/netbox
  DJANGO_SETTINGS_MODULE=netbox.settings python -c \"
import django; django.setup()
from django.conf import settings
print(getattr(settings, 'FRISIAN_MCP_HMAC_KEY', 'NOT SET'))
\"
"
```

---

### Startup warnings: `FRISIAN_MCP_HMAC_KEY is not set` even though it is set

**Cause:** `frisian_mcp.contrib.oauth` and `frisian_mcp.contrib.tokens` call their `ready()` methods before `frisian_mcp_netbox.ready()` runs (Django's `INSTALLED_APPS` ordering puts them first). The warnings fire before the plugin wrapper has propagated the settings.

This is a startup ordering artifact only. The settings are available at request time and the warnings do not affect runtime behaviour. HMAC signing uses the configured key correctly once requests start.

**Suppress in dev:** Set `DEBUG = True` in your configuration — the warnings check `not getattr(settings, "DEBUG", False)` and are silent in debug mode.

---

### `registered 0 tools` at startup

**Cause A:** The plugin wrapper is not installed. Without it, `frisian_mcp` is not in `INSTALLED_APPS` and auto-discovery never runs.

**Cause B:** `FRISIAN_MCP_AUTODISCOVER` was explicitly set to `False`.

**Cause C:** The URL registration step inside the wrapper did not run. Check that `frisian_mcp_netbox` appears in `PLUGINS` **after** all other plugins that register URLs, so frisian-mcp can see the full URL tree.

**Diagnostic:** Send a `ping` to the MCP endpoint to trigger discovery:

```bash
curl -s -X POST https://your-netbox.example.com/api/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
```

If the response is `{"jsonrpc":"2.0","id":1,"result":{}}` but `tools/list` returns an empty array, check auto-discovery settings.

---

### Django admin raises `AttributeError: 'User' object has no attribute 'is_staff'`

NetBox removed the standard Django `is_staff` attribute from its User model. The Django admin interface requires `is_staff` for login.

**Fix:** The plugin wrapper patches the User model at startup to add a compatibility property. If the error persists, verify you are using the plugin wrapper version that includes the User model patch. Accessing `/admin/login/` directly (not via NetBox's custom login page) is required for the frisian-mcp OAuth admin interface.

---

## Authentication

### `Invalid token` or `Invalid v1 token` on API requests

NetBox 4.x uses a v2 token system. Tokens have the format `nbt_<key>.<plaintext>` and require the `Bearer` scheme:

```http
Authorization: Bearer nbt_<key>.<plaintext>
```

Using a raw hex key without the `nbt_` prefix, or using `Authorization: Token <hex-only-key>`, returns `Invalid v1 token`.

The full token value displayed in the NetBox UI (Profile → API Tokens) is the complete string to use after `Bearer`.

**Creating a v1 dev token (predictable key, no pepper/HMAC):** v1 tokens bypass the v2 signing system and use `Authorization: Token <key>` with a plain hex key. Useful for scripted testing:

```python
# Django shell: python manage.py shell
from django.contrib.auth import get_user_model
from users.models import Token
User = get_user_model()
user = User.objects.get(username="admin")
t = Token(user=user, version=1)
t.token = "0123456789abcdef0123456789abcdef01234567"
t.save()
# Use: Authorization: Token 0123456789abcdef0123456789abcdef01234567
```

The `docker-entrypoint.frisian-mcp.sh` script runs this automatically on first container start.

---

### `401 Unauthorized` on all tool calls

**Cause A:** No authentication class is configured and `frisian_MCP_UNAUTHENTICATED_TIER` is not set or is set to `"none"`.

**Cause B:** The Bearer token in the request does not match any token in the database or any static API key in settings.

**Fix:**

1. Check `FRISIAN_MCP_UNAUTHENTICATED_TIER` — set to `"read"` to allow unauthenticated read access.
2. Verify your token value. Static API keys are set in `FRISIAN_MCP_API_KEYS` in your settings file.
3. Check the Django admin under **frisian MCP → Tokens** to confirm the token exists and is active.

---

### OAuth discovery fails — client cannot complete authorization

**Symptom:** Claude.ai or ChatGPT initiates the OAuth flow but cannot complete it or receives an error during the authorization step.

**Cause A:** `FRISIAN_MCP_OAUTH_ISSUER` is not set. Without the issuer URL, the `/.well-known/oauth-authorization-server` metadata endpoint returns an incorrect base URL.

**Cause B:** The `WWW-Authenticate` header returned on 401 responses does not include the `resource_metadata` parameter. Clients use this to auto-discover the authorization server.

**Diagnostic:** Fetch the well-known endpoint directly:

```bash
curl https://your-netbox.example.com/.well-known/oauth-authorization-server | python -m json.tool
```

The `issuer` field must match `FRISIAN_MCP_OAUTH_ISSUER` exactly (protocol + domain, no trailing slash).

**Fix:** Set `FRISIAN_MCP_OAUTH_ISSUER` in your settings:

```python
FRISIAN_MCP_OAUTH_ISSUER = "https://your-netbox.example.com"
```

---

### Anthropic MCP client drops Bearer token intermittently

**Symptom:** MCP connection succeeds, dispatcher help calls work, but resource operations (`list`, `retrieve`, `create`) return `403 You do not have permission`.

**Cause:** This is an intermittent bug in Anthropic's MCP client where the `Authorization: Bearer <token>` header is not forwarded on `tools/call` requests after the initial session establishment. The server correctly returns 403 on unauthenticated requests.

**Workaround:** Re-save the MCP connector configuration in the AI client. This triggers a fresh connection that correctly attaches the token. frisian-mcp's `WWW-Authenticate` header (which includes `resource_metadata`) enables the client to re-discover and re-authenticate automatically.

**This is an Anthropic platform issue, not a NetBox or frisian-mcp bug.**

---

## Tools and discovery

### Dispatch group registers 0 members

**Symptom:** Startup log shows:

```text
frisian-mcp: WARNING: dispatch group 'circuits' has 0 matching tools
```

**Cause:** Basenames in `FRISIAN_MCP_DISPATCH_GROUPS` do not match the registered ViewSet basenames. Basenames are always `Model._meta.object_name.lower()` — the lowercase Django model class name — **not** URL slugs.

| URL slug | Correct basename |
|---|---|
| `ip-addresses` | `ipaddress` |
| `rack-groups` | `rackgroup` |
| `virtual-machines` | `virtualmachine` |
| `front-ports` | `frontport` |

The warning log includes "did you mean" suggestions from the registered basename list. Use those suggestions to correct your dispatch group configuration.

---

### `tools/list` returns the full flat tool list instead of dispatchers

**Cause:** Dispatchers are registered but auto-discovery ran before the dispatcher registration was imported. Auto-discovery registers flat tools first; if dispatchers are not yet in the registry when suppression runs, the flat tools are not suppressed.

**Fix:** Ensure your dispatcher imports execute inside `AppConfig.ready()`, which runs during Django startup, before the first request triggers tool list building.

---

### MCP endpoint returns 404

**Cause:** The path your client is using does not match `FRISIAN_MCP_PATH`.

The default path when `FRISIAN_MCP_PATH` is not set is `mcp`, making the endpoint `/mcp/`. The install docs recommend `FRISIAN_MCP_PATH = "api/mcp"` which makes it `/api/mcp/`. Whichever value is set (or defaulted), that is the only path that returns 200.

Check which path was registered at startup:

```text
[frisian-mcp] registered N tools at /mcp/
```

**Fix:** Ensure your MCP client URL and `FRISIAN_MCP_PATH` match. If using the official netbox-docker image with docker-compose env vars:

```yaml
environment:
  FRISIAN_MCP_PATH: "api/mcp"
```

This results in the endpoint `/api/mcp/`.

---

### Plugin UI nav pages 500 — `'QuerySet' object has no attribute 'restrict'`

**Cause:** `ObjectPermissionRequiredMixin.has_permission()` calls `self.queryset.restrict(user, action)` directly — this runs before `get_queryset()` is ever invoked, so overriding `get_queryset` does not help. `restrict()` is a method on NetBox's custom model manager; frisian-mcp models use plain Django managers and don't implement it.

**Symptom:** `GET /plugins/frisian-mcp/oauth-clients/` or `oauth-tokens/` returns 500 with `AttributeError: 'QuerySet' object has no attribute 'restrict'`.

**Fix:** Mix in a `has_permission` override on each plugin view that checks `is_authenticated` instead of calling `restrict()`. Access control is enforced by NetBox's standard login requirement:

```python
class _NoRestrictMixin:
    """Skip NetBox's restrict() call — frisian-mcp models use plain Django managers."""

    def has_permission(self):
        return self.request.user.is_authenticated


class OAuthClientListView(_NoRestrictMixin, generic.ObjectListView):
    queryset = OAuthClient.objects.all()
    table = tables.OAuthClientTable
    actions = ()
```

Apply the same mixin to `OAuthAccessTokenListView` and `FrisianMcpTokenListView`.

---

### OAuth token exchange never completes — clients authenticated as read-only despite OAuth client having read_write

**Symptom:** Claude.ai connects, OAuth discovery works, authorization redirect succeeds, but MCP calls return read-only results. No `POST /oauth/token/` appears in server logs. `GET /oauth/token/ 405` appears (from the browser-side probe), but the server-side exchange never arrives.

**Cause:** `FRISIAN_MCP_OAUTH_ISSUER` is set to `http://example.com` (no port). The well-known metadata advertises `token_endpoint: http://example.com/oauth/token/` on standard port 80. If the server runs on a non-standard port (e.g., `8082`), the AI client's backend cannot reach the token endpoint. The browser-side requests (discovery, authorize redirect) work because the user's browser accesses the server on the explicit port, but the AI backend's server-to-server token exchange calls the issuer URL's implied port 80.

**Fix:** Set `FRISIAN_MCP_OAUTH_ISSUER` to include the explicit port:

```yaml
environment:
  FRISIAN_MCP_OAUTH_ISSUER: "http://ibrokeprod.com:8082"
```

The `token_endpoint` in `/.well-known/oauth-authorization-server` will then include the port, and the AI client's backend can reach it.

---

### Admin "Delete selected" raises `ValueError: Field 'id' expected a number but got ''`

**Cause:** Some host admin templates (including NetBox's) include a hidden `_selected_action` form field with an empty value. When "Delete selected" is clicked without checking any items, Django admin receives `_selected_action=['']` and calls `queryset.filter(pk__in=[''])`, which raises `ValueError` for models with integer primary keys.

**Fix:** This is fixed in the plugin wrapper's `OAuthClientAdmin` by overriding `changelist_view` to strip empty strings from `_selected_action` before passing to super:

```python
def changelist_view(self, request, extra_context=None):
    if request.method == "POST" and helpers.ACTION_CHECKBOX_NAME in request.POST:
        post = request.POST.copy()
        clean = [v for v in post.getlist(helpers.ACTION_CHECKBOX_NAME) if v.strip()]
        post.setlist(helpers.ACTION_CHECKBOX_NAME, clean)
        request.POST = post
    return super().changelist_view(request, extra_context)
```

To delete individual OAuth clients without hitting this issue, use the **Delete** button on the client's detail page (`/admin/frisian_mcp_oauth/oauthclient/<id>/delete/`) instead of the list-level action.

---

## Cross-references

See `installs/Django/frisian-mcp/testing/` for verification tests to confirm the installation is working correctly after setup.
