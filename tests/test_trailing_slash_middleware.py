"""Tests for ``McpTrailingSlashMiddleware`` and its auto-installation in apps.ready()."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from frisian_mcp.apps import (
    COMMON_MIDDLEWARE_PATH,
    TRAILING_SLASH_MIDDLEWARE_PATH,
    _install_trailing_slash_middleware,
)
from frisian_mcp.middleware import (
    DEFAULT_MCP_URL_PREFIX,
    McpTrailingSlashMiddleware,
    _get_mcp_url_prefix,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(request: HttpRequest) -> HttpResponse:
    """Echo the rewritten path for inspection in assertions."""
    return HttpResponse(request.path_info)


def _build(get_response: Any = None) -> McpTrailingSlashMiddleware:
    """Construct middleware with a default no-op response callable."""
    return McpTrailingSlashMiddleware(get_response or _make_response)


# ---------------------------------------------------------------------------
# TestUrlPrefixResolution
# ---------------------------------------------------------------------------


class TestUrlPrefixResolution:
    """Tests for ``_get_mcp_url_prefix`` configuration handling."""

    def test_default_prefix(self) -> None:
        """Default prefix matches the documented constant."""
        assert _get_mcp_url_prefix() == DEFAULT_MCP_URL_PREFIX

    @override_settings(FRISIAN_MCP_URL_PREFIX="/api/mcp")
    def test_custom_prefix_normalised(self) -> None:
        """Custom prefix is returned verbatim when already normalised."""
        assert _get_mcp_url_prefix() == "/api/mcp"

    @override_settings(FRISIAN_MCP_URL_PREFIX="api/mcp")
    def test_prefix_missing_leading_slash(self) -> None:
        """Leading slash is added when missing."""
        assert _get_mcp_url_prefix() == "/api/mcp"

    @override_settings(FRISIAN_MCP_URL_PREFIX="/api/mcp/")
    def test_prefix_strips_trailing_slash(self) -> None:
        """Trailing slash is stripped for comparison stability."""
        assert _get_mcp_url_prefix() == "/api/mcp"


# ---------------------------------------------------------------------------
# TestMcpTrailingSlashMiddleware
# ---------------------------------------------------------------------------


class TestMcpTrailingSlashMiddleware:
    """Tests for the HTTP request middleware itself."""

    def test_strips_trailing_slash_on_default_prefix(self) -> None:
        """``/mcp/`` is rewritten to ``/mcp`` before downstream processing."""
        request = RequestFactory().post("/mcp/", content_type="application/json")
        seen_paths: list[str] = []

        def capture(req: HttpRequest) -> HttpResponse:
            seen_paths.append(req.path_info)
            return HttpResponse("ok")

        middleware = _build(capture)
        middleware(request)
        assert seen_paths == ["/mcp"]
        assert request.path_info == "/mcp"
        assert request.path == "/mcp"

    def test_no_op_on_already_clean_mcp_path(self) -> None:
        """``/mcp`` (no trailing slash) is left untouched."""
        request = RequestFactory().post("/mcp", content_type="application/json")
        original_path = request.path
        original_info = request.path_info

        middleware = _build()
        middleware(request)
        assert request.path == original_path
        assert request.path_info == original_info

    def test_passthrough_for_non_mcp_path(self) -> None:
        """Paths outside the configured prefix are not modified."""
        request = RequestFactory().get("/api/users/")
        original_path = request.path
        original_info = request.path_info

        middleware = _build()
        middleware(request)
        assert request.path == original_path
        assert request.path_info == original_info

    def test_passthrough_for_mcp_subpath(self) -> None:
        """
        Subpaths under the prefix are passed through unchanged.

        Only the exact ``/<prefix>/`` form is normalised; ``/mcp/foo/`` and
        deeper paths preserve their trailing slash so nested URL routing is
        not broken.
        """
        request = RequestFactory().get("/mcp/foo/")
        original_path = request.path
        original_info = request.path_info

        middleware = _build()
        middleware(request)
        assert request.path == original_path
        assert request.path_info == original_info

    @override_settings(FRISIAN_MCP_URL_PREFIX="/api/mcp")
    def test_respects_custom_prefix(self) -> None:
        """A custom ``FRISIAN_MCP_URL_PREFIX`` is honoured by the middleware."""
        request = RequestFactory().post("/api/mcp/", content_type="application/json")
        middleware = _build()
        middleware(request)
        assert request.path_info == "/api/mcp"
        assert request.path == "/api/mcp"

    @override_settings(FRISIAN_MCP_URL_PREFIX="/api/mcp")
    def test_default_prefix_ignored_when_custom_set(self) -> None:
        """``/mcp/`` is not rewritten when the custom prefix is configured."""
        request = RequestFactory().post("/mcp/", content_type="application/json")
        middleware = _build()
        middleware(request)
        # ``/mcp/`` does not match ``/api/mcp/`` — must be unchanged.
        assert request.path_info == "/mcp/"

    def test_response_propagated(self) -> None:
        """The downstream response is returned unchanged."""
        sentinel = HttpResponse("sentinel-body", status=418)
        middleware = _build(lambda _req: sentinel)
        result = middleware(RequestFactory().get("/mcp/"))
        assert result is sentinel


# ---------------------------------------------------------------------------
# TestInstallTrailingSlashMiddleware
# ---------------------------------------------------------------------------


class TestInstallTrailingSlashMiddleware:
    """Tests for the auto-installation helper invoked from ``ready()``."""

    @override_settings(MIDDLEWARE=["django.middleware.common.CommonMiddleware"])
    def test_inserts_before_common_middleware(self) -> None:
        """When CommonMiddleware is present, insertion happens immediately before it."""
        from django.conf import settings

        inserted = _install_trailing_slash_middleware()
        assert inserted is True
        assert settings.MIDDLEWARE == [
            TRAILING_SLASH_MIDDLEWARE_PATH,
            COMMON_MIDDLEWARE_PATH,
        ]

    @override_settings(
        MIDDLEWARE=[
            "django.middleware.security.SecurityMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
        ]
    )
    def test_inserts_at_correct_index_in_full_stack(self) -> None:
        """Insertion respects existing entries above and below CommonMiddleware."""
        from django.conf import settings

        _install_trailing_slash_middleware()
        assert settings.MIDDLEWARE == [
            "django.middleware.security.SecurityMiddleware",
            TRAILING_SLASH_MIDDLEWARE_PATH,
            COMMON_MIDDLEWARE_PATH,
            "django.middleware.csrf.CsrfViewMiddleware",
        ]

    @override_settings(MIDDLEWARE=["django.middleware.security.SecurityMiddleware"])
    def test_prepends_when_common_middleware_missing(self) -> None:
        """Without CommonMiddleware the entry is inserted at position 0."""
        from django.conf import settings

        _install_trailing_slash_middleware()
        assert settings.MIDDLEWARE == [
            TRAILING_SLASH_MIDDLEWARE_PATH,
            "django.middleware.security.SecurityMiddleware",
        ]

    @override_settings(MIDDLEWARE=[])
    def test_inserts_into_empty_list(self) -> None:
        """An empty MIDDLEWARE list still receives the auto-installed entry."""
        from django.conf import settings

        _install_trailing_slash_middleware()
        assert settings.MIDDLEWARE == [TRAILING_SLASH_MIDDLEWARE_PATH]

    @override_settings(
        MIDDLEWARE=(
            "django.middleware.security.SecurityMiddleware",
            "django.middleware.common.CommonMiddleware",
        )
    )
    def test_handles_tuple_setting(self) -> None:
        """A tuple MIDDLEWARE setting is rebuilt as a list with our entry inserted."""
        from django.conf import settings

        _install_trailing_slash_middleware()
        assert isinstance(settings.MIDDLEWARE, list)
        assert TRAILING_SLASH_MIDDLEWARE_PATH in settings.MIDDLEWARE
        idx_ours = settings.MIDDLEWARE.index(TRAILING_SLASH_MIDDLEWARE_PATH)
        idx_common = settings.MIDDLEWARE.index(COMMON_MIDDLEWARE_PATH)
        assert idx_ours == idx_common - 1

    @override_settings(
        MIDDLEWARE=[
            "frisian_mcp.middleware.McpTrailingSlashMiddleware",
            "django.middleware.common.CommonMiddleware",
        ]
    )
    def test_idempotent_when_already_present(self) -> None:
        """Re-running the installer is a no-op when already configured."""
        from django.conf import settings

        before = list(settings.MIDDLEWARE)
        inserted = _install_trailing_slash_middleware()
        assert inserted is False
        assert settings.MIDDLEWARE == before

    @override_settings(
        MIDDLEWARE=["django.middleware.common.CommonMiddleware"]
    )
    def test_double_call_does_not_duplicate(self) -> None:
        """Calling the installer twice in a row leaves only a single entry."""
        from django.conf import settings

        _install_trailing_slash_middleware()
        _install_trailing_slash_middleware()
        assert settings.MIDDLEWARE.count(TRAILING_SLASH_MIDDLEWARE_PATH) == 1
