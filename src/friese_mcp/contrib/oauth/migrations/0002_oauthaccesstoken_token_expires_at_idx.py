from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("friese_mcp_oauth", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="oauthaccesstoken",
            index=models.Index(
                fields=["token", "expires_at"],
                name="friese_mcp_oat_expires_idx",
            ),
        ),
    ]
