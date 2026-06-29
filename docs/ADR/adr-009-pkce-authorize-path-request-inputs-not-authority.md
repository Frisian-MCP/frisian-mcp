# ADR-009: Unauthenticated PKCE authorize-path: request inputs are never authority

**Status:** Proposed
**Date:** 2026-06-29
**Category:** adr
**Supersedes:** —
**Related:** ADR-003 (URL Auto-Registration), ADR-008 (Permission-Aware Tool Discovery)

---

## 1. Context

The OAuth 2.0 authorization-code + PKCE flow accepts an `authorize` request *before* any caller has authenticated against the server. The unknown-client variant of that path (`FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True`) is a deliberately walk-up surface: a brand-new MCP connector points its browser at `/oauth/authorize`, the server lazily registers the client on first sight, and the operator sees a consent form. That ergonomic posture is what makes "point a connector at the URL and go" possible.

The walk-up posture is only safe under one invariant: **request inputs are never authority**. Anything an unauthenticated browser puts on the wire — `redirect_uri`, requested `scope`, `client_id`, the bare presence of a code-exchange — describes *what the caller wants*, never *what the caller is permitted to do*. The server's stored state, the operator's admin actions, and a real-user consent record are the only authority signals on this path.

frisian-mcp 1.0.12's authorize-path implementation drifted from that invariant in several places at once. The drift was not a single bug; it was a pattern. The `redirect_uri` host was treated as a trust signal under `AUTO_REGISTER`. A request-supplied `redirect_uri` could promote the stored client's `permission` tier upward. The token authenticator re-read `OAuthClient.permission` on every request, so any post-issuance promotion to the stored row immediately widened every previously-issued token. The DEBUG default for `AUTO_APPROVE` skipped the consent form entirely in development, and that default leaked through any operator who flipped `DEBUG=True` to debug a deploy. The `has_perm` method on the OAuth service principal returned `True` for every permission string at `read_write` / `admin` tiers. The PKCE authorization-code single-use check was a cache `get`-then-`delete`, not an atomic compare-and-delete.

Each of those is a separate defect. They share one root cause: places where request shape, request side-effects, or convenience-default behavior were allowed to *become* server authority. This ADR captures the consolidated decisions that re-establish the invariant.

## Decision

### 2. T1 — Host allowlist gates PKCE `AUTO_REGISTER`

`FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER=True` no longer trusts the inbound `redirect_uri` host on its own. A new setting,

```python
FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST: list[str] = []
```

declares the trusted set. A request whose `redirect_uri` does not match any allowlist entry is rejected with `error=invalid_client` — never `invalid_redirect_uri`, so the response shape does not advertise which check rejected the request.

**Pattern syntax is intentionally narrow** (see `frisian_mcp.contrib.oauth._redirect_uri_allowlist`):

- Exact host match: `"claude.ai"` matches `"claude.ai"` only.
- Leading-`*.` wildcard with **label-boundary** semantics: `"*.anthropic.com"` matches `"api.anthropic.com"` and `"x.y.anthropic.com"` but never the bare apex `"anthropic.com"` and never a suffix-substring attacker host like `"anthropic.com.evil.example"`.
- Reverse-DNS custom-scheme native-app redirects (e.g. `com.example.app:/cb`) match on the URI scheme; allowlist entries for that flow are the reverse-DNS string itself.
- Both pattern and host are **IDNA-normalized** before comparison so a Cyrillic look-alike host (e.g. `cl{U+0430}ude.ai`) cannot bypass an entry spelled in ASCII.

**Empty allowlist is fail-closed.** `AUTO_REGISTER=True` with `[]` (or unset) behaves exactly as `AUTO_REGISTER=False`: no unknown-client registration is permitted on any host. There is no implicit `localhost` bypass on this path; loopback redirect URIs still require an explicit allowlist entry under `AUTO_REGISTER`.

**Public log-event symbols** (importable from `frisian_mcp.contrib.oauth._redirect_uri_allowlist`):

