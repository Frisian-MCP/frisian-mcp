"""Create OAuth consent with a fixed-size fingerprint on fresh installations."""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_CONSENT_TABLE = "frisian_mcp_oauth_oauthauthorizeconsent"
_EXPECTED_ORPHAN_COLUMNS = (
    "id",
    "client_id",
    "redirect_uri",
    "scope",
    "created_at",
    "user_id",
)


def _remove_empty_partial_mysql_table(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Remove only the empty table left by MySQL's failed migration 0003."""
    del apps  # Required RunPython argument; this recovery uses no historical model.
    connection = schema_editor.connection
    quoted_table = connection.ops.quote_name(_CONSENT_TABLE)
    with connection.cursor() as cursor:
        table_names = connection.introspection.table_names(cursor)
        if _CONSENT_TABLE not in table_names:
            return
        if connection.vendor != "mysql":
            raise RuntimeError(f"Refusing to remove unexpected non-MySQL table {_CONSENT_TABLE!r}.")
        description = connection.introspection.get_table_description(
            cursor,
            _CONSENT_TABLE,
        )
        actual_columns = tuple(column.name for column in description)
        if actual_columns != _EXPECTED_ORPHAN_COLUMNS:
            raise RuntimeError(f"Refusing to remove {_CONSENT_TABLE!r}: unexpected columns.")
        if any(column.null_ok not in (False, 0) for column in description):
            raise RuntimeError(
                f"Refusing to remove {_CONSENT_TABLE!r}: unexpected nullable columns."
            )
        constraints = connection.introspection.get_constraints(
            cursor,
            _CONSENT_TABLE,
        )
        primary_keys = [
            tuple(details.get("columns", ()))
            for details in constraints.values()
            if details.get("primary_key")
        ]
        non_primary_constraints = [
            name for name, details in constraints.items() if not details.get("primary_key")
        ]
        if primary_keys != [("id",)] or non_primary_constraints:
            raise RuntimeError(f"Refusing to remove {_CONSENT_TABLE!r}: unexpected constraints.")
        cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")  # noqa: S608
        result = cursor.fetchone()
        if result is None or int(result[0]) != 0:
            raise RuntimeError(f"Refusing to remove non-empty table {_CONSENT_TABLE!r}.")
    schema_editor.execute(f"DROP TABLE {quoted_table}")


class Migration(migrations.Migration):
    """Replace migrations 0003-0004 for fresh installations."""

    replaces = [
        ("frisian_mcp_oauth", "0003_oauthauthorizeconsent"),
        ("frisian_mcp_oauth", "0004_consent_grant_fingerprint"),
    ]

    dependencies = [
        ("frisian_mcp_oauth", "0002_oauthclient_user"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            _remove_empty_partial_mysql_table,
            reverse_code=migrations.RunPython.noop,
            atomic=False,
        ),
        migrations.CreateModel(
            name="OAuthAuthorizeConsent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "client_id",
                    models.CharField(
                        help_text=(
                            "OAuth client_id from the authorize request at the time of consent."
                        ),
                        max_length=255,
                    ),
                ),
                (
                    "redirect_uri",
                    models.CharField(
                        help_text=(
                            "Exact redirect_uri the user approved.  Must match on subsequent "
                            "requests."
                        ),
                        max_length=2000,
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        help_text=(
                            "Permission tier at the time of consent.  Currently one of "
                            "``read`` / ``read_write`` / ``admin`` (the OAuthClient.permission "
                            "value at consent time).  Stored as a free-form string so future "
                            "scope refactors do not require a migration."
                        ),
                        max_length=64,
                    ),
                ),
                (
                    "grant_fingerprint",
                    models.CharField(
                        blank=True,
                        editable=False,
                        help_text=(
                            "SHA-256 fingerprint of the exact "
                            "(client_id, redirect_uri, scope) consent tuple."
                        ),
                        max_length=64,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        help_text="Django user who granted this consent.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="oauth_authorize_consents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "OAuth Authorize Consent",
                "verbose_name_plural": "OAuth Authorize Consents",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="oauthauthorizeconsent",
            constraint=models.UniqueConstraint(
                fields=("user", "grant_fingerprint"),
                name="frisian_mcp_oac_user_fp_uniq",
            ),
        ),
    ]
