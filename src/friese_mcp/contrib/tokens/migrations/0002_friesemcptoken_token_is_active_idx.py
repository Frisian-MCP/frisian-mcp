from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("friese_mcp_tokens", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="friesemcptoken",
            index=models.Index(
                fields=["token", "is_active"],
                name="friese_mcp_tok_active_idx",
            ),
        ),
    ]
