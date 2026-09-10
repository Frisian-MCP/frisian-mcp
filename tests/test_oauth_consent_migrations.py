"""Regression tests for OAuth consent schema migrations."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from django.conf import settings
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

APP = "frisian_mcp_oauth"
BASE = (APP, "0002_oauthclient_user")
LEGACY = (APP, "0003_oauthauthorizeconsent")
UPGRADE = (APP, "0004_consent_grant_fingerprint")
RFC8707 = (APP, "0005_rfc8707_resource_indicators")
SQUASH = (APP, "0003_squashed_0004_consent_grant_fingerprint")
CONSENT_TABLE = "frisian_mcp_oauth_oauthauthorizeconsent"


def _v1_fingerprint(client_id: str, redirect_uri: str, scope: str) -> str:
    """Match the historical 0004 fingerprint before RFC 8707 added resource."""
    digest = hashlib.sha256(b"frisian-mcp:oauth-consent:v1\0")
    for value in (client_id, redirect_uri, scope):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def _v2_fingerprint(
    client_id: str, redirect_uri: str, scope: str, resource: str | None = None
) -> str:
    """Match the RFC 8707 migration's null-safe resource fingerprint."""
    digest = hashlib.sha256(b"frisian-mcp:oauth-consent:v2\0")
    for value in (client_id, redirect_uri, scope, resource):
        if value is None:
            digest.update(b"\xff")
            continue
        encoded = value.encode("utf-8")
        digest.update(b"\x00")
        digest.update(len(encoded).to_bytes(4, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def _executor_without_replacements() -> MigrationExecutor:
    """Return an executor exposing the original migrations."""
    executor = MigrationExecutor(connection)
    executor.loader = MigrationLoader(
        connection,
        replace_migrations=False,
    )
    return executor


@pytest.mark.django_db(transaction=True)
def test_original_0003_upgrades_existing_consents() -> None:
    """Upgrade existing consent rows through the original 0004 migration."""
    if connection.vendor not in {"sqlite", "postgresql"}:
        pytest.skip("The intentionally broken legacy 0003 cannot be created on MySQL.")

    try:
        MigrationExecutor(connection).migrate([BASE])

        legacy_executor = _executor_without_replacements()
        legacy_state = legacy_executor.migrate([LEGACY])
        legacy_apps = legacy_state.apps

        applied = set(MigrationRecorder(connection).applied_migrations())
        assert LEGACY in applied
        assert UPGRADE not in applied
        assert SQUASH not in applied

        user_app, user_name = settings.AUTH_USER_MODEL.split(".", 1)
        user_model: Any = legacy_apps.get_model(user_app, user_name)
        consent_model: Any = legacy_apps.get_model(
            APP,
            "OAuthAuthorizeConsent",
        )

        user = user_model.objects.create(username="issue72-migration-user")
        first = consent_model.objects.create(
            user_id=user.pk,
            client_id="client-a",
            redirect_uri="https://one.example/callback",
            scope="read",
        )
        second = consent_model.objects.create(
            user_id=user.pk,
            client_id="client-a",
            redirect_uri="https://two.example/callback",
            scope="read",
        )

        assert not hasattr(first, "grant_fingerprint")
        assert not hasattr(second, "grant_fingerprint")

        first_pk = first.pk
        second_pk = second.pk

        upgrade_executor = MigrationExecutor(connection)

        assert LEGACY in upgrade_executor.loader.graph.nodes
        assert UPGRADE in upgrade_executor.loader.graph.nodes
        assert SQUASH not in upgrade_executor.loader.graph.nodes

        plan = [
            ((migration.app_label, migration.name), backwards)
            for migration, backwards in upgrade_executor.migration_plan([UPGRADE])
        ]
        assert (UPGRADE, False) in plan
        assert all(migration != SQUASH for migration, _backwards in plan)

        final_state = upgrade_executor.migrate([UPGRADE])
        upgrade_executor.check_replacements()

        migrated_consent_model: Any = final_state.apps.get_model(
            APP,
            "OAuthAuthorizeConsent",
        )
        migrated = list(
            migrated_consent_model.objects.filter(
                pk__in=[first_pk, second_pk],
            ).order_by("pk")
        )

        assert len(migrated) == 2
        assert migrated[0].redirect_uri == "https://one.example/callback"
        assert migrated[1].redirect_uri == "https://two.example/callback"
        assert migrated[0].grant_fingerprint == _v1_fingerprint(
            "client-a",
            "https://one.example/callback",
            "read",
        )
        assert migrated[1].grant_fingerprint == _v1_fingerprint(
            "client-a",
            "https://two.example/callback",
            "read",
        )

        applied = set(MigrationRecorder(connection).applied_migrations())
        assert {LEGACY, UPGRADE, SQUASH} <= applied

        rfc8707_executor = MigrationExecutor(connection)
        rfc8707_state = rfc8707_executor.migrate([RFC8707])
        rfc8707_consent_model: Any = rfc8707_state.apps.get_model(APP, "OAuthAuthorizeConsent")
        rfc8707_rows = list(
            rfc8707_consent_model.objects.filter(pk__in=[first_pk, second_pk]).order_by("pk")
        )
        assert [row.resource for row in rfc8707_rows] == [None, None]
        assert [row.grant_fingerprint for row in rfc8707_rows] == [
            _v2_fingerprint("client-a", "https://one.example/callback", "read"),
            _v2_fingerprint("client-a", "https://two.example/callback", "read"),
        ]

    finally:
        restore_executor = MigrationExecutor(connection)
        restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())
        restore_executor.check_replacements()


@pytest.mark.django_db(transaction=True)
def test_mysql_recovers_table_left_by_failed_original_0003() -> None:
    """Issue #72: MySQL recovers the empty table left by failed original 0003."""
    if connection.vendor != "mysql":
        pytest.skip("MySQL non-transactional DDL creates this recovery state.")

    try:
        base_executor = MigrationExecutor(connection)
        _ = base_executor.migrate([BASE])

        legacy_executor = _executor_without_replacements()
        with pytest.raises(OperationalError) as error:
            _ = legacy_executor.migrate([LEGACY])

        assert error.value.args
        assert error.value.args[0] == 1071

        applied = set(MigrationRecorder(connection).applied_migrations())
        assert LEGACY not in applied
        assert UPGRADE not in applied
        assert SQUASH not in applied

        with connection.cursor() as cursor:
            assert CONSENT_TABLE in connection.introspection.table_names(cursor)

            description = connection.introspection.get_table_description(
                cursor,
                CONSENT_TABLE,
            )
            assert tuple(column.name for column in description) == (
                "id",
                "client_id",
                "redirect_uri",
                "scope",
                "created_at",
                "user_id",
            )

            quoted_table = connection.ops.quote_name(CONSENT_TABLE)
            cursor.execute(f"SELECT COUNT(*) FROM {quoted_table}")  # noqa: S608
            result = cursor.fetchone()
            assert result is not None
            assert int(result[0]) == 0

        recovery_executor = MigrationExecutor(connection)
        assert SQUASH in recovery_executor.loader.graph.nodes

        _ = recovery_executor.migrate([SQUASH])
        recovery_executor.check_replacements()

        with connection.cursor() as cursor:
            description = connection.introspection.get_table_description(
                cursor,
                CONSENT_TABLE,
            )
            columns = {column.name: column for column in description}

            assert "grant_fingerprint" in columns
            assert columns["grant_fingerprint"].null_ok in (False, 0)

            constraints = connection.introspection.get_constraints(
                cursor,
                CONSENT_TABLE,
            )
            assert "frisian_mcp_oac_user_fp_uniq" in constraints
            assert "frisian_mcp_oac_unique_grant" not in constraints
            assert tuple(constraints["frisian_mcp_oac_user_fp_uniq"]["columns"]) == (
                "user_id",
                "grant_fingerprint",
            )

            cursor.execute(
                """
                SELECT TABLE_COLLATION, ROW_FORMAT
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                [CONSENT_TABLE],
            )
            table_status = cursor.fetchone()
            assert table_status is not None
            assert table_status[0] == "utf8mb4_unicode_ci"
            assert str(table_status[1]).lower() == "dynamic"

        applied = set(MigrationRecorder(connection).applied_migrations())
        assert {LEGACY, UPGRADE, SQUASH} <= applied

    finally:
        restore_executor = MigrationExecutor(connection)
        _ = restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())
        restore_executor.check_replacements()
