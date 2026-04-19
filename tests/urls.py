"""URL configuration used by the test suite."""

# pylint: disable=abstract-method
from __future__ import annotations

from django.urls import include, path
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
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


# ---------------------------------------------------------------------------
# Serializers for ContextDependentViewSet
# ---------------------------------------------------------------------------


class CreateContextSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer used for create actions."""

    title = serializers.CharField(help_text="Resource title")


class UpdateContextSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Serializer used for update actions."""

    title = serializers.CharField(help_text="Resource title")
    status = serializers.CharField(help_text="Resource status")


class ContextDependentViewSet(ViewSet):
    """ViewSet whose get_serializer_class() inspects self.request.method."""

    def get_serializer_class(self) -> type[serializers.Serializer]:
        """Return serializer based on request method."""
        # Accessing self.request.method reproduces the AttributeError that
        # occurred when _schema_from_viewset set viewset.request = None.
        if self.request.method == "POST":  # type: ignore[union-attr]
            return CreateContextSerializer
        return UpdateContextSerializer

    def create(self, request: Request) -> Response:
        """Create a context-dependent resource."""
        return Response({}, status=201)

    def update(self, request: Request, pk: str | None = None) -> Response:
        """Update a context-dependent resource."""
        return Response({"pk": pk})


# ---------------------------------------------------------------------------
# Stub DjangoFilterBackend (avoids hard dependency on django-filter in tests)
# ---------------------------------------------------------------------------


class _StubFilter:
    """Minimal filter stub that exposes a label attribute."""

    def __init__(self, label: str) -> None:
        """Set the filter label."""
        self.label = label


class _StubFilterSet:
    """Minimal FilterSet stub with base_filters dict."""

    base_filters = {
        "status": _StubFilter("Filter by status"),
        "category": _StubFilter("Filter by category"),
    }


# Stub class whose __name__ == "DjangoFilterBackend" so that the discovery
# backend detects it without requiring the real django-filter package.
DjangoFilterBackend = type("DjangoFilterBackend", (), {})


# ---------------------------------------------------------------------------
# ViewSets for filter backend introspection tests
# ---------------------------------------------------------------------------


class SearchableViewSet(ViewSet):
    """ViewSet with SearchFilter for introspection tests."""

    filter_backends = [SearchFilter]
    search_fields = ["name", "email"]

    def list(self, request: Request) -> Response:
        """List searchable resources."""
        return Response([])


class OrderableViewSet(ViewSet):
    """ViewSet with OrderingFilter and explicit ordering_fields."""

    filter_backends = [OrderingFilter]
    ordering_fields = ["name", "created_at"]

    def list(self, request: Request) -> Response:
        """List orderable resources."""
        return Response([])


class FullyFilteredViewSet(ViewSet):
    """ViewSet with SearchFilter, OrderingFilter, and a stub DjangoFilterBackend."""

    filter_backends = [SearchFilter, OrderingFilter, DjangoFilterBackend]
    search_fields = ["name"]
    ordering_fields = ["name", "price"]
    filterset_fields = ["status", "category"]

    def list(self, request: Request) -> Response:
        """List fully-filtered resources."""
        return Response([])


class FilterSetClassViewSet(ViewSet):
    """ViewSet that uses filterset_class instead of filterset_fields."""

    filter_backends = [DjangoFilterBackend]
    filterset_class = _StubFilterSet

    def list(self, request: Request) -> Response:
        """List filterset-class resources."""
        return Response([])


class TypedActionViewSet(ViewSet):
    """ViewSet with a typed custom GET action for signature introspection tests."""

    @action(detail=False, methods=["get"])
    def export(self, request: Request, fmt: str = "csv", limit: int = 100) -> Response:
        """Export resources in the requested format."""
        return Response({"fmt": fmt, "limit": limit})

    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        """Return a resource summary with no extra parameters."""
        return Response({})


class LimitedViewSet(ViewSet):
    """ViewSet that exposes only the list action via mcp_include_actions."""

    mcp_include_actions = ["list"]

    def list(self, request: Request) -> Response:
        """List limited resources."""
        return Response([])

    def create(self, request: Request) -> Response:
        """Create a limited resource."""
        return Response({}, status=201)


class ExcludeDestroyViewSet(ViewSet):
    """ViewSet that hides destroy via mcp_exclude_actions."""

    mcp_exclude_actions = ["destroy"]

    def list(self, request: Request) -> Response:
        """List exclude-destroy resources."""
        return Response([])

    def destroy(self, request: Request, _pk: str | None = None) -> Response:
        """Delete an exclude-destroy resource."""
        return Response(status=204)


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
_searchable_list = SearchableViewSet.as_view({"get": "list"})
_orderable_list = OrderableViewSet.as_view({"get": "list"})
_fullfiltered_list = FullyFilteredViewSet.as_view({"get": "list"})
_filtersetclass_list = FilterSetClassViewSet.as_view({"get": "list"})
_limited_list = LimitedViewSet.as_view({"get": "list", "post": "create"})
_exclude_destroy_list = ExcludeDestroyViewSet.as_view({"get": "list"})
_exclude_destroy_detail = ExcludeDestroyViewSet.as_view({"delete": "destroy"})

urlpatterns = [
    path("api/users/", _user_list, name="user-list"),
    path("api/users/<pk>/", _user_detail, name="user-detail"),
    path("api/users/export/", _user_export, name="user-export"),
    path("api/users/private/", _user_private, name="user-private"),
    path("api/secure/", _secure_list, name="secure-list"),
    path("api/ignored/", _ignored_list, name="ignored-list"),
    path("api/searchable/", _searchable_list, name="searchable-list"),
    path("api/orderable/", _orderable_list, name="orderable-list"),
    path("api/fullfiltered/", _fullfiltered_list, name="fullfiltered-list"),
    path("api/filtersetclass/", _filtersetclass_list, name="filtersetclass-list"),
    path("api/limited/", _limited_list, name="limited-list"),
    path("api/excludedestroy/", _exclude_destroy_list, name="excludedestroy-list"),
    path("api/excludedestroy/<pk>/", _exclude_destroy_detail, name="excludedestroy-detail"),
    path("mcp/", include("friese_mcp.urls")),
]
