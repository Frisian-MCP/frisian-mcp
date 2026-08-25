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
# Testing from an isolated copy of the tree — read this first
# ---------------------------------------------------------------------------
#
# The package is installed editable, so site-packages holds a ``.pth`` file
# containing the ABSOLUTE path of this checkout's ``src/``.  ``import
# frisian_mcp`` therefore resolves to the LIVE tree no matter what directory
# you run pytest from.  Copying the repo somewhere else and running the suite
# there does NOT test the copy — it re-tests the original, from inside a
# directory that looks isolated.
#
# Set ``PYTHONPATH=<copy>/src`` to actually exercise a copy.  Measured, both
# directions, with a marker added to the copy only:
#
#     cd <copy> && python -c "import frisian_mcp; print(frisian_mcp.__file__)"
#       -> <original>/src/frisian_mcp/__init__.py   (marker absent)
#     cd <copy> && PYTHONPATH=<copy>/src \
#         python -c "import frisian_mcp; print(frisian_mcp.__file__)"
#       -> <copy>/src/frisian_mcp/__init__.py       (marker present)
#
# ``print()`` is load-bearing: ``python -c`` evaluates an expression but does
# not echo it the way the REPL does, so the bare attribute access above used to
# produce a blank line -- a check that silently answered nothing, in the note
# written to stop this trap costing anyone else a day.
#
# What makes it expensive is that the run is a HYBRID, not simply "the original
# re-tested": pytest collects the tests from the COPY (they are cwd-relative)
# while ``import frisian_mcp`` resolves to the ORIGINAL's ``src``.  So you are
# running the copy's tests against the original's source.  Measured — a test
# present only in the copy was collected, ran, and failed, with
# ``frisian_mcp.__file__`` pointing at the original.
#
# **Neither colour is self-certifying.**  A green may mean the fix works, or
# that the original was already green.  A red may mean the change is wrong, or
# merely that the copy's tests do not hold against the original's source —
# which is what you would expect when the change under test is the thing that
# is missing.  A red does NOT prove the isolation worked.
#
# So confirm which tree was imported BEFORE interpreting either result.
# Cheapest check is to print ``frisian_mcp.__file__`` first.
#
# This has cost real debugging time more than once.

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
