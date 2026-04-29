"""Migration: add permission field to FrieseMcpToken."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add permission CharField to FrieseMcpToken for three-tier access control."""

    dependencies = [
        ("friese_mcp_tokens", "0002_friesemcptoken_token_is_active_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="friesemcptoken",
            name="permission",
            field=models.CharField(
                choices=[
                    ("read", "Read Only"),
                    ("read_write", "Read Write"),
                    ("admin", "Admin"),
                ],
                default="read_write",
                help_text=(
                    "Controls which tier of tools this token can access: "
                    "Read Only (read tools only), Read Write (read + write tools), "
                    "or Admin (all tools)."
                ),
                max_length=10,
            ),
        ),
    ]
