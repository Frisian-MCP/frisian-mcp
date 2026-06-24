# Remediation Backlog — frisian-mcp Code Review

**Source:** Darth Claude project `464598c0-ab56-412b-92e4-bf9c12884648` ("frisian-mcp Code Review — Remediation Backlog"), 28 ready tasks created 2026-06-24 01:54–01:57 UTC by Claude.ai code-review pass.

**Status:** Local working copy for triage. To be filed individually on GitHub.

**Severity legend:** `[H]` High · `[M]` Medium · `[L]` Low. Priority numbers are DC task priorities (lower = more urgent).

---

## High severity (8)

### [H] OAuth PKCE auto-register accepts any redirect_uri host (open code issuer)

- **Task:** `277cbdbd-9ac4-4079-95cf-782275d929c7` · priority 10 · role: security
- **File:** `src/frisian_mcp/contrib/oauth/views.py:1046-1063` (`_validate_authorize_params`) and `_redirect_uri_is_safe`

With `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True`, `_validate_authorize_params` returns valid for any unknown `client_id` as long as `_redirect_uri_is_safe` passes — and that helper only checks the URI scheme (https/loopback/custom), never the host. An attacker can drive the authorize endpoint with `redirect_uri=https://attacker.example/cb`, receive an authorization code, and (holding their own PKCE verifier) exchange it. PKCE provides no protection because the attacker generated it. This is effectively an open authorization-code issuer to any HTTPS target when auto-register is enabled.

**Impact:** High (only when `PKCE_AUTO_REGISTER` is on).

**Fix:** Even with auto-register, require the `redirect_uri` host to match a configured allowlist of trusted host patterns, or bind first-seen `client_id` ↔ `redirect_uri` and reject mismatches on subsequent use.

---

### [H] OAuth redirect_uri-derived tier auto-promotes stored client permission

- **Task:** `a567c637-180f-4a58-af1a-f124c483ef04` · priority 10 · role: security
- **File:** `src/frisian_mcp/contrib/oauth/views.py:443-466` (`_handle_authorization_code`)

`effective_permission` is derived purely from the attacker-controlled `redirect_uri` prefix via `_pkce_permission_for_uri()`. When it outranks the stored client permission, the code mutates and saves `client.permission` to the higher tier (lines 460-462). Combined with the auto-register open-redirect issue, a client can present a `redirect_uri` whose prefix matches a high-tier entry in `FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP` and have its stored permission permanently escalated (up to admin).

**Impact:** High — privilege escalation driven by request data.

**Fix:** Never auto-promote a stored client's permission from request-derived input. Tier must be operator-assigned via admin; `redirect_uri` → tier mapping must not be a privilege-granting input. At minimum, gate promotion behind an explicit operator setting and never above the originally registered tier.

---

### [H] OAuth token tier read live from client propagates escalation to issued tokens

- **Task:** `d86d1267-19f6-4c52-acff-691a3a27280c` · priority 15 · role: security
- **File:** `src/frisian_mcp/contrib/oauth/authentication.py:182-184`

The authenticator reads `access_token.client.permission` rather than the token's own snapshot (`access_token.permission`). A token minted as `read` is silently upgraded to `read_write`/`admin` the moment the issuing client is promoted — including via the `redirect_uri`-driven auto-promotion path. This breaks the principle that a token's authority is fixed at issuance.

**Impact:** High when combined with the client auto-promotion finding; Medium standalone.

**Fix:** Enforce effective tier as `min(token snapshot, client)` so a client change can revoke/narrow a token but never silently widen it. The existing comment about propagating admin-console changes immediately should only apply to downgrades.

---

### [H] JSON-RPC: tools/call without id silently dropped (never executed)

- **Task:** `8b9ce637-4d44-4155-b2a6-263fffc72f4e` · priority 15 · role: python-development
- **File:** `src/frisian_mcp/views.py:1520-1526` (`_parse_and_dispatch`)

