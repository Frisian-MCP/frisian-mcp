# ADR-012: Resource-aware OAuth consent and token audience binding

**Date:** 2026-08-25
**Category:** adr
**Supersedes:** —
**Related:** ADR-009 (Authorize-path inputs are never authority), ADR-010 (Per-route permission model), issue #69

---

## Context

RFC 9728 metadata is already emitted per protected route.  RFC 8707-capable
MCP clients send the selected protected-resource URL as `resource` to the
OAuth authorization endpoint.  The current authorization-code flow discards
that parameter: its code-cache payload, `OAuthAccessToken`, and
`OAuthAuthorizeConsent` have no resource/audience identity.  Consequently, a
consent that auto-approves an authorize request for route A can also
auto-approve route B, and the resulting bearer token is accepted on either
route.

This is not fixed by route tier ceilings.  A ceiling limits what a token may do
*after* it reaches a route; it does not establish where the token may be
presented.

## Decision

**`resource` joins the identity of `OAuthAuthorizeConsent`.**  A remembered
consent is exactly:

```
(user, client_id, redirect_uri, scope, resource)
```

where `resource` is the canonical protected-resource URL, or `NULL` when the
request carries no RFC 8707 resource indicator.  It is not deliberately
resource-blind.  `AUTO_APPROVE=True` may only fast-path an exact five-part
identity match.

The same canonical value is stored on a token issued from the authorization
code.  An OAuth token with a non-NULL resource is valid only when presented to
that exact configured protected route.  A token whose resource is `NULL` skips
audience validation.  The latter is an explicit backwards-compatibility rule
for all tokens issued before this change and for clients that do not send RFC
8707; it is not evidence that a resource-bearing consent may match a
resource-less request.

### Resource value and validation

The server, not the client string alone, establishes the persisted audience:

1. Read at most one `resource` parameter from the authorize request; duplicate
   values are rejected (`invalid_target`).
2. Resolve it against `route_resources.protected_resources()` using the current
   request origin (`_get_base_url(request)`) and each resource's
   `ProtectedResource.resource_url(base)` value.
3. Only an exact match to an advertised, authenticated protected resource is
   accepted.  Unknown, open-route, malformed, alternate-origin, and
   slash-normalized look-alike values are rejected (`invalid_target`), rather
   than stored as arbitrary audience strings.
4. Persist that canonical URL unchanged through consent, code cache, and token.

No resource parameter remains `NULL`; it is not inferred from `redirect_uri`,
the default protected resource, path names, or a client tier.  That preserves
legacy clients without silently binding them to the wrong route.  A client
which follows the per-route RFC 9728 card receives exactly the URL it must
supply and later present the token to.

### Consent and token rules

* `has_prior_consent`, `record_consent`, consent fingerprints, the prior-consent
  log throttle, and admin prepopulation all use the five-part identity.
* The consent `POST` must carry the validated resource in a hidden field and
  revalidate it.  It must not trust a browser-modified field.
* `_issue_code_redirect` records the resource in the authorization-code cache
  payload.  `TokenView._handle_authorization_code` copies that value into the
  newly created `OAuthAccessToken`.
* `OAuthTokenAuthentication.authenticate` resolves the called request path via
  `resource_for_path(request.path)`.  For a non-NULL token resource, it compares
  the canonical URL for that resolved route with the token value before updating
  `last_used_at` or returning a principal.  No resolved protected resource, or
  a different one, is an authentication failure (401), not a tier denial (403).
* On a mismatch, the `WWW-Authenticate` resource-metadata pointer identifies
  the **token's** configured resource, not the route that rejected it.  The
  authenticator may stamp the mismatch audience on the request so
  `authenticate_header` can map it back through `protected_resources()`.
  If that defensive mapping is impossible, fail with the normal route challenge;
  never accept the token.

The `client_credentials` flow is unchanged by issue #69: it has no
authorization request/code cache and continues to mint resource-less tokens.
Adding RFC 8707 to that flow is a separate design and must not be implied by
this migration.

## Schema and migration plan

Add nullable fields (recommended `CharField(max_length=2000, null=True,
blank=True)`) to both models:

| Model | Field | Meaning |
|---|---|---|
| `OAuthAuthorizeConsent` | `resource` | Exact canonical resource approved by the user; `NULL` is legacy/no-indicator consent. |
| `OAuthAccessToken` | `resource` | Exact canonical audience; `NULL` skips route validation for compatibility. |

