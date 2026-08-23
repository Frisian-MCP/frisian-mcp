"""
CL-1 — SyncInvocation must report the true HTTP status for DRF exceptions.

Both broad ``except Exception`` handlers in :class:`SyncInvocation` (the one
guarding ``viewset.initial()`` and the one guarding the action call) used to
return ``ToolResult(content={"error": message})`` with no status at all, so
``views.py`` supplied its ``500`` default for *every* failure.  A not-found
retrieve therefore reported ``status_code: 500`` — and 404 and 500 mean very
different things to an agent deciding whether to retry, because a 500 invites
a retry that can never succeed.

Reading ``exc.status_code`` is not sufficient on its own.  The synthetic
invocation path calls the action method directly and never runs
``APIView.handle_exception``, so DRF's own normalisation of Django's
``Http404`` and ``PermissionDenied`` into ``NotFound``/``PermissionDenied``
never fires.  ``rest_framework.generics.get_object_or_404`` raises a bare
``Http404``, which carries neither ``.detail`` nor ``.status_code`` — so the
originally reported repro is precisely the case a ``.status_code`` lookup
alone would miss.
"""

# pylint: disable=redefined-outer-name,protected-access
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from django.test import RequestFactory
from rest_framework import exceptions as drf_exceptions, status as drf_status
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from frisian_mcp.backends.base import ToolDefinition
from frisian_mcp.backends.invocation import SyncInvocation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(rf: RequestFactory) -> Any:
    """Return a stub MCP gateway request with an authenticated mock user."""
    req = rf.post("/mcp/", content_type="application/json")
    user = MagicMock()
    user.is_authenticated = True
    user.is_superuser = True
    req.user = user
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


def _action_viewset(exc: BaseException) -> type:
    """Return a ViewSet whose ``list`` action raises *exc*."""

    class _RaisingActionViewSet(ViewSet):
        permission_classes: list[Any] = []

        def list(self, request: Any, *args: Any, **kwargs: Any) -> Response:
            raise exc

    return _RaisingActionViewSet


def _initial_viewset(exc: BaseException) -> type:
    """Return a ViewSet whose ``initial()`` hook raises *exc*."""

    class _RaisingInitialViewSet(ViewSet):
        permission_classes: list[Any] = []

        def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
            raise exc

        def list(self, request: Any, *args: Any, **kwargs: Any) -> Response:
            return Response({"ok": True})

    return _RaisingInitialViewSet


def _invoke(view_cls: type, rf: RequestFactory) -> dict[str, Any]:
    """Invoke *view_cls* and return the error content dict."""
    result = SyncInvocation().invoke(_tool(view_cls), {}, _make_request(rf))
    assert result.is_error is True
    assert isinstance(result.content, dict)
    return result.content


#: Both broad handlers must behave identically, so every case below is
#: parametrised over the two entry points that reach them.
_HANDLERS = [("action", _action_viewset), ("initial", _initial_viewset)]


# ---------------------------------------------------------------------------
# Every exception type whose reported status changes
# ---------------------------------------------------------------------------

_STATUS_CASES: list[tuple[str, BaseException, int]] = [
    # Django exceptions that DRF would normalise inside handle_exception(),
    # which the synthetic path never runs.  Neither carries a .status_code.
    ("http404", Http404("No Widget matches the given query."), drf_status.HTTP_404_NOT_FOUND),
    ("django_permission_denied", DjangoPermissionDenied("nope"), drf_status.HTTP_403_FORBIDDEN),
    # DRF APIException subclasses, which carry .status_code directly.
    ("drf_not_found", drf_exceptions.NotFound(), drf_status.HTTP_404_NOT_FOUND),
    ("drf_permission_denied", drf_exceptions.PermissionDenied(), drf_status.HTTP_403_FORBIDDEN),
    ("drf_not_authenticated", drf_exceptions.NotAuthenticated(), drf_status.HTTP_401_UNAUTHORIZED),
    (
        "drf_authentication_failed",
        drf_exceptions.AuthenticationFailed(),
        drf_status.HTTP_401_UNAUTHORIZED,
    ),
    ("drf_throttled", drf_exceptions.Throttled(wait=30), drf_status.HTTP_429_TOO_MANY_REQUESTS),
    (
        "drf_method_not_allowed",
        drf_exceptions.MethodNotAllowed("POST"),
        drf_status.HTTP_405_METHOD_NOT_ALLOWED,
    ),
    ("drf_parse_error", drf_exceptions.ParseError(), drf_status.HTTP_400_BAD_REQUEST),
    (
        "drf_unsupported_media_type",
        drf_exceptions.UnsupportedMediaType("text/plain"),
        drf_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    ),
    ("drf_not_acceptable", drf_exceptions.NotAcceptable(), drf_status.HTTP_406_NOT_ACCEPTABLE),
    # Unchanged: the DRF base class is genuinely a 500.
    (
        "drf_api_exception_base",
        drf_exceptions.APIException(),
        drf_status.HTTP_500_INTERNAL_SERVER_ERROR,
    ),
]