The code treats ANY message lacking an `id` key as a JSON-RPC notification and returns HTTP 202 with no dispatch — including request-shaped methods like `tools/call` and `tools/list`. A client that omits `id` on a `tools/call` gets a silent success (202) with no result and no error, and the tool never runs. This is a hard-to-debug foot-gun.

**Impact:** High — silent failure of real tool invocations.

**Fix:** Only treat known notification methods (`notifications/initialized`, etc.) as notifications. For request-shaped methods sent without an `id`, either dispatch (and discard the response) or return `INVALID_REQUEST` rather than silently accepting.

---

### [H] partial_update (PATCH) schema does not require id

- **Task:** `d46b112f-7b27-43f8-aad7-a8fbc32f153c` · priority 15 · role: python-development
- **File:** `src/frisian_mcp/backends/discovery.py:245-246` (`get_input_schema`)

`id` is added to `schema['required']` for every detail action EXCEPT `partial_update`: `if action != 'partial_update': schema['required'] = ['id']`. So a caller can invoke `<resource>.partial_update` with no target id. In `_split_arguments` (`invocation.py`) no pk lands in `view_kwargs`, and DRF `get_object()` then raises — or, on a permissive custom viewset, may operate on an unintended object. Making body fields optional for PATCH is correct, but the target id must stay required.

**Impact:** High — PATCH without a target.

**Fix:** Keep `id` required for `partial_update`. The body-field required-merge is already skipped separately at `discovery.py:264`, so only that body-field skip is needed — the id requirement should not be coupled to it.

---

### [H] _ignore_model_permissions disables Django model/object perms for all MCP callers

- **Task:** `fb3f83c8-0f41-4299-ad50-7e5142eb977a` · priority 15 · role: security
- **File:** `src/frisian_mcp/backends/invocation.py:503`

`viewset._ignore_model_permissions = True` disables `DjangoModelPermissions`/`DjangoObjectPermissions.has_permission()` for the synthetic request. For a host app whose access control relies on Django model/object permissions (rather than `get_queryset()` scoping), this grants EVERY MCP caller create/update/delete regardless of those permissions. The MCP tier gate only distinguishes read/read_write/admin globally and does not encode per-model Django permissions on the invocation path. The inline comment also claims writes are protected by "post-save validation hooks that roll back creates" — no such rollback mechanism exists in this library.

**Impact:** High for hosts relying on DRF model/object permissions.

**Fix:** Make `_ignore_model_permissions` opt-in via a setting (default off), and correct the docstring/comment which describes a rollback that does not exist.

---

### [H] Bulk list-body path skips ALL jsonschema validation, not just required-field checks

- **Task:** `94c78bdc-7cbd-4c1c-b1a9-c12dffa5d9a7` · priority 15 · role: python-development
- **File:** `src/frisian_mcp/registry.py:599-633`

When `_is_list_body` is true, `jsonschema.validate` is skipped entirely (`if not is_dispatcher_help and not _is_list_body:`), so type checks, item-shape checks, and `additionalProperties` are all bypassed — not just required-field validation as the comment claims. A caller can send `{'objects': [...]}` (or any key in `_BULK_LIST_BODY_KEYS`: `objects`/`data`/`items`/`_items`/`body`) with arbitrary garbage list items past the gateway's schema layer for ANY tool whose single arg key happens to match — even a read tool with an `items` argument that never intended bulk semantics.

**Impact:** High — validation bypass.

**Fix:** Restrict the bypass to tools that actually declare bulk support (e.g. `is_write` plus an explicit bulk flag), and validate each list element against an item schema rather than skipping validation wholesale.

---

### [H] mcp_config --token leaks raw Bearer secret to argv and stdout

- **Task:** `7a37dd95-22a3-445f-bd76-e8abe6c06a54` · priority 20 · role: security
- **File:** `src/frisian_mcp/management/commands/mcp_config.py:80-116`

