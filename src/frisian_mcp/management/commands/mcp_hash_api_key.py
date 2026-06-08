"""Management command: print the HMAC-SHA256 digest for an API key."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Print the HMAC-SHA256 digest to use as a key in FRISIAN_MCP_API_KEYS."""

    help = (
        "Hash a raw API key for use in FRISIAN_MCP_API_KEYS.  "
        "FRISIAN_MCP_API_KEYS must store HMAC-SHA256 digests, not raw keys.  "
        "Pass the raw key here; store the printed digest in settings."
    )

    def add_arguments(self, parser: Any) -> None:
        """Register the positional raw_key argument."""
        parser.add_argument(
            "raw_key",
            help="The raw API key to hash.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Hash raw_key and write the digest to stdout."""
        raw_key = str(options["raw_key"])
        if not raw_key:
            raise CommandError("raw_key must not be empty.")

        from frisian_mcp.contrib.tokens.models import (  # pylint: disable=import-outside-toplevel
            _hmac_token,
        )

        digest = _hmac_token(raw_key)
        self.stdout.write(digest)
