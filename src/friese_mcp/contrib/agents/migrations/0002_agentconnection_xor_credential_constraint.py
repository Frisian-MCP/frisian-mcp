"""Migration: add XOR credential CheckConstraint to AgentConnection."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add agent_connection_xor_credential CheckConstraint."""

    dependencies = [
        ("friese_mcp_agents", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="agentconnection",
            constraint=models.CheckConstraint(
                condition=models.Q(token__isnull=True) | models.Q(oauth_client__isnull=True),
                name="agent_connection_xor_credential",
            ),
        ),
    ]