Keep the fixed-size fingerprint constraint rather than adding a multi-column
unique constraint.  Change `OAuthAuthorizeConsent.fingerprint_for()` to accept
`resource: str | None` and hash all five values under a new, versioned domain
separator (for example `frisian-mcp:oauth-consent:v2\\0`).  Encode NULL
separately from an empty string.  This avoids MySQL's oversized index problem,
keeps case-sensitive/exact tuple verification, and makes a resource-bearing
row impossible to collide logically with a legacy row.

Ship one forward migration after the existing squashed consent migration:

1. add both nullable `resource` columns;
2. recompute every existing consent fingerprint using v2 and `resource=NULL`;
3. replace `frisian_mcp_oac_user_fp_uniq` only as necessary around the
   backfill (drop/re-add if the backend cannot update constrained keys in
   place); and
4. leave existing tokens with `resource=NULL`.

The migration must update the historical model in batches and detect duplicate
`(user_id, v2_fingerprint)` values before enforcing/re-adding uniqueness, as
0004 does today.  In practice no distinct v1 tuple can collapse under the
injective v2 encoding, but fail closed rather than choosing a consent row if a
database is already corrupt.  Update the squashed fresh-install migration to
create both fields and the v2 fingerprint contract.  Preserve the existing
MySQL recovery path and extend its expected schema/tests for the new leaf
state; do not rewrite already-applied migration history.

Existing `OAuthAuthorizeConsent` rows deliberately become resource-less,
not grants for every route.  Therefore their prior-consent fast path still
works only for a future resource-less authorization request; the first
resource-bearing request renders consent and creates a distinct row.  This is
the migration consequence that prevents an old broad remembered grant from
silently becoming a new route-specific grant.

## Affected implementation surfaces

| Surface | Required change |
|---|---|
| `contrib/oauth/models.py` | Add `resource` fields; make consent fingerprint/derived-field refresh five-part and NULL-safe. |
| `contrib/oauth/_consent_gate.py` | Thread resource through render, lookup, create, collision verification, and log-throttle identity. |
| `contrib/oauth/views.py` | Parse/validate resource on GET and POST; preserve it in template/form, code-cache payload, and token issuance. |
| `contrib/oauth/templates/.../authorize.html` | Add the hidden resource field. |
| `contrib/oauth/authentication.py` | Enforce non-NULL token audience against request route and produce the token-resource challenge on mismatch. |
| `contrib/oauth/admin.py` | Display/filter/search resource on consent and token records; describe revocation as five-part. |
| `contrib/oauth/migrations/` | Add the forward data migration and update the squash/replacement path. |
| OAuth tests and migration tests | Cover exact audience behavior and all upgrade paths, especially MySQL. |

## Acceptance criteria

1. A user approving `(client, redirect URI, tier, route A)` cannot be
   auto-approved for route B; route B renders consent and records a distinct
   row.
2. Repeating the same five-part tuple with `AUTO_APPROVE=True` fast-paths;
   changing any part, including resource or NULL-versus-present, does not.
3. A code issued with route A's `resource` mints a token carrying route A's
   canonical URL; the token authenticates on A and returns 401 on B before
   `last_used_at` is changed or host permissions are consulted.
4. The mismatch 401 advertises route A's resource metadata.  A token with
   `resource=NULL` retains the existing no-audience-check behavior.
5. Invalid, duplicate, open-route, or noncanonical resource indicators are not
   persisted and do not issue a code/token.
6. A tampered consent POST cannot substitute its resource after the GET.
7. Existing consent rows migrate to `resource=NULL` and v2 fingerprints;
   existing access tokens remain usable; no migration creates a route-wide
   approval from a legacy row.
8. Fresh installs, upgrades from original 0003/0004, the squashed path, and
   MySQL's failed-0003 recovery all reach the same schema and uniqueness
   invariant.
9. Client-credentials behavior and non-OAuth authenticators remain unchanged.

## Alternatives rejected

* **Keep consent resource-blind.** Rejected: it preserves the precise
  cross-route auto-approval that audience binding is intended to stop.
* **Treat legacy consent as consent for all resources.** Rejected: migration
  convenience would mint a newly route-bound token without route-specific user
  approval.
* **Bind only tokens, not consent.** Rejected: token use would be safe but a
  request for route B could still silently mint a new B token from A's consent.
* **Infer an audience from redirect URI, tier, or default route.** Rejected:
  those values are not a route audience and inference can bind a client to a
  route it did not select.
* **Use route names/paths as the stored audience.** Rejected: RFC 8707 and
  RFC 9728 use the resource URI; a URL is the interoperable identity and
  preserves legacy trailing-slash semantics.

## References

* RFC 8707 — Resource Indicators for OAuth 2.0
* RFC 9728 — OAuth 2.0 Protected Resource Metadata
* Issue #69 — OAuth tokens are not bound to a route
* ADR-009 — request inputs do not become authority
