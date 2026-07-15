"""Settings for the CI ``mcp_doctor --strict`` gate (V11-21).

This is the representative *valid* configuration the pipeline runs the doctor
against.  It is deliberately minimal and deliberately clean: ``mcp_doctor
--strict`` must exit 0 here, so the module doubles as the fixture that proves
the gate is green on a healthy host — and, by construction, red the moment the
doctor cannot run or reports an error-level / LOUD finding.

Why a dedicated module and not :mod:`frisian_mcp.test_settings`:

* ``test_settings`` installs ``django.contrib.admin`` without the middleware,
  session, and message wiring the admin requires, so Django's own system
  checks fail on it — the doctor never gets to run.  The doctor gate needs a
  configuration that passes ``manage.py check`` first.
* ``test_settings`` has no ``FRISIAN_MCP_ROUTES``, so it exercises only the
  legacy mount.  This module sets a single valid route so the gate exercises
  the per-route path (``_check_route_surface`` + ``_check_gateway_mounted`` +
  the forced discovery) — the surface V11-18 and V11-19 fixed.

Kept intentionally free of error-level triggers so the gate is stable, not
flaky:

* ``allow_list=['*']`` is clean against any registry — populated or empty.  An
  empty registry yields an empty ``allow_union``, so it cannot trip the W008
  net-empty LOUD (W008 fires only when a non-empty selection is fully denied),
  and the wildcard is exempt from the W110/W111 per-entry findings.
* The gateway is intentionally open here, so ``FRISIAN_MCP_ALLOW_UNAUTHENTICATED``
  is set to keep the W001 "empty permission classes" advisory from firing.
"""

SECRET_KEY = "frisian-mcp-ci-doctor-not-for-production"  # noqa: S105

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "frisian_mcp",
    "frisian_mcp.contrib.tokens",
    "frisian_mcp.contrib.oauth",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ROOT_URLCONF = "frisian_mcp._ci_doctor_urls"

#: A single valid per-route surface.  Exercises the per-route path in the
#: doctor while producing no error-level or LOUD findings.
FRISIAN_MCP_ROUTES = {
    "default": {"path": "mcp", "allow_list": ["*"]},
}

#: The route above is deliberately open; declare it so the doctor's W001
#: "empty permission classes" advisory does not fire on this fixture.
FRISIAN_MCP_ALLOW_UNAUTHENTICATED = True
