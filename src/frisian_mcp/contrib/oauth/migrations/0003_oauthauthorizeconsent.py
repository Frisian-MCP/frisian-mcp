"""Add OAuthAuthorizeConsent for the first-time consent gate (T9 / M-oauth-auto-approve-debug-default)."""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Create OAuthAuthorizeConsent with a unique grant per ``(user, client_id, redirect_uri, scope)``."""

    dependencies = [
        ("frisian_mcp_oauth", "0002_oauthclient_user"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OAuthAuthorizeConsent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "client_id",
                    models.CharField(
                        help_text=(
                            "OAuth client_id from the authorize request at the time of consent."
                        ),
                        max_length=255,
                    ),
                ),
                (
                    "redirect_uri",
                    models.CharField(
                        help_text=(
                            "Exact redirect_uri the user approved.  Must match on subsequent "
                            "requests."
                        ),
                        max_length=2000,
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        help_text=(
                            "Permission tier at the time of consent.  Currently one of "
                            "``read`` / ``read_write`` / ``admin`` (the OAuthClient.permission "
                            "value at consent time).  Stored as a free-form string so future "
                            "scope refactors do not require a migration."
                        ),
                        max_length=64,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        help_text="Django user who granted this consent.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="oauth_authorize_consents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "OAuth Authorize Consent",
                "verbose_name_plural": "OAuth Authorize Consents",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="oauthauthorizeconsent",
            constraint=models.UniqueConstraint(
                fields=("user", "client_id", "redirect_uri", "scope"),
                name="frisian_mcp_oac_unique_grant",
            ),
        ),
    ]