The `--token` value is taken from the command line (visible in shell history, `ps`, `/proc/<pid>/cmdline`, process listings) and embedded verbatim into the JSON written to stdout (`'Authorization': 'Bearer <token>'`), which commonly lands in CI logs, terminal scrollback, and screen-shares. This is a raw, directly-usable Bearer credential leaked to argv and stdout/logs.

**Impact:** High — credential disclosure.

**Fix:** Accept the token via an environment variable or an interactive `getpass` prompt instead of a `--token` argv flag; and/or emit a `Bearer <YOUR_TOKEN>` placeholder by default with the real value only written on explicit opt-in (and to stderr with a redaction notice).

---

## Medium severity (10)

### [M] Resource URI-template vars allow .. traversal, passed raw to handlers

- **Task:** `65d82266-d508-4b92-8b05-8853d9d0c360` · priority 40 · role: security
- **File:** `src/frisian_mcp/resources.py:30-46` (`_match_uri_template`) and 186-192

Placeholders compile to `(?P<var>[^/]+)` — this blocks `/` but not `.` or `..`, and the registry performs no normalization before dispatch. A handler doing filesystem/SQL lookup on the raw extracted variable is exposed to path traversal. Additionally, template iteration returns the first dict-order match, so overlapping templates resolve non-deterministically by registration order rather than specificity.

**Impact:** Medium — info disclosure / traversal in handler code.

**Fix:** Constrain the placeholder character class to a safe set (e.g. exclude `.`) or validate/normalize extracted variables before dispatch; resolve template ambiguity by specificity; and document that handlers must treat extracted variables as untrusted.

---

### [M] OAuth authorization-code single-use is non-atomic (replayable under concurrency)

- **Task:** `5666a8e9-770f-494b-9f33-f61595c7e832` · priority 40 · role: security
- **File:** `src/frisian_mcp/contrib/oauth/views.py:406-435` (token exchange)

Single-use relies on cache `get()` then `delete()`, which is not atomic. Two concurrent exchanges of the same code can both pass `cache.get()` before either `cache.delete()` runs, yielding two access tokens from one authorization code (code replay under concurrency).

**Impact:** Medium.

**Fix:** Use an atomic cache operation (e.g. Redis `GETDEL`), or move authorization codes to the DB with `select_for_update()` plus a single-use/consumed flag so the read-and-consume is atomic.

---

### [M] Forwarded headers trusted for OAuth issuer/metadata and rate-limit key

- **Task:** `88f63917-0c6c-48c0-be22-ae1f9b6d3b1f` · priority 45 · role: security
- **Files:** `src/frisian_mcp/contrib/oauth/views.py:231-247` (`_get_base_url`), `contrib/oauth/authentication.py:222-226`, `contrib/middleware.py:160-166` (`_resolve_key`)

With trusted-proxy config (or a direct-to-app attacker), `X-Forwarded-Host`/`-Proto` are used to build the issuer/base URL in discovery metadata and the `WWW-Authenticate resource_metadata` header — a spoofed host poisons the advertised `authorization_endpoint`/`token_endpoint` and can redirect discovery-first clients to an attacker-controlled server. Separately, an attacker who can reach the app and inject `X-Forwarded-For` with ≥ `proxy_count` entries chooses their own rate-limit key, evading per-IP limits.

**Impact:** Medium.

**Fix:** Prefer an explicit `FRISIAN_MCP_OAUTH_ISSUER` for metadata; validate forwarded host against `ALLOWED_HOSTS`; and only honor XFF for rate-limit keying when `REMOTE_ADDR` is the configured trusted proxy.

---

### [M] DRF throttles run against synthetic request, rendering per-client throttles ineffective

- **Task:** `98b83c16-00d1-4134-a627-4159941b1833` · priority 45 · role: python-development
- **File:** `src/frisian_mcp/backends/invocation.py:443-505`

The synthetic `HttpRequest` is built with path `/`, `SERVER_NAME='localhost'`, and no `REMOTE_ADDR`/forwarded headers. DRF throttle classes key on `request.META['REMOTE_ADDR']` and the view path via `get_ident()`. Every MCP call therefore shares the same empty/None ident and path `/`, so per-client and scoped throttles collapse all MCP traffic into one bucket (or a `None`-keyed cache). The docstring claims throttles "fire before the action", but they fire ineffectively.

