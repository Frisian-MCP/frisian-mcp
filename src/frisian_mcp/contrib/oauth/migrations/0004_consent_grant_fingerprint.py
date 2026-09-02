"""Backfill OAuth consent fingerprints and replace the oversized unique constraint."""

from __future__ import annotations

import hashlib
from typing import Any, cast

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_FINGERPRINT_DOMAIN = b"frisian-mcp:oauth-consent:v1\0"


def _fingerprint_for(client_id: str, redirect_uri: str, scope: str) -> str:
    """Return the migration-stable fingerprint for a consent tuple."""
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN)
    for value in (client_id, redirect_uri, scope):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def _backfill_grant_fingerprints(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Populate fingerprints and reject any duplicate derived keys."""
    consent_model = apps.get_model(
        "frisian_mcp_oauth",
        "OAuthAuthorizeConsent",
    )
    database_alias = schema_editor.connection.alias
    consents = consent_model.objects.using(database_alias)
    batch: list[Any] = []
    for consent in consents.all().iterator(chunk_size=500):
        consent.grant_fingerprint = _fingerprint_for(
            cast(str, consent.client_id),
            cast(str, consent.redirect_uri),
            cast(str, consent.scope),
        )
        batch.append(consent)
        if len(batch) == 500:
            consents.bulk_update(
                batch,
                ["grant_fingerprint"],
                batch_size=500,
            )
            batch.clear()
    if batch:
        consents.bulk_update(
            batch,
            ["grant_fingerprint"],
            batch_size=500,
        )
    if consents.filter(grant_fingerprint__isnull=True).exists():
        raise RuntimeError("Failed to backfill every OAuth consent fingerprint.")
    duplicate_exists = (
        consents.values("user_id", "grant_fingerprint")
        .annotate(row_count=models.Count("pk"))
        .filter(row_count__gt=1)
        .exists()
    )
    if duplicate_exists:
        raise RuntimeError("Duplicate OAuth consent fingerprints prevent enforcing uniqueness.")


class Migration(migrations.Migration):
    """Backfill and index fixed-size OAuth consent fingerprints."""

    dependencies = [
        ("frisian_mcp_oauth", "0003_oauthauthorizeconsent"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthauthorizeconsent",
            name="grant_fingerprint",
            field=models.CharField(
                blank=True,
                editable=False,
                help_text=(
                    "SHA-256 fingerprint of the exact (client_id, redirect_uri, scope) consent tuple."
                ),
                max_length=64,
                null=True,
            ),
        ),
        migrations.RunPython(
            _backfill_grant_fingerprints,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="oauthauthorizeconsent",
            name="grant_fingerprint",
            field=models.CharField(
                blank=True,
                editable=False,
                help_text=(
                    "SHA-256 fingerprint of the exact (client_id, redirect_uri, scope) consent tuple."
                ),
                max_length=64,
            ),
        ),
        migrations.AddConstraint(
            model_name="oauthauthorizeconsent",
            constraint=models.UniqueConstraint(
                fields=("user", "grant_fingerprint"),
                name="frisian_mcp_oac_user_fp_uniq",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="oauthauthorizeconsent",
            name="frisian_mcp_oac_unique_grant",
        ),
    ]
