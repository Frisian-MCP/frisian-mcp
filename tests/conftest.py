"""Shared pytest fixtures for the friese-mcp test suite."""

# pylint: disable=redefined-outer-name
from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from friese_mcp.registry import ToolRegistry

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