@pytest.mark.parametrize("handler_id,viewset_factory", _HANDLERS, ids=[h[0] for h in _HANDLERS])
@pytest.mark.parametrize("case_id,exc,expected", _STATUS_CASES, ids=[c[0] for c in _STATUS_CASES])
def test_true_status_reported(
    handler_id: str,
    viewset_factory: Any,
    case_id: str,
    exc: BaseException,
    expected: int,
    rf: RequestFactory,
) -> None:
    """Each exception type reports its own status, through both handlers."""
    content = _invoke(viewset_factory(exc), rf)
    assert (
        content.get("status_code") == expected
    ), f"{case_id} via {handler_id}: expected {expected}, got {content.get('status_code')!r}"


# ---------------------------------------------------------------------------
# The 500 fallback must not disappear
# ---------------------------------------------------------------------------

_NON_DRF: list[tuple[str, BaseException]] = [
    ("runtime_error", RuntimeError("boom")),
    ("value_error", ValueError("bad")),
    ("key_error", KeyError("missing")),
]


@pytest.mark.parametrize("handler_id,viewset_factory", _HANDLERS, ids=[h[0] for h in _HANDLERS])
@pytest.mark.parametrize("case_id,exc", _NON_DRF, ids=[c[0] for c in _NON_DRF])
def test_non_drf_exception_still_reports_500(
    handler_id: str,
    viewset_factory: Any,
    case_id: str,
    exc: BaseException,
    rf: RequestFactory,
) -> None:
    """A plain exception has no status of its own and must remain a 500."""
    content = _invoke(viewset_factory(exc), rf)
    assert (
        content.get("status_code") == drf_status.HTTP_500_INTERNAL_SERVER_ERROR
    ), f"{case_id} via {handler_id}: fallback lost, got {content.get('status_code')!r}"


# ---------------------------------------------------------------------------
# A host-declared status outside the error range cannot leak through
# ---------------------------------------------------------------------------


class _SuccessStatusAPIException(drf_exceptions.APIException):
    """A host APIException that (wrongly) declares a 2xx status."""

    status_code = 200
    default_detail = "declared success on an error"


class _NonsenseStatusAPIException(drf_exceptions.APIException):
    """A host APIException with a status outside any valid HTTP range."""

    status_code = 9999
    default_detail = "not a real status"


class _NonIntegerStatusAPIException(drf_exceptions.APIException):
    """A host APIException whose status_code is not an integer at all."""

    status_code = "418"  # type: ignore[assignment]
    default_detail = "status declared as a string"


_OUT_OF_RANGE: list[tuple[str, BaseException]] = [
    ("success_status", _SuccessStatusAPIException()),
    ("nonsense_status", _NonsenseStatusAPIException()),
    ("non_integer_status", _NonIntegerStatusAPIException()),
]


@pytest.mark.parametrize("handler_id,viewset_factory", _HANDLERS, ids=[h[0] for h in _HANDLERS])
@pytest.mark.parametrize("case_id,exc", _OUT_OF_RANGE, ids=[c[0] for c in _OUT_OF_RANGE])
def test_out_of_range_status_falls_back_to_500(
    handler_id: str,
    viewset_factory: Any,
    case_id: str,
    exc: BaseException,
    rf: RequestFactory,
) -> None:
    """An error envelope must never carry a status a caller could read as success."""
    content = _invoke(viewset_factory(exc), rf)
    assert (
        content.get("status_code") == drf_status.HTTP_500_INTERNAL_SERVER_ERROR
    ), f"{case_id} via {handler_id}: got {content.get('status_code')!r}"


# ---------------------------------------------------------------------------
# The message envelope must be unchanged by this fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler_id,viewset_factory", _HANDLERS, ids=[h[0] for h in _HANDLERS])
def test_message_envelope_unchanged(
    handler_id: str, viewset_factory: Any, rf: RequestFactory
) -> None:
    """Adding a status must not disturb the flattened ``.detail`` message."""
    content = _invoke(viewset_factory(drf_exceptions.PermissionDenied()), rf)
    assert content["error"] == "You do not have permission to perform this action."
