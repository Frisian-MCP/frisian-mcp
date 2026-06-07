"""Add nullable user FK to OAuthClient for per-client permission-aware discovery."""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add user field to OAuthClient."""

    dependencies = [
        ("frisian_mcp_oauth", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthclient",
            name="user",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Django user whose permissions govern tool visibility for this client "
                    "when FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY is enabled. "
                    "Leave blank to use the global FRISIAN_MCP_OAUTH_SERVICE_USER setting."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
