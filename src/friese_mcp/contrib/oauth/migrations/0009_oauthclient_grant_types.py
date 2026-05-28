"""Add grant_types field to OAuthClient (RFC 7591 §2)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("friese_mcp_oauth", "0008_alter_oauthclient_client_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthclient",
            name="grant_types",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Allowed OAuth 2.0 grant types for this client (RFC 7591 §2).  "
                    "An empty list means no restriction — all supported grant types "
                    "(\"client_credentials\", \"authorization_code\") are permitted.  "
                    "Set to [\"client_credentials\"] for service-to-service clients "
                    "that should never use the PKCE flow, or [\"authorization_code\"] "
                    "for browser/native clients that should not use client_credentials."
                ),
            ),
        ),
    ]
