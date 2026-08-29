# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Django 6.0 support declared.** The package is now classified for Django 6.0 in
  addition to 5.0–5.2. No code change was required; the classifier previously stopped at
  5.2 while `django>=5.0` carried no upper bound, so the package installed onto 6.0
  without claiming to support it.

- **Opt-in token-usage reporting.** Successful dispatcher results can carry a `_usage` block
  (`schema_tokens`, `request_tokens`, `result_tokens`, `total_tokens`, `encoding`) as an
  additive sibling of `content`/`isError`, counted with the pinned tiktoken `cl100k_base`
  encoding (optional `frisian-mcp[usage]` extra; degrades to an `approx-char4` fallback, never
  500s). **Off by default** — with reporting off the response is byte-identical to before.
  Layered opt-in: global default `FRISIAN_MCP_USAGE_REPORTING` (bool) → system policy
  `FRISIAN_MCP_USAGE_REPORTING_POLICY` (`allow`/`deny`/`None`) → per-request
  `X-Frisian-MCP-Usage` header / `?usage=` query. **System `deny` is authoritative** — a
  request flag can never re-enable it. A `frisian_mcp.W014` startup check warns when the policy
  is set to an unhonored value. No persistence, ledger, or billing. See the
  [Token Usage Reporting](docs/v1.1/Guide/token-usage-reporting.md) guide.
- **Model-visible usage line** (`FRISIAN_MCP_USAGE_IN_CONTENT`, bool, default `False`; per-request
  `X-Frisian-MCP-Usage-Content` header / `?usage_content=` query). When enabled alongside
  reporting, the same usage numbers are appended as a **second `content` item** (labeled JSON,
  `_usage: {…}`) so the agent can read and self-report its own cost — `content[0]` is never
  modified and `result_tokens` still measures `content[0]` only. Subordinate to the master gate:
  it has no `allow`/`deny` of its own and system `deny` suppresses both surfaces. The caller-side
  sibling remains the default. (A future MCP `_meta` emission is a small additive follow-up once
  `_meta` client-forwarding is dependable — no contract change.)

### Changed

- **`FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD` now ships a default (`25000` bytes) instead of
  `None` (behavior change on upgrade).** The auto-negotiate backstop wraps any successful
  non-write response whose serialized JSON exceeds the threshold in a probe envelope, so the
  caller negotiates how much to retrieve rather than receiving a context-blowing payload.
  (Writes return their lean confirmation envelope before the backstop; `@mcp_heavy` tools use
  their own probe path.) Previously the
  package default was `None`, leaving the backstop **dormant** until an operator explicitly
  set the value — so high-cardinality list actions (e.g. a full device list) returned their
  entire payload by default. It now defaults to `25000` bytes (~25 KB, ≈6k `cl100k_base`
  tokens): above a normal small filtered read, well below a large list page.

  **Upgrade impact:** every host (NetBox, Nautobot, paperless, …) gains probe-first behavior
  for large responses on upgrade — no config change required. To preserve the old always-full
  behavior, set `FRISIAN_MCP_AUTO_NEGOTIATE_THRESHOLD = None` (fully disables the backstop);
  raise the value to probe less often, or lower it to probe sooner. `@mcp_heavy` remains the
  preferred explicit mechanism for known-heavy tools.

### Fixed

- **Issue #74 — responses no longer advertise `http://localhost`.** Absolute URLs built by a
  host serializer (hyperlinked fields, pagination `next`/`previous`, `Location` headers) now
  carry the caller's real origin, port included, instead of a hardcoded `localhost`. On hosts
  whose `ALLOWED_HOSTS` is strict this was a hard failure — every ViewSet-backed tool call
  raised `DisallowedHost` — and elsewhere it silently produced links an agent could not
  follow. The same fix forwards the caller's **scheme**, so an HTTPS deployment no longer
  emits protocol-downgraded `http://` URLs. The two are inseparable: forwarding the scheme
  without also deriving the port from it yields `https://host:80/`.

  The Host is taken from `request.get_host()`, which enforces `ALLOWED_HOSTS` and honours
  `USE_X_FORWARDED_HOST`. Note this validates only where `ALLOWED_HOSTS` is a real list — a
  host configured with `["*"]` accepts any `Host` header by definition, and no package-level
  change can alter that.

