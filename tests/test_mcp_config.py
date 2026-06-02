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
        """Without FRISIAN_MCP_SERVER_NAME, server key defaults to 'frisian-mcp'."""
        data = self._run()
        assert "frisian-mcp" in data["mcpServers"]

    def test_custom_server_name(self) -> None:
        """FRISIAN_MCP_SERVER_NAME is used as the server key in mcpServers."""
        data = self._run(FRISIAN_MCP_SERVER_NAME="my-app")
        assert "my-app" in data["mcpServers"]
        assert "frisian-mcp" not in data["mcpServers"]

    def test_default_url(self) -> None:
        """Without FRISIAN_MCP_BASE_URL or --url, defaults to localhost:8000/mcp/."""
        data = self._run()
        server = next(iter(data["mcpServers"].values()))
        assert server["url"] == "http://localhost:8000/mcp/"

    def test_settings_url(self) -> None:
        """FRISIAN_MCP_BASE_URL setting is used when present."""
        data = self._run(FRISIAN_MCP_BASE_URL="https://api.example.com/mcp/")
        server = next(iter(data["mcpServers"].values()))
        assert server["url"] == "https://api.example.com/mcp/"

    def test_url_flag_overrides_settings(self) -> None:
        """--url CLI flag takes precedence over FRISIAN_MCP_BASE_URL setting."""
        buf = io.StringIO()
        with override_settings(FRISIAN_MCP_BASE_URL="https://settings.example.com/mcp/"):
            call_command("mcp_config", "--url", "https://flag.example.com/mcp/", stdout=buf)
        data = json.loads(buf.getvalue())
        server = next(iter(data["mcpServers"].values()))
        assert server["url"] == "https://flag.example.com/mcp/"

    def test_transport_is_http(self) -> None:
        """Server entry includes transport: 'http'."""
        data = self._run()
        server = next(iter(data["mcpServers"].values()))
        assert server["transport"] == "http"

    # ------------------------------------------------------------------
    # --name flag
    # ------------------------------------------------------------------

    def test_name_flag_overrides_server_key(self) -> None:
        """--name overrides the mcpServers entry key."""
        data = self._run("--name", "my-mcp")
        assert "my-mcp" in data["mcpServers"]
        assert "frisian-mcp" not in data["mcpServers"]

    def test_name_flag_overrides_settings_name(self) -> None:
        """--name takes precedence over FRISIAN_MCP_SERVER_NAME."""
        data = self._run("--name", "cli-name", FRISIAN_MCP_SERVER_NAME="settings-name")
        assert "cli-name" in data["mcpServers"]

    # ------------------------------------------------------------------
    # --token flag
    # ------------------------------------------------------------------

    def test_token_flag_embeds_bearer_header(self) -> None:
        """--token embeds Authorization: Bearer <token> in the headers dict."""
        data = self._run("--token", "mytoken123")
        server = next(iter(data["mcpServers"].values()))
        assert server["headers"]["Authorization"] == "Bearer mytoken123"

    def test_no_token_flag_omits_headers(self) -> None:
        """Without --token, the headers key is absent from the server block."""
        data = self._run()
        server = next(iter(data["mcpServers"].values()))
        assert "headers" not in server

    # ------------------------------------------------------------------
    # --client flag schemas
    # ------------------------------------------------------------------

    def test_client_claude_code_uses_type_field(self) -> None:
        """--client claude-code uses 'type: http' schema (not 'transport')."""
        data = self._run("--client", "claude-code")
        server = next(iter(data["mcpServers"].values()))
        assert server["type"] == "http"
        assert "transport" not in server

    def test_client_cursor_uses_type_field(self) -> None:
        """--client cursor uses 'type: http' schema."""
        data = self._run("--client", "cursor")
        server = next(iter(data["mcpServers"].values()))
        assert server["type"] == "http"
        assert "transport" not in server

    def test_client_claude_code_with_token(self) -> None:
        """--client claude-code with --token embeds headers."""
        data = self._run("--client", "claude-code", "--token", "tok123")
        server = next(iter(data["mcpServers"].values()))
        assert server["type"] == "http"
        assert server["headers"]["Authorization"] == "Bearer tok123"

    def test_client_claude_desktop_no_type_field(self) -> None:
        """--client claude-desktop omits 'type' field."""
        data = self._run("--client", "claude-desktop")
        server = next(iter(data["mcpServers"].values()))
        assert "type" not in server
        assert "transport" not in server
        assert "url" in server

    def test_client_claude_desktop_with_token(self) -> None:
        """--client claude-desktop with --token embeds headers."""
        data = self._run("--client", "claude-desktop", "--token", "tok")
        server = next(iter(data["mcpServers"].values()))
        assert server["headers"]["Authorization"] == "Bearer tok"

    def test_client_generic_uses_transport_field(self) -> None:
        """--client generic (default) keeps 'transport: http' for backwards compatibility."""
        data = self._run("--client", "generic")
        server = next(iter(data["mcpServers"].values()))
        assert server["transport"] == "http"
        assert "type" not in server
