"""
Guard against fabricated ``_mcp_*`` request attributes in security tests (H22).

THE FAILURE MODE
----------------
Every security gate in the package reads its request state the same way::

    caps = getattr(request, "_mcp_capabilities", None)          # visibility lens
    tier = getattr(request, "_mcp_effective_tier", None)        # tier ranking
    filt = getattr(request, "_mcp_perm_entry_filter", None)     # re-authorization

``None`` is the production default and it is the *safe* one — it means "control
off / not stamped / unrestricted", and the gate falls through to its base check.
A value that is *present* flips the gate onto its restricted branch.

A bare ``MagicMock()`` request breaks this. ``getattr`` on an unset attribute of
a ``MagicMock`` does not return the default — it **auto-materialises a truthy
child mock**. So the gate reads a fabricated "stamp" that no real request ever
carried, and takes a branch the code under test would never take in production.
This produced three false security results in three days:

===========  ==============================  ==================================
Instance     Fabricated attribute            Consequence
===========  ==============================  ==================================
H7           ``_mcp_effective_tier``         a Mock ranked as a tier; 79 tests
                                             asserted against a lying fixture
H17          ``_mcp_perm_entry_filter``      the helper "could" refuse given a
                                             state production never supplies
today (#1)   ``_mcp_capabilities``           every member read invisible; the
                                             right answer from worthless evidence
===========  ==============================  ==================================

Every instrument the project trusts is blind to this: a green suite passes
because the fixture makes the assertion pass; mutation testing reads the mutant
as killed because the mock-built test dies for the wrong reason; ``mypy`` and
``pylint`` cannot see it because ``MagicMock`` is ``Any`` by construction.

THE DISCRIMINATOR
-----------------
security drew the line in H17 and it holds: **fabrication is the defect,
explicit construction is not.** A test that *deliberately* stamps
``_mcp_effective_tier="read"`` — because production populates it and a live run
proved it does — is legitimate. The two are mechanically distinguishable:

* **Explicit** (``MagicMock(_mcp_x=v)`` or ``m._mcp_x = v``) stores the value in
  the instance, so normal attribute lookup finds it and ``Mock.__getattr__``
  is **never consulted**.
* **Fabrication** (unset attribute) is the *only* case that reaches
  ``Mock.__getattr__`` and auto-creates a child mock.

So a guard that intercepts *only* the auto-materialisation of a guarded name
fires on exactly the defect and never on legitimate explicit construction.

WHY RAISING WORKS THROUGH ``getattr(request, name, None)``
----------------------------------------------------------
``getattr(obj, name, default)`` suppresses ``AttributeError`` only. Raising a
non-``AttributeError`` (``McpMockFabricationError``) from ``__getattr__``
therefore **defeats the production default swallow** and surfaces as a hard test
failure at the exact read site, naming the attribute and the gate. A spec'd
double whose out-of-spec access raises ``AttributeError`` is left alone — that
one already behaves like production (absent → ``None``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import NonCallableMock

#: The ``_mcp_*`` request attributes that gate an authorization or visibility
#: decision. Fabricating any of these steers a security branch. Kept in sync
#: with the source by :func:`test_guarded_set_matches_source` in the H22 test
#: module — this set is the contract, that test is its enforcement.
GUARDED_ATTRS: frozenset[str] = frozenset(
    {
        "_mcp_effective_tier",  # tier ranking (H7)
        "_mcp_perm_entry_filter",  # entry re-authorization (H17)
        "_mcp_capabilities",  # discovery / suggestion visibility lens (today)
        "_mcp_max_tier",  # route ceiling
        "_mcp_route_view",  # route surface (H4 re-authorization target)
    }
)


class McpMockFabricationError(AssertionError):
    """
    A test let a mock fabricate a security-relevant ``_mcp_*`` request attribute.

    Subclasses ``AssertionError`` so pytest reports it as a test failure rather
    than an error, and — importantly — it is **not** an ``AttributeError``, so
    ``getattr(request, name, None)`` in the code under test does not swallow it.
    """


_ORIGINAL_GETATTR = NonCallableMock.__getattr__


def _guarded_getattr(self: Any, name: str) -> Any:
    """Fire only when a mock auto-materialises a guarded name (fabrication)."""
    result = _ORIGINAL_GETATTR(self, name)  # AttributeError (spec) propagates as-is
    if name in GUARDED_ATTRS and isinstance(result, NonCallableMock):
        raise McpMockFabricationError(
            f"a mock fabricated request.{name!r} — this attribute gates a security "
            f"decision and its production default is None (control off / not stamped). "
            f"A bare MagicMock() makes getattr(request, {name!r}, None) return a truthy "
            f"child mock, so the gate takes a branch no real request would. Use "
            f"tests._mcp_mock_guard.mcp_request(...) for a faithful request, or set "
            f"{name!r} explicitly if production genuinely stamps it (that is legitimate)."
        )
    return result


@contextlib.contextmanager
def mock_fabrication_guard() -> Iterator[None]:
    """
    Install the guard for the duration of the block. Idempotent, reversible.

    Restores whatever was installed **when this context was entered**, not the
    unguarded original.  The session fixture installs this guard for the whole
    run, so a test that enters the context again would otherwise uninstall the
    session guard on its own exit and leave every later test unprotected — the
    guard disabled by the act of using it, which is the failure class it exists
    to catch.
    """
    previous = NonCallableMock.__getattr__
    NonCallableMock.__getattr__ = _guarded_getattr  # type: ignore[method-assign]
    try:
        yield
    finally:
        NonCallableMock.__getattr__ = previous  # type: ignore[method-assign]


class _FaithfulRequest:
    """
    A request double that behaves like a real one for ``_mcp_*`` reads.

    Only the attributes a test sets explicitly exist; everything else is genuinely
    absent, so ``getattr(request, "_mcp_x", None)`` returns ``None`` — the
    production default — instead of a fabricated mock. This is the "make the
    faithful thing the easy thing" half of the guard: reach for this instead of a
    bare ``MagicMock`` and the failure mode cannot occur in the first place.
    """

    def __init__(self, **attrs: Any) -> None:
        # ``auth``/``user`` default to the anonymous shape a real unauthenticated
        # request carries, not to a truthy mock.
        self.auth = attrs.pop("auth", None)
        self.user = attrs.pop("user", None)
        for key, value in attrs.items():
            setattr(self, key, value)


def mcp_request(**attrs: Any) -> _FaithfulRequest:
    """
    Build a faithful request double.

    Pass any ``_mcp_*`` attribute a test genuinely needs stamped (legitimate,
    per the H17 discriminator); leave the rest unset so production defaults hold.
    """
    return _FaithfulRequest(**attrs)
