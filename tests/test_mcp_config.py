"""Tests for the mcp_config management command."""

from __future__ import annotations

import io
import json

from django.core.management import call_command
from django.test import override_settings


class TestMcpConfigCommand:
    """Tests for `python manage.py mcp_config`."""

    def _run(self, *args: str, **settings_overrides: object) -> dict:
        """Run the command with optional settings overrides and return parsed JSON."""
        buf = io.StringIO()
        with override_settings(**settings_overrides):
            call_command("mcp_config", *args, stdout=buf)
        return json.loads(buf.getvalue())

    def test_command_runs_without_error(self) -> None:
        """Command exits cleanly and produces output."""
        buf = io.StringIO()
        call_command("mcp_config", stdout=buf)
        assert buf.getvalue().strip() != ""

    def test_output_is_valid_json(self) -> None:
        """Output is parseable JSON."""
        buf = io.StringIO()
        call_command("mcp_config", stdout=buf)
        data = json.loads(buf.getvalue())
        assert isinstance(data, dict)

    def test_mcp_servers_key_present(self) -> None:
        """Top-level output has 'mcpServers' key."""
        data = self._run()
        assert "mcpServers" in data

    def test_default_server_name(self) -> None:
        """Without FRIESE_MCP_SERVER_NAME, server key defaults to 'friese-mcp'."""
        data = self._run()
        assert "friese-mcp" in data["mcpServers"]

    def test_custom_server_name(self) -> None:
        """FRIESE_MCP_SERVER_NAME is used as the server key in mcpServers."""
        data = self._run(FRIESE_MCP_SERVER_NAME="my-app")
        assert "my-app" in data["mcpServers"]
        assert "friese-mcp" not in data["mcpServers"]

    def test_default_url(self) -> None:
        """Without FRIESE_MCP_BASE_URL or --url, defaults to localhost:8000/mcp/."""
        data = self._run()
        server = next(iter(data["mcpServers"].values()))
        assert server["url"] == "http://localhost:8000/mcp/"

    def test_settings_url(self) -> None:
        """FRIESE_MCP_BASE_URL setting is used when present."""
        data = self._run(FRIESE_MCP_BASE_URL="https://api.example.com/mcp/")
        server = next(iter(data["mcpServers"].values()))
        assert server["url"] == "https://api.example.com/mcp/"

    def test_url_flag_overrides_settings(self) -> None:
        """--url CLI flag takes precedence over FRIESE_MCP_BASE_URL setting."""
        buf = io.StringIO()
        with override_settings(FRIESE_MCP_BASE_URL="https://settings.example.com/mcp/"):
            call_command("mcp_config", "--url", "https://flag.example.com/mcp/", stdout=buf)
        data = json.loads(buf.getvalue())
        server = next(iter(data["mcpServers"].values()))
        assert server["url"] == "https://flag.example.com/mcp/"

    def test_transport_is_http(self) -> None:
        """Server entry includes transport: 'http'."""
        data = self._run()
        server = next(iter(data["mcpServers"].values()))
        assert server["transport"] == "http"
