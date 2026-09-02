"""Django test settings for MySQL."""

from __future__ import annotations

import os
from typing import Any

# These settings intentially extend the standard test settings module.
# pylint: disable=unused-wildcard-import,wildcard-import
from .test_settings import *  # noqa: F403

mysql_databases: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("FRISIAN_MCP_TEST_DB_NAME", "frisian_mcp"),
        "USER": os.environ.get("FRISIAN_MCP_TEST_DB_USER", "root"),
        "PASSWORD": os.environ.get("FRISIAN_MCP_TEST_DB_PASSWORD", "frisian-test"),
        "HOST": os.environ.get("FRISIAN_MCP_TEST_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("FRISIAN_MCP_TEST_DB_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
        "TEST": {
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_unicode_ci",
        },
    }
}

DATABASES = mysql_databases
