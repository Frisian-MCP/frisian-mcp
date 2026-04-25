"""Migration: add last_used_at field to OAuthAccessToken."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add last_used_at DateTimeField to OAuthAccessToken."""

    dependencies = [
        ("friese_mcp_oauth", "0002_oauthaccesstoken_token_expires_at_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthaccesstoken",
            name="last_used_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Timestamp of the most recent authenticated request using this token.",
            ),
        ),
    ]