| Constant | Emitted when |
|---|---|
| `OAUTH_PKCE_AUTO_REGISTER_ALLOWLIST_EMPTY` | `AUTO_REGISTER=True` and the allowlist is empty / unset. The request is rejected and operators are warned about the misconfiguration. |
| `OAUTH_PKCE_AUTO_REGISTER_HOST_REJECTED` | A request's redirect URI host fails the allowlist check. |

### 3. T7 — Authorize-path inputs never set client tier

The `redirect_uri` is no longer a tier signal. Two changes restore the invariant:

- **`FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP` is removed.** The setting accepted a `redirect_uri → tier` mapping and applied it to the stored `OAuthClient.permission` at first contact under `AUTO_REGISTER`. It is gone in this release. The helper `_pkce_permission_for_uri` is removed with it. Operators who depended on per-redirect tier inference must set `OAuthClient.permission` explicitly in the Django admin after auto-registration; see the CHANGELOG entry for migration guidance.
- **The stored `OAuthClient.permission` is operator-set authority.** Nothing on the authorize-path mutates the stored tier upward (or downward) based on request shape — neither `redirect_uri`, requested `scope`, nor any other client-supplied field. Operator action in the admin is the only mutation pathway.

The token endpoint logs `OAUTH_PKCE_REDIRECT_URI_IGNORED_AS_TIER_SIGNAL` (INFO) when a code is exchanged whose authorize-time `redirect_uri` would have, under the old behavior, promoted the client's tier. The signal lets operators observe attempted-but-blocked promotion without blocking a legitimate exchange. The log fires at **code redemption** (in `TokenView._handle_authorization_code`), not at `authorize`-time code issuance; an operator searching for the signal looks at the token exchange in the request lifecycle, not the authorize step.

### 4. T8 — Token authority is fixed at issuance

Token tier is now snapshotted onto `OAuthAccessToken.permission` at issuance and is the ceiling for that token for the rest of its life.

The authenticator (`frisian_mcp.contrib.oauth.authentication._effective_tier`) returns

```
min(token.permission, client.permission)
```

over the tier ordering `read < read_write < admin`. This makes the live `OAuthClient.permission` read a **narrowing cap**, not a widening signal:

- An operator admin-console **downgrade** of the issuing client takes effect live — every outstanding token narrows to the lower of (snapshot, current client) at the next request.
- An operator admin-console **upgrade** does NOT widen previously-issued tokens. The issuance snapshot is the ceiling. Operators who need to grant a wider tier to an existing client must reissue the token after the upgrade.

When the snapshot is strictly narrower than the live client tier — the case that surfaces either a legitimate downgrade or a propagated-but-blocked escalation attempt — the authenticator emits the INFO log `oauth_token_authority_narrower_than_client_tier`, throttled per `(token_pk, observed_client_tier)` per process. Downstream alerting can correlate the log with the `OAuthClient` admin audit trail to distinguish the two cases.

### 5. T9 — `AUTO_APPROVE` reframed as "remember consent"

`FRISIAN_MCP_OAUTH_AUTO_APPROVE` no longer means "skip the consent form." It now means "remember consent for repeat grants of the same `(user, client_id, redirect_uri, scope)` tuple." Two coordinated changes restore the invariant:

