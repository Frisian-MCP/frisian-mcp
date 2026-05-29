"""Management command: print a ready-to-paste mcpServers JSON block."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

_DEFAULT_URL = "http://localhost:8000/mcp/"

_CLIENT_CHOICES = ["claude-code", "cursor", "claude-desktop", "generic"]

_CLAUDE_DESKTOP_CONFIG_PATH = {
    "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
    "win32": r"%APPDATA%\Claude\claude_desktop_config.json",
    "linux": "~/.config/claude/claude_desktop_config.json",
}


def _server_block(url: str, client: str, token: str | None) -> dict[str, Any]:
    """
    Build the per-server config block for the given *client* type.

    Schema:
    - ``claude-code`` / ``cursor``: ``{"type": "http", "url": ..., ["headers": {...}]}``
    - ``claude-desktop``:           ``{"url": ..., ["headers": {...}]}``
    - ``generic``:                  ``{"url": ..., "transport": "http", ["headers": {...}]}``
    """
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if client in ("claude-code", "cursor"):
        block: dict[str, Any] = {"type": "http", "url": url}
        if headers:
            block["headers"] = headers
        return block

    if client == "claude-desktop":
        block = {"url": url}
        if headers:
            block["headers"] = headers
        return block

    # generic
    block = {"url": url, "transport": "http"}
    if headers:
        block["headers"] = headers
    return block


class Command(BaseCommand):
    """Print an mcpServers JSON block for Claude Desktop, Cursor, or any MCP client."""

    help = "Print a ready-to-paste mcpServers JSON block for Claude Desktop / Cursor."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Register CLI arguments."""
        parser.add_argument(
            "--url",
            default=None,
            help=(
                "MCP endpoint URL. Defaults to FRIESE_MCP_BASE_URL setting "
                f"or {_DEFAULT_URL!r} if neither is set."
            ),
        )
        parser.add_argument(
            "--client",
            choices=_CLIENT_CHOICES,
            default="generic",
            help=(
                "Target MCP client. Controls the output schema. "
                "Choices: claude-code, cursor, claude-desktop, generic (default)."
            ),
        )
        parser.add_argument(
            "--token",
            default=None,
            metavar="VALUE",
            help="Bearer token to embed as an Authorization header in the config.",
        )
        parser.add_argument(
            "--name",
            default=None,
            metavar="KEY",
            help=(
                "Override the mcpServers key for this entry. "
                "Defaults to FRIESE_MCP_SERVER_NAME setting or 'friese-mcp'."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Write the mcpServers block to stdout."""
        server_name: str = (
            str(options["name"])
            if options.get("name")
            else getattr(settings, "FRIESE_MCP_SERVER_NAME", "friese-mcp")
        )
        url: str = (
            str(options["url"])
            if options.get("url")
            else getattr(settings, "FRIESE_MCP_BASE_URL", _DEFAULT_URL)
        )
        client: str = str(options.get("client") or "generic")
        token: str | None = str(options["token"]) if options.get("token") else None

        config = {
            "mcpServers": {
                server_name: _server_block(url, client, token),
            }
        }
        self.stdout.write(json.dumps(config, indent=2))

        if client == "claude-desktop":
            platform = sys.platform
            config_path = _CLAUDE_DESKTOP_CONFIG_PATH.get(platform, "claude_desktop_config.json")
            self.stderr.write(
                f"Paste the mcpServers block into your Claude Desktop config: {config_path}"
            )
