# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Security

- **AUTH-4 — Token hashing:** `FrieseMcpToken.token` and `OAuthClient.client_secret` are
  now stored as HMAC-SHA256 keyed by `SECRET_KEY` instead of plaintext.  The raw value is
  shown exactly once at creation time via the `_raw_token` / `_raw_client_secret` instance
  attribute (available immediately after `save()`, absent after a DB reload).

  **Migration note:** Existing tokens and OAuth clients created before this release are
  automatically invalidated — the stored plaintext no longer matches any HMAC lookup.
  Regenerate all `FrieseMcpToken` and `OAuthClient` records after deploying this version.
  No data migration is provided because the original plaintext values cannot be recovered
  from the database.

  Rotating `SECRET_KEY` also invalidates all tokens; this is intentional and desirable.

  `OAuthAccessToken.token` remains plaintext (short-lived, 1-hour TTL).