**Impact:** Medium — throttle bypass / ineffective rate limiting on the host path.

**Fix:** Copy `REMOTE_ADDR` (and relevant `X-Forwarded-For`) from the original request into the synthetic request META, and set a representative path so throttle idents distinguish callers.

---

### [M] Anonymous MCP caller silently substituted with SERVICE_ACCOUNT_USER

- **Task:** `efabd68b-709d-421e-8349-25f50306dd8a` · priority 45 · role: security
- **File:** `src/frisian_mcp/backends/invocation.py:678-690` (`_resolve_effective_user`)

When `request.user` is not authenticated and `FRISIAN_MCP_SERVICE_ACCOUNT_USER` is set, the synthetic request silently runs as that service account. Combined with `_ignore_model_permissions=True`, an anonymous MCP caller executes host ViewSets as a real Django user with that user's object-level permissions. The default behavior (any anonymous request gets elevated) is surprising and easy to misconfigure.

**Impact:** Medium.

**Fix:** Gate the substitution behind the tier system explicitly, and ensure the substituted identity cannot exceed the tier the anonymous caller was granted. Document the elevation risk prominently. (Also note `_resolve_effective_user` is invoked twice per call — `invocation.py:458` and `:650` — resolve once and reuse to avoid divergent identities.)

---

### [M] Non-APIException errors return str(exc) to MCP client (internal info disclosure)

- **Task:** `d9b0a0cb-47c0-4b33-a101-832ef263c153` · priority 45 · role: python-development
- **File:** `src/frisian_mcp/backends/invocation.py:533-539` and `_exception_envelope_message` at 207-208

The generic `except Exception` around the action call returns `ToolResult(content={'error': _exception_envelope_message(exc)})`. For non-`APIException` exceptions, `_exception_envelope_message` falls back to `str(exc)`, which can leak internal details (an `IntegrityError` exposing column names/constraints, a `KeyError`, or a filesystem path) to the caller. DRF's own pipeline would convert these to a generic 500 without the message.

**Impact:** Medium — information disclosure.

**Fix:** For non-`APIException` exceptions, return a generic message (e.g. "Internal tool error") and keep the detail only in the server log (`logger.exception`).

---

### [M] Dispatcher instance shared across concurrent requests (cross-request state leak)

- **Task:** `29d9711d-7b47-4f72-b585-ad86ee6e0cab` · priority 45 · role: python-development
- **Files:** `src/frisian_mcp/backends/dispatcher.py:215` (`instance = cls()`) and `decorators.py:147-173`

The `@mcp_dispatcher` class is instantiated once at registration, and every concurrent request invokes `entry.method(instance, request, params)` on that single shared instance. The docstring even states "The class is instantiated once... request is passed per-call." Any dispatcher method that stores per-request state on `self` (e.g. `self.request = request`, caching) races across concurrent requests and can cross-contaminate request/user context between callers — a potential auth/data-leak under load.

**Impact:** Medium (thread-safety; depends on user dispatcher code).

**Fix:** Instantiate the dispatcher per-invocation, or enforce/loudly document the statelessness contract. Group dispatcher (`group_dispatcher.py`) shares the same pattern — audit both.

---

### [M] Dispatcher action enum unfiltered when max_tier=None (privileged names leak)

- **Task:** `70996e1c-6979-4e79-b326-55364b3fe252` · priority 45 · role: security
- **Files:** `src/frisian_mcp/registry.py:495-498` with `backends/dispatcher.py _visible_actions`

When `list_tools(max_tier=None)` is called (the documented cache-key / opt-out path), the dispatcher schema is rebuilt with `_visible_actions` returning ALL actions including write/admin. If any code path serves a `max_tier=None` listing to a real client, every privileged action name leaks in `inputSchema.action.enum`. Related inconsistency: `_apply_max_tier_cap` / `list_tools` default an unknown tier string to rank 2 (admin) for listing but rank 0 (read) for dispatch — an unknown tier could list admin tools while unable to invoke them.

