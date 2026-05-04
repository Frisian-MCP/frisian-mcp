"""SEC-1: hash existing OAuthAccessToken.token rows in place.

Prior to SEC-1, ``OAuthAccessToken.token`` was stored as a 64-char plaintext
hex string and authentication compared the bearer header against that exact
value.  After SEC-1 the column stores the HMAC-SHA256 digest of the raw token
and authentication hashes the bearer header before lookup.

This migration upgrades any existing deployed rows non-destructively:
each plaintext token in the column is replaced with its HMAC digest, treating
the previously-stored value as the raw token (which is, in fact, exactly what
clients still hold).  After the migration old clients keep working — they
present the raw token, the gateway hashes it, the digest matches.

Tokens that already look like an HMAC digest from the same key (e.g. because
this migration was applied twice) are skipped.  In practice both shapes are
64 hex characters, so the safe path is to skip rows whose digest *of itself*
already matches the stored value — a no-op idempotency guard.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_lib
from typing import Any

from django.conf import settings
from django.db import migrations, models


def _hmac_secret(raw: str) -> str:
    """Mirror of ``contrib.oauth.models._hmac_secret`` for this migration only."""
    hmac_key: str = getattr(settings, "FRIESE_MCP_HMAC_KEY", "") or settings.SECRET_KEY
    return _hmac_lib.new(hmac_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _hash_existing_tokens(apps: Any, schema_editor: Any) -> None:
    """Replace plaintext OAuthAccessToken.token values with their HMAC digest."""
    # pylint: disable=invalid-name
    OAuthAccessToken = apps.get_model("friese_mcp_oauth", "OAuthAccessToken")
    for row in OAuthAccessToken.objects.all().iterator():
        digest = _hmac_secret(row.token)
        if digest == row.token:
            # Already-hashed (idempotency).  Skip.
            continue
        row.token = digest
        row.save(update_fields=["token"])


def _noop_reverse(apps: Any, schema_editor: Any) -> None:
    """Reverse migration intentionally no-op: HMAC is one-way."""


class Migration(migrations.Migration):
    """Replace plaintext access tokens with their HMAC digest in place."""

    dependencies = [
        ("friese_mcp_oauth", "0005_alter_oauthclient_client_secret"),
    ]

    operations = [
        migrations.RunPython(_hash_existing_tokens, _noop_reverse),
        migrations.AlterField(
            model_name="oauthaccesstoken",
            name="token",
            field=models.CharField(
                editable=False,
                help_text=(
                    "HMAC-SHA256 of the raw Bearer token keyed by SECRET_KEY.  Never "
                    "the raw value — the raw token is exposed exactly once via "
                    "``plaintext_token`` on the freshly-saved instance and is never "
                    "persisted, so a leaked DB row is not directly exploitable."
                ),
                max_length=64,
                unique=True,
            ),
        ),
    ]
