"""
Tests for V11-11 — capabilities resolved via ``has_perm()``, not enumeration.

The property under test is the one that would have caught the 2026-07-13 live
leak automatically:

    for every registered tool:  discovered  ⟺  invocable

On the hosts this package targets, "invocable" means the host's per-user
queryset restriction returns rows, and that restriction evaluates the *same*
predicate as ``has_perm`` (``superuser ∨ exempt ∨ perm ∈ get_all_permissions``).
So asserting discovery against ``has_perm`` asserts it against the real data
boundary — in both directions:

* ``has_perm`` False ⟹ restricted queryset is empty ⟹ hiding the tool is honest.
* ``has_perm`` True  ⟹ queryset returns rows ⟹ the tool IS invocable, so hiding
  it would be an absence *lie*.  Enumerating ``get_all_permissions()`` — the old
  behaviour — tells exactly that lie whenever a host view exemption is in play.

Security conditions pinned here (V11-14): **C6** fail-closed, and the exemption
parity that **C1** rests on.  ``HasPermCapabilities`` is a lazy container, so
these tests assert membership, never iteration.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from frisian_mcp.contrib.permissions.base import (
    DjangoPermissionAdapter,
    HasPermCapabilities,
)


class _Principal:
    """A user whose ``has_perm`` mirrors a host backend: exempt ∨ granted."""

    is_superuser = False
    is_active = True

    def __init__(self, granted: set[str], exempt_views: bool = False) -> None:
        self._granted = granted
        self._exempt_views = exempt_views
        self.has_perm_calls: list[str] = []

    def get_all_permissions(self) -> set[str]:
        # Mirrors the host: the permission TABLE never records exemptions.
        return set(self._granted)

    def has_perm(self, perm: str, obj: Any = None) -> bool:
        # Mirrors the host predicate: exemption honoured BEFORE the table lookup.
        self.has_perm_calls.append(perm)
        if self._exempt_views and perm.split(".", 1)[-1].startswith("view_"):
            return True
        return perm in self._granted


ITEM_PERMS = {f"catalog.{verb}_item" for verb in ("view", "add", "change", "delete")}


class TestParityWithTheDataBoundary:
    """Discovery must track has_perm — the predicate the host's data gate uses."""

    def test_granted_permission_is_present(self) -> None:
        """A held permission resolves True."""
        caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS))
        assert "catalog.view_item" in caps
        assert "catalog.delete_item" in caps

    def test_ungranted_permission_is_absent(self) -> None:
        """A permission the principal does not hold resolves False."""
        caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS))
        assert "warehouse.view_crate" not in caps

    def test_scoped_principal_does_not_see_the_estate(self) -> None:
        """THE REGRESSION TEST: a catalog-scoped account sees catalog and nothing else.

        This is the 2026-07-13 leak, in miniature.  With no host exemption the
        old enumeration behaved correctly too — the point is that it stays
        correct here while ALSO getting the exemption case right below.
        """
        caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS))
        for perm in (
            "warehouse.view_crate",
            "warehouse.view_pallet",
            "vault.view_secret",
            "accounts.view_token",
        ):
            assert perm not in caps, f"estate leak: {perm}"

    def test_host_exemption_is_honoured_natively(self) -> None:
        """A view exemption the permission TABLE cannot see is still honoured.

        The host's queryset restriction returns ALL rows for an exempt perm, so
        the tool IS invocable and discovery must show it.  Enumerating
        ``get_all_permissions()`` would hide it — an absence lie.  This is the
        whole reason ExemptViewPermissionAdapter existed; has_perm closes it
        generically.
        """
        principal = _Principal(ITEM_PERMS, exempt_views=True)
        caps = DjangoPermissionAdapter().get_capabilities(principal)

        # Exempt => invocable => must be discoverable.
        assert "warehouse.view_crate" in caps
        # The permission table does NOT record it — proving we did not enumerate.
        assert "warehouse.view_crate" not in principal.get_all_permissions()

    def test_exemption_does_not_widen_writes(self) -> None:
        """A *view* exemption grants reads only — writes still need a real perm."""
        caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS, exempt_views=True))
        assert "warehouse.view_crate" in caps
        assert "warehouse.add_crate" not in caps
        assert "warehouse.delete_crate" not in caps


class TestFailClosed:
    """V11-14 C6: a broken backend may only HIDE tools, never reveal them."""

    def test_raising_has_perm_denies(self) -> None:
        """An exception from has_perm is treated as 'not granted'."""

        class Exploding:
            is_superuser = False
            is_active = True

            def has_perm(self, perm: str, obj: Any = None) -> bool:
                raise RuntimeError("permission backend is down")

        caps = DjangoPermissionAdapter().get_capabilities(Exploding())
        assert "warehouse.view_crate" not in caps
        assert "catalog.view_item" not in caps

    def test_non_string_membership_is_denied(self) -> None:
        """A non-string lookup cannot accidentally grant."""
        caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS))
        assert 42 not in caps  # type: ignore[operator]
        assert None not in caps  # type: ignore[operator]


class TestLaziness:
    """Resolution is on demand and memoised — one has_perm call per permission."""

    def test_nothing_resolved_until_asked(self) -> None:
        """Constructing the capability set performs no permission checks."""
        principal = _Principal(ITEM_PERMS)
        DjangoPermissionAdapter().get_capabilities(principal)
        assert principal.has_perm_calls == []

    def test_repeated_lookups_hit_the_cache(self) -> None:
        """The same permission is resolved at most once per request."""
        principal = _Principal(ITEM_PERMS)
        caps = DjangoPermissionAdapter().get_capabilities(principal)
        for _ in range(5):
            assert "catalog.view_item" in caps
            assert "warehouse.view_crate" not in caps
        assert principal.has_perm_calls.count("catalog.view_item") == 1
        assert principal.has_perm_calls.count("warehouse.view_crate") == 1

    def test_only_requested_permissions_are_resolved(self) -> None:
        """We never enumerate: unasked permissions cost nothing."""
        principal = _Principal(ITEM_PERMS)
        caps = DjangoPermissionAdapter().get_capabilities(principal)
        assert "catalog.view_item" in caps
        assert principal.has_perm_calls == ["catalog.view_item"]


class TestDeprecatedExemptAdapter:
    """The old adapter still imports and boots, but is a warned no-op."""

    def test_emits_deprecation_warning(self) -> None:
        """A host whose settings still name it gets a DeprecationWarning."""
        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        with pytest.warns(DeprecationWarning, match="no longer does anything"):
            ExemptViewPermissionAdapter()

    def test_behaves_identically_to_the_default(self) -> None:
        """It delegates to the has_perm-backed default — no synthesis of its own."""
        from frisian_mcp.contrib.permissions.exempt_view_adapter import (
            ExemptViewPermissionAdapter,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = ExemptViewPermissionAdapter()

        principal = _Principal(ITEM_PERMS, exempt_views=True)
        legacy_caps = legacy.get_capabilities(principal)
        default_caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS, True))

        for perm in ("catalog.view_item", "warehouse.view_crate", "warehouse.add_crate"):
            assert (perm in legacy_caps) == (perm in default_caps)

    def test_is_a_container_not_a_frozenset(self) -> None:
        """Contract: discovery only ever asks membership."""
        caps = DjangoPermissionAdapter().get_capabilities(_Principal(ITEM_PERMS))
        assert isinstance(caps, HasPermCapabilities)
        assert "catalog.view_item" in caps
