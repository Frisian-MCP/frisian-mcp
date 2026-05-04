"""SEC-2: add ``redirect_uris`` to OAuthClient for the authorize-endpoint allowlist."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the registered redirect-URI allowlist to OAuthClient."""

    dependencies = [
        ("friese_mcp_oauth", "0006_hash_oauth_access_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthclient",
            name="redirect_uris",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Registered OAuth 2.0 redirect URIs (RFC 6749 §3.1.2).  The "
                    "authorize endpoint requires an exact-match against this list "
                    "before issuing an authorization code (SEC-2).  An empty list "
                    "disables the authorize endpoint for this client; populate it "
                    "via Django admin, RFC 7591 dynamic registration, or set "
                    "``FRIESE_MCP_OAUTH_PKCE_AUTO_REGISTER=True`` to permit "
                    "on-demand PKCE clients (HTTPS / loopback validation still "
                    "applies)."
                ),
            ),
        ),
    ]
