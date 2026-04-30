"""Migration: add validate_tool_name_list validator to AgentConnection.allowed_tools."""

from django.db import migrations, models

import friese_mcp.contrib.agents.models


class Migration(migrations.Migration):
    """Alter allowed_tools to include validate_tool_name_list validator."""

    dependencies = [
        ("friese_mcp_agents", "0002_agentconnection_xor_credential_constraint"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agentconnection",
            name="allowed_tools",
            field=models.JSONField(
                blank=True,
                help_text=(
                    'Optional JSON array of tool names this agent may see and call '
                    '(e.g. ["users.list", "workouts.create"]). '
                    "Leave blank to allow all registered tools."
                ),
                null=True,
                validators=[friese_mcp.contrib.agents.models.validate_tool_name_list],
            ),
        ),
    ]
