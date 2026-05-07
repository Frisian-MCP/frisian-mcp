from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("friese_mcp_oauth", "0007_oauthclient_redirect_uris"),
    ]

    operations = [
        migrations.AlterField(
            model_name="oauthclient",
            name="client_id",
            field=models.CharField(
                editable=False,
                help_text="OAuth client identifier — auto-generated (32 hex chars) or supplied by the client.",
                max_length=255,
                unique=True,
            ),
        ),
    ]