- **The DEBUG-derived default is removed.** `_auto_approve_default()` returns `False` unconditionally. Operators who flipped `DEBUG=True` to debug a deploy no longer silently inherit a no-consent posture from the framework's debug toggle.
- **A first-time consent gate is now mandatory.** A new model, `OAuthAuthorizeConsent` (FK `user`, char `client_id` / `redirect_uri` / `scope`, unique-together), records each granted consent. The `AuthorizeView` `GET` handler walks a five-rule decision tree before issuing a code:

  1. Validation error → standard error path.
  2. `just_auto_registered=True` (the client was lazily created by the T1 path on this very request) → ALWAYS render the consent form. `AUTO_APPROVE` cannot fast-path on a first-touch registration.
  3. `AUTO_APPROVE=True` AND a prior `OAuthAuthorizeConsent(user, client_id, redirect_uri, scope=client.permission)` row exists → issue the code. Log `OAUTH_AUTHORIZE_AUTO_APPROVED_ON_PRIOR_CONSENT` (INFO, throttled per-tuple per-process).
  4. `AUTO_APPROVE=True` AND no prior consent → render the consent form. Log `OAUTH_AUTHORIZE_CONSENT_REQUIRED` (INFO, `reason="no_prior_consent"`).
  5. `AUTO_APPROVE=False` → render the consent form silently. Log `OAUTH_AUTHORIZE_CONSENT_REQUIRED` (INFO, `reason="no_prior_consent"`).

  The `POST` handler validates an `allow=true` / `allow=false` decision and either persists a new `OAuthAuthorizeConsent` row + issues the code, or logs `OAUTH_AUTHORIZE_CONSENT_DENIED` (WARNING) and rejects. CSRF is enforced by Django's existing `CsrfViewMiddleware` — `AuthorizeView` is not `csrf_exempt`'d (unlike `TokenView` / `RegistrationView` / `BareRegisterView` which are intentionally exempt for browserless OAuth flows), and the existing consent template carries `{% csrf_token %}`. No new CSRF code is added by this fix.

The log-event symbols are importable from `frisian_mcp.contrib.oauth._consent_gate`:

| Constant | Emitted when |
|---|---|
| `OAUTH_AUTHORIZE_CONSENT_REQUIRED` | `INFO`; consent form must render (`reason ∈ {"just_auto_registered", "no_prior_consent"}`). |
| `OAUTH_AUTHORIZE_CONSENT_DENIED` | `WARNING`; user POSTed the form and refused. |
| `OAUTH_AUTHORIZE_AUTO_APPROVED_ON_PRIOR_CONSENT` | `INFO`, throttled per `(user_id, client_id, redirect_uri, scope)` per process; the AUTO_APPROVE fast-path matched a prior consent record. |

**Operator workflow for M2M / browserless flows.** Server-to-server callers cannot render a consent form. Operators with M2M clients pre-populate `OAuthAuthorizeConsent` records via the Django admin (a `revoke_selected_consents` bulk action is registered for the inverse operation). The admin surface is intentionally browsable so an operator audit trail of "who consented to what scope for which redirect URI" is always one query away from the host's admin UI.

### 6. T10 — `has_perm` default-deny with `TIER_PERMISSIONS` opt-in

`OAuthServicePrincipal.has_perm` and `has_module_perms` no longer return `True` for any permission string at the `read_write` / `admin` tiers. They now default-deny, and the operator opts in via a new setting:

```python
FRISIAN_MCP_OAUTH_TIER_PERMISSIONS: dict[str, list[str]] = {
    "read":       ["dcim.view_device"],
    "read_write": ["dcim.change_device"],
    "admin":      ["dcim.delete_device"],
}
```

Default is `{}` — default-deny across every tier.

**Inheritance is monotonic up the ladder.** `admin` accumulates its own list plus `read_write`'s list plus `read`'s list; `read_write` accumulates its own plus `read`'s; `read` returns only its own. The accumulated set is what `_tier_permissions_for(tier)` returns. Operators who set `{"admin": [...], "read": [...]}` see `admin` callers correctly inherit the `read` allowlist without re-listing it. Strict-no-inheritance would force operators to duplicate every lower-tier perm at every higher tier and create sharp gotchas (an `admin` client unable to perform a `read` perm because the operator forgot to copy the allowlist down).

API contract:

