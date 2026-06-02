"""Minimal Django settings used by mypy, pylint, and pytest."""

SECRET_KEY = "frisian-mcp-test-secret-key-not-for-production"  # noqa: S105

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",
    "rest_framework",
    "frisian_mcp",
    "frisian_mcp.contrib.tokens",
    "frisian_mcp.contrib.oauth",
    "frisian_mcp.contrib.agents",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    }
]
