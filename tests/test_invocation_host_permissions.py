"""
PKG-28 — SyncInvocation bypasses host model permission gate.

Without the fix, host apps that use DjangoObjectPermissions (or any subclass,
such as Nautobot's TokenPermissions) as their global DEFAULT_PERMISSION_CLASSES
would return 403 for every non-superuser token that doesn't have an
ObjectPermission configured per model — impractical for large surfaces.

The fix sets ``viewset._ignore_model_permissions = True`` before calling
``initial()``, which tells DjangoObjectPermissions.has_permission() to return
True unconditionally, making the MCP tier system the primary access gate.
Queryset restriction and non-DjangoObjectPermissions permission classes still
run normally.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

import unittest.mock
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from rest_framework.permissions import BasePermission, DjangoObjectPermissions, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from frisian_mcp.backends.base import ToolDefinition
from frisian_mcp.backends.invocation import SyncInvocation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(rf: RequestFactory, *, authenticated: bool = True) -> Any:
    """Return a stub MCP gateway request with a mock user."""
    req = rf.post("/mcp/", content_type="application/json")
    if authenticated:
        user = MagicMock()
        user.is_authenticated = True
        user.is_superuser = False
        # Simulate no ObjectPermissions configured: has_perms always False.
        user.has_perms = MagicMock(return_value=False)
        user.get_all_permissions = MagicMock(return_value=set())
        req.user = user
    else:
        req.user = AnonymousUser()
    req.auth = None
    return req


def _tool(view_cls: type, action: str = "list") -> ToolDefinition:
    return ToolDefinition(
        name=f"stub_{action}",
        description="stub",
        input_schema={"type": "object", "properties": {}},
        permission_classes=(),
        source="auto",
        view_class=view_cls,
        action=action,
        permission_tier="read",
    )


# ---------------------------------------------------------------------------
# ViewSets for the three test scenarios
# ---------------------------------------------------------------------------


class _DjangoObjectPermissionsViewSet(ViewSet):
    """
    ViewSet that uses DjangoObjectPermissions.

    DjangoObjectPermissions is the base class that Nautobot's TokenPermissions
    extends.  Without the fix, any authenticated user without has_perms()
    returning True for this model would get 403.
    """

    permission_classes = [DjangoObjectPermissions]

    # Provide a queryset so DjangoObjectPermissions can derive the model name.
    queryset = MagicMock()
    queryset.model = MagicMock()
    queryset.model._meta = MagicMock()
    queryset.model._meta.app_label = "myapp"
    queryset.model._meta.model_name = "mymodel"

    def get_queryset(self) -> Any:
        return self.__class__.queryset

    def list(self, request: Any) -> Response:
        return Response({"count": 0, "results": []})


class _IsAuthenticatedViewSet(ViewSet):
    """
    ViewSet that uses IsAuthenticated (NOT DjangoObjectPermissions).

    _ignore_model_permissions must NOT bypass this check — unauthenticated
    callers must still be denied.
    """

    permission_classes = [IsAuthenticated]

    def list(self, request: Any) -> Response:
        return Response({"count": 0, "results": []})


class _CustomNonDjangoPermission(BasePermission):
    """A custom permission class that always denies. Not a DjangoObjectPermissions subclass."""

    def has_permission(self, request: Any, view: Any) -> bool:
        return False


class _CustomPermissionViewSet(ViewSet):
    """ViewSet with a custom (non-DjangoObjectPermissions) permission that always denies."""

    permission_classes = [_CustomNonDjangoPermission]

    def list(self, request: Any) -> Response:
        return Response({"count": 0, "results": []})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rf() -> RequestFactory:
    return RequestFactory()


# ---------------------------------------------------------------------------
# Tests: _ignore_model_permissions bypasses DjangoObjectPermissions
# ---------------------------------------------------------------------------


class TestIgnoreModelPermissions:
    """SyncInvocation sets _ignore_model_permissions=True before initial()."""

    def test_flag_set_on_viewset_before_initial(self, rf: RequestFactory) -> None:
        """
        _ignore_model_permissions=True is set before initial() runs.

        DjangoObjectPermissions.has_permission() returns True regardless of
        user.has_perms().
        """
        req = _make_request(rf)
        invocation = SyncInvocation()

        result = invocation.invoke(
            _tool(_DjangoObjectPermissionsViewSet), {}, req
        )

        assert result.is_error is False
        assert result.content == {"count": 0, "results": []}

    def test_non_superuser_no_object_permission_gets_200_not_403(
        self, rf: RequestFactory
    ) -> None:
        """
        Non-superuser with has_perms()=False gets 200, not 403.

        No ObjectPermission configured — previously would return 403.
        """
        req = _make_request(rf, authenticated=True)
        # Confirm the mock returns False for any permission check.
        assert req.user.has_perms(["myapp.view_mymodel"]) is False

        invocation = SyncInvocation()
        result = invocation.invoke(
            _tool(_DjangoObjectPermissionsViewSet), {}, req
        )

        assert result.is_error is False

    def test_result_is_not_permission_denied(self, rf: RequestFactory) -> None:
        """The result content does not contain an error key about permissions."""
        req = _make_request(rf, authenticated=True)
        invocation = SyncInvocation()
        result = invocation.invoke(
            _tool(_DjangoObjectPermissionsViewSet), {}, req
        )
        content = result.content
        if isinstance(content, dict):
            error_str = str(content.get("error", "")).lower()
            assert "error" not in content or "permission" not in error_str


# ---------------------------------------------------------------------------
# Tests: non-DjangoObjectPermissions classes still run
# ---------------------------------------------------------------------------


class TestNonDjangoPermissionsStillEnforced:
    """IsAuthenticated and other non-DjangoObjectPermissions checks still fire."""

    def test_unauthenticated_denied_by_is_authenticated(
        self, rf: RequestFactory
    ) -> None:
        """
        AnonymousUser is still denied by IsAuthenticated.

        _ignore_model_permissions=True does not affect IsAuthenticated because
        it does not extend DjangoObjectPermissions.
        """
        req = _make_request(rf, authenticated=False)
        invocation = SyncInvocation()
        result = invocation.invoke(_tool(_IsAuthenticatedViewSet), {}, req)
        assert result.is_error is True

    def test_authenticated_user_passes_is_authenticated(
        self, rf: RequestFactory
    ) -> None:
        """Authenticated user passes IsAuthenticated and gets a result."""
        req = _make_request(rf, authenticated=True)
        invocation = SyncInvocation()
        result = invocation.invoke(_tool(_IsAuthenticatedViewSet), {}, req)
        assert result.is_error is False

    def test_custom_non_django_permission_still_denies(
        self, rf: RequestFactory
    ) -> None:
        """
        Custom non-DjangoObjectPermissions permission classes still run.

        _ignore_model_permissions only affects DjangoObjectPermissions and its
        subclasses; other permission classes can still deny the request.
        """
        req = _make_request(rf, authenticated=True)
        invocation = SyncInvocation()
        result = invocation.invoke(_tool(_CustomPermissionViewSet), {}, req)
        assert result.is_error is True


# ---------------------------------------------------------------------------
# Tests: FRISIAN_MCP_SERVICE_ACCOUNT_USER substitution
# ---------------------------------------------------------------------------


class TestServiceAccountUser:
    """
    Service account user substitution tests.

    When FRISIAN_MCP_SERVICE_ACCOUNT_USER is configured and the incoming request
    has an anonymous user, _resolve_effective_user substitutes the service account
    so that host ViewSets using IsAuthenticated are satisfied.
    """

    def test_anonymous_substituted_by_service_account(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """Anonymous request is substituted with the service account user."""
        service_user = MagicMock()
        service_user.is_authenticated = True
        service_user.username = "mcp-service"

        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "mcp-service"

        with unittest.mock.patch(
            "frisian_mcp.backends.invocation.SyncInvocation._resolve_effective_user",
            return_value=service_user,
        ):
            req = _make_request(rf, authenticated=False)
            invocation = SyncInvocation()
            result = invocation.invoke(_tool(_IsAuthenticatedViewSet), {}, req)

        assert result.is_error is False

    def test_authenticated_user_not_replaced(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """When the request already has an authenticated user, it is never replaced."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "some-service"

        req = _make_request(rf, authenticated=True)
        original_user = req.user

        resolved = SyncInvocation._resolve_effective_user(req)

        assert resolved is original_user

    def test_service_account_missing_falls_back_to_anonymous(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """When the named user does not exist, _resolve_effective_user returns AnonymousUser."""
        settings.FRISIAN_MCP_SERVICE_ACCOUNT_USER = "nonexistent-user"

        class _FakeDoesNotExistError(Exception):
            pass

        mock_model = MagicMock()
        # DoesNotExist must be a real exception class so it can be raised and caught.
        mock_model.DoesNotExist = _FakeDoesNotExistError
        mock_model.objects.get.side_effect = _FakeDoesNotExistError

        with unittest.mock.patch(
            "django.contrib.auth.get_user_model", return_value=mock_model
        ):
            req = _make_request(rf, authenticated=False)
            resolved = SyncInvocation._resolve_effective_user(req)

        assert isinstance(resolved, AnonymousUser)

    def test_no_setting_returns_anonymous_unchanged(
        self, rf: RequestFactory, settings: Any
    ) -> None:
        """With no FRISIAN_MCP_SERVICE_ACCOUNT_USER set, anonymous user passes through."""
        if hasattr(settings, "FRISIAN_MCP_SERVICE_ACCOUNT_USER"):
            delattr(settings, "FRISIAN_MCP_SERVICE_ACCOUNT_USER")

        req = _make_request(rf, authenticated=False)
        resolved = SyncInvocation._resolve_effective_user(req)

        assert isinstance(resolved, AnonymousUser)
