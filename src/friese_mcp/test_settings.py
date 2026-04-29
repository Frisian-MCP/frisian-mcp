"""Minimal Django settings used by mypy, pylint, and pytest."""

SECRET_KEY = "friese-mcp-test-secret-key-not-for-production"  # noqa: S105

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",
    "rest_framework",
    "friese_mcp",
    "friese_mcp.contrib.tokens",
    "friese_mcp.contrib.oauth",
    "friese_mcp.contrib.agents",
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
