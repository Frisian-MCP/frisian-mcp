"""Management command: print a ready-to-paste mcpServers JSON block."""

from __future__ import annotations

import argparse
import json

from django.conf import settings
from django.core.management.base import BaseCommand

_DEFAULT_URL = "http://localhost:8000/mcp/"


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

    def handle(self, *args: object, **options: object) -> None:
        """Write the mcpServers block to stdout."""
        server_name: str = getattr(settings, "FRIESE_MCP_SERVER_NAME", "friese-mcp")
        url: str = (
            str(options["url"])
            if options.get("url")
            else getattr(settings, "FRIESE_MCP_BASE_URL", _DEFAULT_URL)
        )
        config = {
            "mcpServers": {
                server_name: {
                    "url": url,
                    "transport": "http",
                }
            }
        }
        self.stdout.write(json.dumps(config, indent=2))