- **Issue #74 — write actions are no longer advertised with an empty schema.** Schema
  derivation failed for ViewSets whose `get_serializer_class()` reads the request — for
  example `request.query_params` or `request.version` — because discovery supplied a
  hand-built stand-in carrying only four attributes. The action was still offered, with no
  field information, so an agent had to guess the payload; calls succeeded because an empty
  schema imposes no validation, which is why nothing failed visibly. Discovery now builds a
  real DRF `Request`, supplying the whole attribute surface at once.

- **Issue #74 — the discovery request resolves its API version from the host.** Following the
  fix above, `version` and `versioning_scheme` are now determined from the host's own
  versioning class rather than being hardcoded, so a ViewSet that branches on
  `request.version` derives the same serializer discovery would see at dispatch. Previously
  the value was fixed at `None`, which any host dereferencing it unguarded (e.g.
  `int(request.version)`) turned into a failed derivation. Hosts with no versioning
  configured still resolve to `None`, matching a dispatched request. Authentication,
  permission and throttle checks are deliberately **not** run during discovery.

- **Issue #71 — permission-aware group descriptions.** Group dispatcher descriptions
  in `tools/list` now count the same tier- and permission-filtered actions and
  resolved resources returned by `action="help"`, including on scoped routes.

- **Issue #12 — custom detail actions:** Auto-discovered DRF `@action(detail=True)` handlers
  now require an `id` in their MCP schema and pass it through as `pk` during invocation,
  preventing missing-`pk` tracebacks for integrations such as Nautobot `napalm`.

### Removed

- **`McpEndpointView` alias dropped.** The `McpEndpointView` name (a backward-compatible alias
  for `McpView` introduced during the rename) has been removed before the 1.0 release. Update
  any imports: `from frisian_mcp.views import McpView`.

- **`FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP` removed (breaking change).** The setting,
  which mapped a `redirect_uri` to a permission tier and applied it to the stored
  `OAuthClient.permission` at first contact under `AUTO_REGISTER`, is gone. The helper
  `frisian_mcp.contrib.oauth.views._pkce_permission_for_uri` is removed with it. The
  `redirect_uri` is no longer a tier signal on any path; the stored `OAuthClient.permission`
  is operator-set authority only.

  **Migration note:** any deployment that depended on per-redirect tier inference must now set
  `OAuthClient.permission` explicitly in the Django admin after the client is auto-registered.
  Operators upgrading without per-client tiers assigned see new clients land at the
  `FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION` default (`"read"`). The token endpoint emits
  `oauth_pkce_redirect_uri_ignored_as_tier_signal` (INFO) at code redemption when a code
  exchange would have, under the old behavior, promoted the client's tier — operators can
  use the signal to spot any caller relying on the removed promotion.

### Security