- **`has_perm(perm, obj=None)`** — empty `perm` → `False`; otherwise returns `perm in _tier_permissions_for(self.permission)`. The `obj` argument is accepted for `BaseBackend`-style call compatibility but is not consulted (T10 enforces a string-level allowlist, not an object-level check; object-level safety on reads is inherited from the host's existing query-restriction machinery per ADR-008).
- **`has_perms(perm_list, obj=None)`** — unchanged; the existing `all(has_perm(p) for p in perm_list)` pattern naturally returns `False` when any item fails.
- **`has_module_perms(app_label)`** — empty `app_label` → `False`; otherwise returns `True` iff any perm in the accumulated set starts with `f"{app_label}."`.

Unknown perm strings, unknown tier keys in the dict, and non-`dict` / non-`list` misconfigurations all fall through to `False` (no `KeyError`, no exception at request time). The defensive shape makes operator misconfiguration noisy at the `mcp_doctor` audit (T3 surfaces a `WARN` when `OAuthTokenAuthentication` is wired and `TIER_PERMISSIONS` is empty) rather than at runtime.

**Scope clarification.** T10 narrows `request.user.has_perm(...)` calls that originate in **host code outside the MCP layer**. The MCP-internal tier filter (`FRISIAN_MCP_MAX_TIER`, `FRISIAN_MCP_TOKEN_TIER_MAP`, the dispatcher's per-action tier gate) is unchanged. Hosts that never relied on `OAuthServicePrincipal.has_perm` (i.e. hosts that exclusively gate by MCP tier) are unaffected.

### 7. T11 — Atomic PKCE authorization-code single-use

The PKCE authorization-code single-use check was a `cache.get(payload)` → cheap-shape checks → `cache.delete(payload)` → mint sequence. Under concurrent exchanges of the same code, two callers could both pass the `cache.get` step before either reached the `cache.delete`, and both would mint a token. The fix replaces the sequence with an atomic gate:

1. `cache.get(payload)` — missing / expired → `invalid_grant`.
2. Cheap shape checks (`client_id`, `redirect_uri`, PKCE verifier).
3. **`cache.add(consume_marker, True, _AUTH_CODE_TTL)`** — the atomic gate. The loser of the race gets `False` back from `cache.add`, logs `OAUTH_AUTHORIZATION_CODE_REPLAY_DETECTED` (WARNING), and returns `invalid_grant` to the caller.
4. `cache.delete(payload)` — cleanup.
5. Mint the token.

The consume-marker key is namespaced separately from the payload key so the gate is not entangled with the cache eviction policy of the code itself. The constant `_AUTH_CODE_CONSUMED_PREFIX = "frisian_mcp:oauth_code_consumed:"` is importable from `frisian_mcp.contrib.oauth.views`. The log-event symbol `OAUTH_AUTHORIZATION_CODE_REPLAY_DETECTED` is intentionally NOT throttled — replays are rare and operationally interesting; a sequence of identical events points at a real concurrent-exchange or replay scenario the operator should look at.

**`cache.add()` is part of Django's `BaseCache` contract.** Backend compatibility:

| Backend | `add()` semantics | Atomic for T11 |
|---|---|---|
| `LocMemCache` | thread-locked `setdefault` | Yes (within-process) |
| `RedisCache` | `SET NX EX` (single command) | Yes (across clients) |
| `MemcachedCache` | native `ADD` (fails if key exists) | Yes |
| `DatabaseCache` | `INSERT … IF NOT EXISTS` | Yes |
| `DummyCache` | always returns `True` | **No — T11 does not function on `DummyCache`** |

Operators running `DummyCache` in production are already in a misconfigured state (every cache operation no-ops); the doctor's `_check_cache_backend` signal surfaces this independently of T11. T11 introduces no new setting and requires no operator action beyond ensuring the cache backend is one of the four supported above.

The choice of `cache.add()` over Redis-specific primitives (`GETDEL`, Lua scripts) is deliberate: the contract is the `BaseCache` interface, not a single backend, so unit tests run against `LocMemCache` and integration / CI tests run against `RedisCache` without code divergence. Operators are not forced into Redis specifically.

## 8. Consequences

### Positive

- **Walk-up posture is preserved.** `AUTO_REGISTER=True` with an explicit host allowlist is the supported way to keep "point a connector at the URL and go" working; the operator declares the trusted set once and walk-up registration continues for callers on those hosts.
- **Defense in depth.** T1, T7, and T8 each fail closed on their own. The host allowlist (T1) stops unknown hosts from registering; the tier-inference removal (T7) stops a registered host from promoting the stored tier; the issuance snapshot (T8) stops any post-issuance promotion from widening already-issued tokens. An operator who only ships one of the three still gets the protection that one provides.
- **Operator surface stays narrow.** One new setting (`FRISIAN_MCP_OAUTH_PKCE_AUTO_REGISTER_HOST_ALLOWLIST`) and one removed setting (`FRISIAN_MCP_OAUTH_PKCE_REDIRECT_TIER_MAP`). No new admin UI is required for T1/T7/T8 — the existing `OAuthClient` admin and the standard Django logging pipeline carry the rest.
- **Log symbols are importable.** Tests and external log consumers import canonical constants from `_redirect_uri_allowlist` and `views.py` instead of hard-coding event-name strings; renames cause an import error at collection time rather than a silent missed assertion.
- **Public-discovery fall-back is preserved.** Token authenticator `WWW-Authenticate` shape and the `FRISIAN_MCP_OAUTH_PUBLIC_DISCOVERY` gate are unchanged.

### Negative / risks

- **Breaking change for `PKCE_REDIRECT_TIER_MAP` users.** Any deployment that depended on the removed setting to assign tiers per-`redirect_uri` must move that assignment to the admin (set `OAuthClient.permission` per-client). The CHANGELOG entry calls this out explicitly. Operators upgrading without setting per-client tiers see clients land at the `FRISIAN_MCP_OAUTH_PKCE_DEFAULT_PERMISSION` default (`"read"`), which is intentionally the safe end of the range.
- **Allowlist administration is now an operator responsibility.** `AUTO_REGISTER=True` with an empty allowlist is a no-op — equivalent to `AUTO_REGISTER=False`. Operators who flipped `AUTO_REGISTER=True` and never reviewed the host set will see auto-registration stop working until they populate the allowlist. The `mcp_doctor` extended security audit (`python manage.py mcp_doctor --security`) now ERRORs (exit non-zero) when `AUTO_REGISTER=True` with an empty allowlist outside `DEBUG`, and WARNs under `DEBUG`. The in-request log `OAUTH_PKCE_AUTO_REGISTER_ALLOWLIST_EMPTY` is the second audit trail for the same condition.
- **Outstanding tokens do not widen on admin upgrade (T8 contract).** An operator who upgrades a client from `read` → `read_write` and expects existing tokens to gain write access will see them stay at the issuance snapshot. The intentional contract is "tokens narrow live, never widen live"; the workaround is to reissue the token. Documented in the README posture section.
- **IDNA-normalization failure is treated as no-match.** A pattern or host that the IDNA encoder rejects (rare, but possible with malformed input) silently never matches. This is intentional fail-closed behavior — an unprocessable host is not trusted — but operators who write patterns and never see a match should suspect normalization first.
- **`AUTO_APPROVE` semantic change is breaking (T9).** Operators who flipped `FRISIAN_MCP_OAUTH_AUTO_APPROVE=True` previously got "no consent form, ever" and silently inherited the same posture in development by way of the DEBUG default. The new contract is "remember consent for repeat grants of the same tuple"; the first authorize for any new `(user, client_id, redirect_uri, scope)` still renders a form. M2M operators must pre-populate `OAuthAuthorizeConsent` records via the admin to preserve silent code issuance. The `mcp_doctor` standard audit WARNs when `AUTO_APPROVE=True` AND `OAuthAuthorizeConsent.objects.count() == 0` — the operator opted in to `AUTO_APPROVE` but never recorded any consent, which is almost always a misconfiguration.
- **`has_perm` default-deny is breaking outside the MCP layer (T10).** Hosts that read `request.user.has_perm("app.action")` in code paths outside the MCP gateway will see `False` for OAuth-authenticated requests where they previously saw `True` (at `read_write` / `admin` tiers). The remediation is to populate `FRISIAN_MCP_OAUTH_TIER_PERMISSIONS` with the perm strings the operator wants each tier to grant. The MCP-internal tier filter (`FRISIAN_MCP_MAX_TIER`, `FRISIAN_MCP_TOKEN_TIER_MAP`, dispatcher per-action tier gate) is unchanged; hosts that never relied on `OAuthServicePrincipal.has_perm` outside MCP are unaffected.
- **`DummyCache` deployments break T11 silently.** The atomic single-use primitive (`cache.add()`) returns `True` unconditionally on `DummyCache`, so the gate never fires and concurrent exchanges of the same code can both mint tokens. `DummyCache` in production is already a misconfigured state (every cache operation no-ops); the `mcp_doctor` standard audit's `_check_cache_backend` signal surfaces this independently of T11. Operators on `LocMemCache` / `RedisCache` / `MemcachedCache` / `DatabaseCache` are not affected.

### Neutral

- The unknown-client branch of `_validate_authorize_params` now returns a `_AuthorizeValidation(error, just_auto_registered)` NamedTuple rather than a bare `str`. Existing call sites that indexed `[0]` continue to work; new call sites use the field names. The shape change supports the consent gate that lands in §5.

## 9. Alternatives considered

- **First-seen `client_id` ↔ `redirect_uri` binding.** Considered. Adds a stored invariant that the first `redirect_uri` observed for a given `client_id` is the only one ever accepted for subsequent authorize requests. Rejected for this release on the grounds that the host allowlist + tier-inference removal + issuance snapshot already close the documented escalation chain, and adding a fourth gate raises the risk of operator-visible regressions (legitimate redirect URI rotation) for marginal additional coverage. **Deferred, not declined.** Tracked as a follow-up if subsequent red-team review surfaces a residual gap that first-seen binding would close.
- **Reject `AUTO_REGISTER=True` entirely.** Considered. Would have removed the walk-up posture and required every OAuth client to be pre-registered in the admin before its first authorize request. Rejected because the walk-up posture is the documented onboarding ergonomic for new connectors and removing it would break that workflow with no migration path. The host allowlist preserves the posture while making it explicit.
- **Per-client allowlist instead of a global allowlist.** Considered. The `OAuthClient` admin already carries an allowed-redirect-URI list per known client; layering a per-client allowlist onto the unknown-client `AUTO_REGISTER` branch was rejected because at that point the client is, by definition, unknown — there is no per-client record to consult. A global allowlist is the natural operator surface for "the set of hosts this server will lazily register on first sight."
- **Implicit loopback bypass for development.** Considered. Rejected. Loopback redirect URIs still go through the `AUTO_REGISTER` allowlist on the unknown-client path; operators who need walk-up loopback registration in development add `"127.0.0.1"` / `"::1"` / `"localhost"` to the allowlist explicitly. The implicit-loopback exception in `_redirect_uri_is_safe` covers scheme-level safety only — it does not, and must not, satisfy the unknown-client `AUTO_REGISTER` gate.

## 10. References

- ADR-003 — URL Auto-Registration (auto-discovery; complements the OAuth client surface this ADR addresses).
- ADR-008 — Permission-Aware Tool Discovery (the broader principle that authority on the MCP surface is derived from the host auth backend, never from request shape; T7 / T8 are the OAuth-side application of that principle).
- IETF RFC 6749 — The OAuth 2.0 Authorization Framework.
- IETF RFC 7636 — Proof Key for Code Exchange by OAuth Public Clients (PKCE).
- IETF RFC 8252 — OAuth 2.0 for Native Apps (the reverse-DNS custom-scheme redirect convention used by §2's pattern syntax).
- `docs/Security/security.md` — frisian-mcp's deployment-shape recommendations (path separation, rate limiting, group-scoped doc visibility); this ADR refines the OAuth surface that recommendation assumes.
- `docs/Reference/installation-configuration-reference.md` — the complete settings reference; the new and removed settings in §2 / §3 land there in the same release.