**Impact:** Medium — info disclosure of privileged surface.

**Fix:** Treat `max_tier=None` as all-actions only for genuinely internal cache-key computation; default `_visible_actions` to `read` rank for any schema actually returned to a client. Normalize unknown tier strings to one known (most-restrictive) value consistently.

---

### [M] OAuth AUTO_APPROVE defaults True under DEBUG; with auto-register mints codes without consent

- **Task:** `5b9ca388-3996-4453-9a20-b2ba6ad7dd6d` · priority 50 · role: security
- **File:** `src/frisian_mcp/contrib/oauth/views.py:870-879` (`_auto_approve_default`) and 974-975

`_auto_approve_default()` returns `True` when `settings.DEBUG`. When auto-approve is on, the authorize endpoint issues codes with no user authentication and no consent — purely on a valid-shaped request. Combined with `PKCE_AUTO_REGISTER=True` (and the unrestricted redirect host), an unauthenticated remote party can mint authorization codes. The GET-based auto-approve path also bypasses the consent form's CSRF protection.

**Impact:** Medium (defaults are safe in production, but the DEBUG default plus auto-register is a full bypass if DEBUG leaks to prod or settings are mixed).

**Fix:** Refuse auto-approve for unregistered/auto-registered clients; document `FRISIAN_MCP_OAUTH_AUTO_APPROVE` + `PKCE_AUTO_REGISTER` as a mutually dangerous combination; reconsider tying auto-approve to `DEBUG`.

---

### [M] OAuthServicePrincipal.has_perm returns True for ALL permissions at read_write/admin

- **Task:** `50893d83-279a-40fc-81fe-2aa2a1320077` · priority 50 · role: security
- **File:** `src/frisian_mcp/contrib/oauth/authentication.py:94-104`

`has_perm(perm, obj)` ignores the `perm` argument entirely and returns `True` for ANY permission name when the tier is `read_write` or `admin`. Host apps that gate sensitive Django model operations via `request.user.has_perm('app.delete_x')` will get `True` for every permission, granting blanket Django-level authority to any non-read OAuth client. The docstring says "MCP tier filtering is the real gate", but any host relying on standard Django perms is silently over-authorized.

**Impact:** Medium.

**Fix:** Default-deny `has_perm` (return `False`) and require hosts to check `request.auth.permission` for the MCP tier, or map tiers to explicit, narrow permission sets rather than a blanket `True`.

---

## Low severity (10)

### [L] register() silently overwrites a tool with a duplicate name

- **Task:** `ec1abbc4-7880-4f3c-8d12-6922d8098627` · priority 70 · role: python-development
- **Files:** `src/frisian_mcp/registry.py:348-365` (`ToolRegistry.register`); same pattern in `resources.py` (keyed on `uri_template`)

`self._tools[name] = _ToolEntry(...)` unconditionally replaces any prior entry. Two `@mcp_tool` decorators, or an auto-discovered tool plus a hand-written one, sharing a name silently clobber each other — order-dependent and invisible. The docstring states "Unique tool name", so a collision is a config error that currently fails silently.

**Fix:** Log a warning (or raise in a strict mode) when name already exists in `self._tools` before overwriting. Apply the same to `resource_registry.register`.

---

### [L] permission_tier / API-key tier values not validated; typo downgrades to read

- **Task:** `92d3e7c7-e4a8-47d1-b71c-331605f01155` · priority 70 · role: python-development
- **Files:** `src/frisian_mcp/registry.py` (rank lookups use `_TIER_RANK.get(x, 0)`); `checks.py:140-181` (`check_api_keys_are_hashed` validates dict keys but not values)