- **Authorize-path hardening: request inputs are never authority.** The unknown-client
  variant of the OAuth authorize endpoint (`AUTO_REGISTER`) is, by design, a walk-up surface
  that accepts an unauthenticated browser. Three coordinated changes restore the invariant
  that anything an unauthenticated request puts on the wire describes *what the caller
  wants*, never *what the caller is permitted to do*. Full design rationale in
  [ADR-009](docs/ADR/adr-009-pkce-authorize-path-request-inputs-not-authority.md).

  - **`FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST` (new setting; default `[]`).**
    `frisian_mcp/contrib/oauth/_redirect_uri_allowlist.py`,
    `frisian_mcp/contrib/oauth/views.py`. Gates the unknown-client branch on a declared
    host allowlist. Pattern syntax: exact hosts, leading-`*.` wildcard with label-boundary
    semantics, and reverse-DNS native-app schemes (RFC 8252). Patterns and hosts are
    IDNA-normalized before comparison; Cyrillic look-alikes cannot bypass an ASCII entry.
    Empty allowlist is fail-closed — `AUTO_REGISTER=True` with `[]` behaves exactly as
    `AUTO_REGISTER=False`. Rejection returns `error=invalid_client` (not
    `invalid_redirect_uri`) so the response shape does not advertise which check rejected.
    Loopback hosts still require an explicit allowlist entry under `AUTO_REGISTER`; the
    scheme-level loopback safety check does not, and must not, satisfy the unknown-client
    gate. Importable log-event constants:
    `OAUTH_PKCE_AUTO_REGISTER_HOST_REJECTED`,
    `OAUTH_PKCE_AUTO_REGISTER_ALLOWLIST_EMPTY`.

  - **`PKCE_REDIRECT_TIER_MAP` removed; `redirect_uri` is no longer a tier signal.**
    `frisian_mcp/contrib/oauth/views.py`. See the Removed section above for the migration
    note. The companion log `oauth_pkce_redirect_uri_ignored_as_tier_signal` (INFO) fires
    at **code redemption** (in `TokenView._handle_authorization_code`), not at the
    authorize step — operators searching for the signal look at the token exchange in
    the request lifecycle.

  - **Token authority is fixed at issuance.**
    `frisian_mcp/contrib/oauth/authentication.py`. `OAuthAccessToken.permission` is
    snapshotted when the token is issued and is the ceiling for that token's lifetime.
    The authenticator returns `min(token.permission, client.permission)` over the tier
    ordering `read < read_write < admin`. An operator admin-console *downgrade* of the
    issuing client narrows every outstanding token live; an admin-console *upgrade* does
    NOT widen previously-issued tokens. The authenticator emits
    `oauth_token_authority_narrower_than_client_tier` (INFO, throttled per
    `(token_pk, observed_client_tier)`) when the snapshot is strictly narrower than the
    live client tier — surfacing either a legitimate downgrade or a propagated-but-blocked
    escalation attempt for downstream correlation.

  - **`FRISIAN_MCP_OAUTH_AUTO_APPROVE` semantics reframed; DEBUG default removed.**
    `frisian_mcp/contrib/oauth/views.py`,
    `frisian_mcp/contrib/oauth/_consent_gate.py`,
    `frisian_mcp/contrib/oauth/models.py`,
    `frisian_mcp/contrib/oauth/migrations/0003_oauthauthorizeconsent.py`,
    `frisian_mcp/contrib/oauth/admin.py`.
    `FRISIAN_MCP_OAUTH_AUTO_APPROVE` no longer skips the consent form; it now means
    "remember consent." A first-time consent gate renders the consent form for new
    `(user, client_id, redirect_uri, scope)` tuples even when `AUTO_APPROVE=True`. The
    DEBUG default (`bool(DEBUG)`) is removed — default is now `False` unconditionally.
    Operators with M2M flows must pre-populate `OAuthAuthorizeConsent` records via admin
    to preserve silent code issuance. New model + admin surface: `OAuthAuthorizeConsent`
    (FK user, char `client_id` / `redirect_uri` / `scope`, unique-together,
    admin-registered with `revoke_selected_consents` bulk action). CSRF is enforced by
    Django's existing `CsrfViewMiddleware`; `AuthorizeView` is not `csrf_exempt`'d and
    the existing consent template carries `{% csrf_token %}`. Importable log-event
    constants from `frisian_mcp.contrib.oauth._consent_gate`:
    `OAUTH_AUTHORIZE_CONSENT_REQUIRED` (INFO, `reason ∈ {"just_auto_registered", "no_prior_consent"}`),
    `OAUTH_AUTHORIZE_CONSENT_DENIED` (WARNING),
    `OAUTH_AUTHORIZE_AUTO_APPROVED_ON_PRIOR_CONSENT` (INFO, throttled per tuple per
    process).

  - **BREAKING — `OAuthServicePrincipal.has_perm` and `has_module_perms` now default-deny.**
    `frisian_mcp/contrib/oauth/authentication.py`. Hosts that relied on the pre-T10
    blanket-True semantics at `read_write` / `admin` tiers must populate
    `FRISIAN_MCP_OAUTH_TIER_PERMISSIONS` with the Django permission strings they want
    each tier to grant. Inheritance is monotonic; higher tiers accumulate the allowlists
    of all lower tiers. An empty mapping or an unknown perm string returns `False`. The
    MCP layer's own tier filter is unchanged — this only narrows `request.user.has_perm`
    calls that originate in host code outside MCP. Example:

    ```python
    FRISIAN_MCP_OAUTH_TIER_PERMISSIONS = {
        "read":       ["dcim.view_device"],
        "read_write": ["dcim.change_device"],
        "admin":      ["dcim.delete_device"],
    }
    ```

    Results: a `read` principal accumulates `{dcim.view_device}`; `read_write`
    accumulates `{dcim.view_device, dcim.change_device}`; `admin` accumulates all three.

  - **PKCE authorization-code single-use is now atomic.**
    `frisian_mcp/contrib/oauth/views.py`. The previous `cache.get` →
    cheap-shape-checks → `cache.delete` sequence had a race window under concurrent
    exchanges of the same code. The token endpoint now gates code consumption on
    `cache.add()` against the cache-key family
    `frisian_mcp:oauth_code_consumed:` (importable as
    `_AUTH_CODE_CONSUMED_PREFIX`). Concurrent or replayed exchanges of the same code
    return `invalid_grant` and log `oauth_authorization_code_replay_detected` at
    `WARNING`. The primitive is backend-agnostic across Django's `BaseCache`
    (`LocMemCache`, `RedisCache`, `MemcachedCache`, `DatabaseCache`); `DummyCache`
    makes the gate silently inert and is not supported for production OAuth
    deployments. No new setting; no operator action beyond a real cache backend.

  **Operator action:** existing `OAuthClient`, `OAuthAccessToken`, and pre-T9
  authorization grants survive the upgrade without re-issue. Operators flipping
  `AUTO_REGISTER=True` must populate
  `FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST` before walk-up registration
  works again. Operators expecting an admin-console tier upgrade to widen an outstanding
  token must reissue the token after the upgrade. Operators with M2M OAuth flows must
  pre-populate `OAuthAuthorizeConsent` rows via the Django admin before the first
  scheduled exchange or the first request will block on the consent form. Hosts that
  call `request.user.has_perm(...)` outside the MCP layer for OAuth-authenticated
  requests must populate `FRISIAN_MCP_OAUTH_TIER_PERMISSIONS` or the calls will return
  `False`. The `mcp_doctor --security` extended audit covers every new setting and
  every operator-misconfiguration combination in this entry.

- **AUTH-4 — Token hashing:** `FrisianMcpToken.token` and `OAuthClient.client_secret` are
  now stored as HMAC-SHA256 keyed by `SECRET_KEY` instead of plaintext.  The raw value is
  shown exactly once at creation time via the `_raw_token` / `_raw_client_secret` instance
  attribute (available immediately after `save()`, absent after a DB reload).

  **Migration note:** Existing tokens and OAuth clients created before this release are
  automatically invalidated — the stored plaintext no longer matches any HMAC lookup.
  Regenerate all `FrisianMcpToken` and `OAuthClient` records after deploying this version.
  No data migration is provided because the original plaintext values cannot be recovered
  from the database.

  Rotating `SECRET_KEY` also invalidates all tokens; this is intentional and desirable.

  `OAuthAccessToken.token` follows the same pattern — stored as HMAC-SHA256 of the raw
  Bearer value, never plaintext.  The raw token is exposed exactly once via
  ``plaintext_token`` on the freshly-saved instance (see ``TokenView``).  Existing access
  tokens are also invalidated on upgrade and will need to be re-issued.
