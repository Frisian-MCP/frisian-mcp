"""Persist RFC 8707 resource indicators and upgrade consent fingerprints."""

from __future__ import annotations

import hashlib

from django.db import migrations, models


def _fingerprint(client_id: str, redirect_uri: str, scope: str, resource: str | None) -> str:
    digest = hashlib.sha256(b"frisian-mcp:oauth-consent:v2\0")
    for value in (client_id, redirect_uri, scope, resource):
        if value is None:
            digest.update(b"\xff")
            continue
        encoded = value.encode("utf-8")
        digest.update(b"\x00")
        digest.update(len(encoded).to_bytes(4, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def _upgrade_consent_fingerprints(apps: migrations.state.StateApps, schema_editor: object) -> None:
    """Backfill v2 fingerprints without treating legacy consent as route-wide."""
    del schema_editor
    Consent = apps.get_model("frisian_mcp_oauth", "OAuthAuthorizeConsent")
    rows = list(Consent.objects.all().only("pk", "user_id", "client_id", "redirect_uri", "scope"))
    updates: list[object] = []
    seen: set[tuple[object, str]] = set()
    for consent in rows:
        fingerprint = _fingerprint(
            consent.client_id, consent.redirect_uri, consent.scope, None
        )
        key = (consent.user_id, fingerprint)
        if key in seen:
            raise RuntimeError("Refusing to migrate duplicate OAuth consent fingerprints.")
        seen.add(key)
        consent.resource = None
        consent.grant_fingerprint = fingerprint
        updates.append(consent)
    if updates:
        Consent.objects.bulk_update(updates, ["resource", "grant_fingerprint"])


class Migration(migrations.Migration):
    dependencies = [("frisian_mcp_oauth", "0004_consent_grant_fingerprint")]

    operations = [
        migrations.AddField(
            model_name="oauthaccesstoken",
            name="resource",
            field=models.CharField(
                blank=True,
                help_text=(
                    "RFC 8707 resource requested during authorization. Null tokens are "
                    "compatible with every protected route."
                ),
                max_length=2000,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="oauthauthorizeconsent",
            name="resource",
            field=models.CharField(
                blank=True,
                help_text="RFC 8707 resource URL included in the approved authorization request.",
                max_length=2000,
                null=True,
            ),
        ),
        migrations.RunPython(_upgrade_consent_fingerprints, migrations.RunPython.noop),
    ]
