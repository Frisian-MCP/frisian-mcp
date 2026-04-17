"""Minimal Django settings used by mypy, pylint, and pytest."""

SECRET_KEY = "friese-mcp-test-secret-key-not-for-production"  # noqa: S105

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "friese_mcp",
    "friese_mcp.contrib.tokens",
    "friese_mcp.contrib.oauth",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