If a tool is registered with a typo'd `permission_tier` (e.g. `write` instead of `read_write`, or `Admin` capitalized), the rank becomes 0, so the tool is treated as `read` and becomes visible/invocable by everyone — a silent privilege downgrade in the least-restrictive direction. The same applies to `FRISIAN_MCP_API_KEYS` tier VALUES, which are consumed directly as `_ApiKeyAuth(permission=tier)` and resolve via `_TIER_RANK.get(tier, 0)`.

**Fix:** Validate `permission_tier` against `_TIER_RANK` at registration time (raise or default to most-restrictive). Extend `W002` (or add a sibling check) to warn when any `FRISIAN_MCP_API_KEYS` value is not in `{read, read_write, admin}`.

---

### [L] Pagination cursor offset not bounds-checked (negative offset)

- **Task:** `513ce5d8-62bd-4d9b-a03a-170c1888ccc6` · priority 75 · role: python-development
- **File:** `src/frisian_mcp/views.py:861-871` (`_decode_cursor`), used at ~958

`_decode_cursor` decodes an attacker-controlled base64 string to an int with no sign or magnitude validation. A negative offset produces `tools[-5 : -5+page_size]`, silently returning a wrong/garbage page rather than an error.

**Fix:** Reject `offset < 0` (and optionally `offset > len(tools)`) with `INVALID_PARAMS`.

---

### [L] int(page)/int(page_size) unvalidated -> 500 instead of INVALID_PARAMS

- **Task:** `c6ca7fd8-12da-456e-b477-3faf451cb851` · priority 75 · role: python-development
- **File:** `src/frisian_mcp/views.py:316-340` (`_serve_heavy_mode` paginated branch)

`int(arguments.get('page', 1))` and `int(arguments.get('page_size', ...))` raise `ValueError`/`TypeError` on non-numeric input (e.g. `page='abc'`). This runs before the main try block in the heavy continuation path, so the exception propagates out of `_handle_tools_call` and surfaces as a generic 500 / unhandled error rather than a clean `INVALID_PARAMS`.

**Fix:** Wrap the `int()` coercions and surface `INVALID_PARAMS` (or an `isError` content payload) for non-integer paging args.

---

### [L] ExemptViewPermissionAdapter wildcard only matches bare string, not list form

- **Task:** `3e22d829-a5f2-4907-88b1-d4a9cd4d52eb` · priority 75 · role: python-development
- **File:** `src/frisian_mcp/contrib/permissions/exempt_view_adapter.py:47-48`

`if exempt in ('__all__', '*')` only matches when the setting is exactly the string `__all__` or `*`. Django apps commonly express this as a list, e.g. `EXEMPT_VIEW_PERMISSIONS = ['*']` or `['__all__']`. In that case `['*'] in ('__all__','*')` is `False`, so the code falls into the else branch and tries `str(model_label).split('.', 1)` on `*`, producing no capabilities and silently failing to synthesize the global view-exempt set. The docstring claims it supports both the `__all__` / `*` shorthand.

**Fix:** Normalize the setting to a set/list first and check membership, treating the wildcard if `*` or `__all__` appears in (or equals) the setting.

---

### [L] In-memory rate limit backend is fixed-window not sliding; counters not evicted

- **Task:** `a1d30597-7665-415d-beb0-2e5d4b5b5c05` · priority 75 · role: python-development
- **File:** `src/frisian_mcp/contrib/middleware.py:60-70`

The docstrings describe a sliding-window limiter, but the implementation resets the count to 0 once the elapsed time exceeds the window length — a fixed window. A caller can send a full allowance at the end of one window and another full allowance at the start of the next, permitting up to double the intended rate across the boundary. The counters dict also grows with each distinct key (e.g. per IP) and is never pruned, so memory grows with the number of distinct keys observed.

**Fix:** Implement a true sliding window (or correct the documentation to say fixed window), and bound or periodically prune stale counter entries.

---

### [L] FrisianMcpToken.token help_text says SECRET_KEY but code uses FRISIAN_MCP_HMAC_KEY

- **Task:** `819b72b2-64c8-46c8-b6b9-c2c7ae7f6e1c` · priority 80 · role: documentation-generation
- **File:** `src/frisian_mcp/contrib/tokens/models.py:75` (help_text) vs `_hmac_token` at line 49

