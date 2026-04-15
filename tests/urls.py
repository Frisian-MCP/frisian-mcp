"""URL configuration used by the test suite."""

# pylint: disable=abstract-method
from __future__ import annotations

from django.urls import include, path
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from friese_mcp.decorators import mcp_ignore

# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class UserSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer for the test User resource."""

    name = serializers.CharField(help_text="Full name")
    email = serializers.EmailField(help_text="Email address")
    age = serializers.IntegerField(required=False)


# ---------------------------------------------------------------------------
# ViewSets
# ---------------------------------------------------------------------------


class UserViewSet(ViewSet):
    """Minimal ViewSet representing a user resource."""

    serializer_class = UserSerializer

    def get_serializer_class(self) -> type[UserSerializer]:
        """Return the serializer class."""
        return UserSerializer

    def list(self, request: Request) -> Response:
        """List all users."""
        return Response([])

    def create(self, request: Request) -> Response:
        """Create a new user."""
        return Response({"created": True}, status=201)

    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        """Retrieve a single user."""
        return Response({"pk": pk})

    def update(self, request: Request, pk: str | None = None) -> Response:
        """Update a user."""
        return Response({"pk": pk, "updated": True})

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        """Apply a partial update to a user."""
        return Response({"pk": pk, "patched": True})

    def destroy(self, request: Request, _pk: str | None = None) -> Response:
        """Delete a user by pk."""
        return Response(status=204)

    @action(detail=False, methods=["get"])
    def export(self, request: Request) -> Response:
        """Export users — public custom action."""
        return Response({"format": "csv"})

    @mcp_ignore
    @action(detail=False, methods=["get"])
    def private_action(self, request: Request) -> Response:
        """Exclude this action from MCP discovery."""
        return Response({"secret": True})


class DenyAll(BasePermission):
    """Permission class that always denies."""

    def has_permission(self, request: Request, view: object) -> bool:
        """Deny every request."""
        return False


class SecureViewSet(ViewSet):
    """ViewSet protected by IsAuthenticated."""

    permission_classes = [IsAuthenticated]

    def list(self, request: Request) -> Response:
        """List secure resources."""
        return Response([])


@mcp_ignore
class IgnoredViewSet(ViewSet):
    """ViewSet excluded from MCP discovery at the class level."""

    def list(self, request: Request) -> Response:
        """List ignored resources."""
        return Response([])


# ---------------------------------------------------------------------------
# URL patterns (manual wiring — no DefaultRouter required)
# ---------------------------------------------------------------------------

_user_list = UserViewSet.as_view({"get": "list", "post": "create"})
_user_detail = UserViewSet.as_view(
    {
        "get": "retrieve",
        "put": "update",
        "patch": "partial_update",
        "delete": "destroy",
    }
)
_user_export = UserViewSet.as_view({"get": "export"})
_user_private = UserViewSet.as_view({"get": "private_action"})
_secure_list = SecureViewSet.as_view({"get": "list"})
_ignored_list = IgnoredViewSet.as_view({"get": "list"})

urlpatterns = [
    path("api/users/", _user_list, name="user-list"),
    path("api/users/<pk>/", _user_detail, name="user-detail"),
    path("api/users/export/", _user_export, name="user-export"),
    path("api/users/private/", _user_private, name="user-private"),
    path("api/secure/", _secure_list, name="secure-list"),
    path("api/ignored/", _ignored_list, name="ignored-list"),
    path("mcp/", include("friese_mcp.urls")),
]
