"""Shared pytest fixtures for the frisian-mcp test suite."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from frisian_mcp.registry import ToolRegistry
from tests._mcp_mock_guard import mock_fabrication_guard

# ---------------------------------------------------------------------------
# H22 — fabrication guard, active for the whole suite
# ---------------------------------------------------------------------------
#
# A bare ``MagicMock()`` request auto-materialises a truthy child mock for any
# unset ``_mcp_*`` attribute, so a security gate reads a "stamp" no real request
# carries and takes a branch it never would in production. This shipped three
# false results in three days (H7, H17, and the #62 review round). The suite
# is clean of it today; this keeps it clean. See ``tests/_mcp_mock_guard.py``.
#
# Session-scoped and autouse: the guard fires only on genuine fabrication of a
# guarded name, never on explicit construction, so it costs nothing to leave on
# and reds immediately if the pattern returns.


@pytest.fixture(scope="session", autouse=True)
def _guard_against_mock_fabrication() -> Generator[None, None, None]:
    """Install the H22 fabrication guard for the entire test session."""
    with mock_fabrication_guard():
        yield


# ---------------------------------------------------------------------------
# Registry fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> ToolRegistry:
    """Return a fresh, isolated ToolRegistry instance per test."""
    return ToolRegistry()


# ---------------------------------------------------------------------------
# Request fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rf() -> RequestFactory:
    """Return a Django RequestFactory."""
    return RequestFactory()


@pytest.fixture()
def anon_request(rf: RequestFactory) -> Any:
    """Return a POST request with an anonymous user."""
    request = rf.post("/mcp/", content_type="application/json")
    request.user = AnonymousUser()
    return request


@pytest.fixture()
def auth_request(rf: RequestFactory) -> Any:
    """Return a POST request with a mock authenticated user."""
    request = rf.post("/mcp/", content_type="application/json")
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    request.user = user
    return request


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def use_test_urls(settings: Any) -> Generator[None, None, None]:
    """Override ROOT_URLCONF to the test URL conf for the duration of a test."""
    settings.ROOT_URLCONF = "tests.urls"
    yield
    # pytest-django restores settings automatically; explicit cleanup not required.