The field help text says "HMAC-SHA256 of the raw Bearer token keyed by `SECRET_KEY`", but `_hmac_token` keys by `FRISIAN_MCP_HMAC_KEY` and only falls back to `SECRET_KEY`. The admin UI surfaces this help text to operators. Misleading text can cause an operator to rotate `SECRET_KEY` believing it is the HMAC key (or vice-versa), invalidating all stored tokens unexpectedly.

**Fix:** Change the help text to "keyed by `FRISIAN_MCP_HMAC_KEY` (falling back to `SECRET_KEY`)".

---

### [L] No Django system check warns when HMAC silently falls back to SECRET_KEY

- **Task:** `6a2afc09-1526-4dd0-a917-b8e080bce05d` · priority 80 · role: python-development
- **Files:** `src/frisian_mcp/contrib/tokens/models.py:49` (`_hmac_token` fallback); `checks.py` (absent)

`_hmac_token` silently falls back to `SECRET_KEY` when `FRISIAN_MCP_HMAC_KEY` is unset. `mcp_doctor` warns about this (`_check_hmac_key_rotation`), but there is no Django system check, so it is invisible to `manage.py check` / CI unless an operator manually runs the doctor. (The fallback itself is a reasonable default — this is only about discoverability.)

**Fix:** Add an informational `frisian_mcp.W00x` system check in `checks.py` mirroring the doctor's HMAC-key-rotation warning.

---

### [L] SSE single-message wrapping never fires for spec-compliant clients

- **Task:** `f41a82f3-ae08-494c-8265-f5d57c32a6ac` · priority 80 · role: python-development
- **File:** `src/frisian_mcp/views.py:780-781` (`_maybe_sse`)

`if 'application/json' in accept: return response` returns the plain `JsonResponse` whenever `application/json` is present in the `Accept` header. MCP Streamable HTTP clients are required to send `Accept: application/json, text/event-stream` on POST, so this guard makes the SSE-wrapping branch effectively dead for every spec-compliant client, contradicting the docstring that responses stream "when the caller accepts it".

**Fix:** Decide the contract explicitly — either drop the `application/json` short-circuit, or rename/redocument the helper to reflect that it only fires for `text/event-stream`-only callers.

---

### [L] {data:{...}} write-wrapper guard only catches _BULK_LIST_BODY_KEYS

- **Task:** `93f7add0-e5d9-4662-bafc-841ca9787346` · priority 80 · role: python-development
- **File:** `src/frisian_mcp/registry.py:609-627`

The comment says agents often send `{data: {field: value}}` and the guard exists to reject that mistake. But the condition requires `_wrap_key in _BULK_LIST_BODY_KEYS` (`{objects, data, items, _items, body}`). A wrapper like `{payload: {...}}` or `{attributes: {...}}` passes straight through to validation/serializer and silently produces empty records — exactly the failure mode the block is meant to prevent. The logic also conflates the wrapper concern with the bulk-key set.

**Fix:** Detect the wrapper case generically as "a single dict-valued arg whose key is not a declared property of the tool schema", independent of `_BULK_LIST_BODY_KEYS`.

---

## Summary by area

| Area | High | Medium | Low | Total |
|---|---:|---:|---:|---:|
| OAuth / auth | 4 | 4 | 0 | 8 |
| Permission / tier | 1 | 2 | 1 | 4 |
| Validation / schema | 2 | 1 | 4 | 7 |
| JSON-RPC / SSE | 1 | 0 | 1 | 2 |
| Throttle / rate-limit | 0 | 1 | 1 | 2 |
| Error handling / disclosure | 0 | 1 | 0 | 1 |
| Concurrency / state | 0 | 1 | 0 | 1 |
| Resources / templates | 0 | 1 | 0 | 1 |
| Tooling / config | 1 | 0 | 3 | 4 |
| **Total** | **8** | **10** | **10** | **28** |
