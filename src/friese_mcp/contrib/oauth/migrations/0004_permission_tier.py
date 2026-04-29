"""Migration: replace scope field with permission tier on OAuthClient and OAuthAccessToken."""

from django.db import migrations, models

_PERMISSION_CHOICES = [
    ("read", "Read Only"),
    ("read_write", "Read Write"),
    ("admin", "Admin"),
]


class Migration(migrations.Migration):
    """
    Replace the ``scope`` CharField with ``permission`` on both OAuth models.

    Existing rows all had ``scope="mcp"`` (full access), which maps to the new
    default ``permission="read_write"``.  The default handles the data migration
    automatically — no explicit data migration step is needed.
    """

    dependencies = [
        ("friese_mcp_oauth", "0003_oauthaccesstoken_last_used_at"),
    ]

    operations = [
        # OAuthClient: add permission, remove scope
        migrations.AddField(
            model_name="oauthclient",
            name="permission",
            field=models.CharField(
                choices=_PERMISSION_CHOICES,
                default="read_write",
                help_text=(
                    "Controls which tier of tools tokens issued to this client can access: "
                    "Read Only, Read Write, or Admin."
                ),
                max_length=10,
            ),
        ),
        migrations.RemoveField(
            model_name="oauthclient",
            name="scope",
        ),
        # OAuthAccessToken: add permission, remove scope
        migrations.AddField(
            model_name="oauthaccesstoken",
            name="permission",
            field=models.CharField(
                choices=_PERMISSION_CHOICES,
                default="read_write",
                help_text="Permission tier inherited from the issuing client at token creation time.",
                max_length=10,
            ),
        ),
        migrations.RemoveField(
            model_name="oauthaccesstoken",
            name="scope",
        ),
    ]
