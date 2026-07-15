"""
Permission adapter protocol and default Django implementation.

When ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` is ``True``, frisian-mcp
loads the adapter class named in ``FRISIAN_MCP_PERMISSION_ADAPTER`` (default:
:class:`DjangoPermissionAdapter`) and calls it once per ``tools/list`` request
to decide which tools a given user is allowed to see.

The adapter answers two questions:

1. ``get_capabilities(user)`` — what Django-style permissions does this user
   hold?  Returns a ``frozenset`` of ``"app_label.action_model"`` strings,
   matching the format produced by ``user.get_all_permissions()``.

2. ``is_unrestricted(user)`` — should this user see ALL tools, skipping the
   per-capability filter entirely?  Typically ``True`` for superusers.
"""

from __future__ import annotations

from collections.abc import Container
from typing import Any, Protocol, runtime_checkable

#: Maps DRF viewset action names to the Django permission verb used when
#: checking whether a user can perform that action.  Unknown action names
#: fall back to ``"view"`` (most conservative default).
_DRF_ACTION_TO_PERM_VERB: dict[str, str] = {
    "list": "view",
    "retrieve": "view",
    "create": "add",
    "update": "change",
    "partial_update": "change",
    "destroy": "delete",
    # Bulk DRF actions
    "bulk_destroy": "delete",
    "bulk_update": "change",
    "bulk_partial_update": "change",
}


@runtime_checkable
class PermissionAdapter(Protocol):
    """
    Protocol for permission-aware discovery adapters.

    Implementors map a Django request user to the set of Django-style
    permission strings that determine which MCP tools are included in the
    ``tools/list`` response.
    """

    def get_capabilities(self, user: Any) -> Container[str]:
        """
        Return the capability set held by *user*.

        Discovery only ever asks this object one question —
        ``"app_label.verb_model" in capabilities`` — so any ``Container[str]``
        is a valid return: a plain ``frozenset`` of permission strings (what
        adapters returned before v1.1.0, still fully supported), or a lazy
        resolver such as :class:`HasPermCapabilities` that answers membership on
        demand.  It is never iterated, sized, or serialised.
        """
        raise NotImplementedError

    def is_unrestricted(self, user: Any) -> bool:
        """Return ``True`` when *user* should see all tools regardless of permissions."""
        raise NotImplementedError


class HasPermCapabilities(Container[str]):
    """
    Lazy capability set that answers membership by asking ``user.has_perm()``.

    Duck-types a ``frozenset[str]`` for the only operation discovery performs on
    a capability set — ``"app_label.verb_model" in capabilities`` — but resolves
    each permission on demand instead of enumerating up front.

    **Why ask instead of enumerate (V11-11).**  ``user.get_all_permissions()``
    and ``user.has_perm()`` are *not* the same predicate.  On the hosts this
    package targets, ``has_perm`` additionally honours superuser status, view
    exemptions, and any custom authentication backend, while
    ``get_all_permissions()`` reports only the rows a permission model happens
    to store.  ``has_perm`` is therefore a strict superset, and it is the
    predicate the host's own **data** boundary uses: a host that scopes objects
    with a per-user queryset restriction (``restrict(user, verb)``) evaluates
    the *same* condition ``superuser ∨ exempt ∨ perm ∈ get_all_permissions``.

    Consequences, both directions:

    * ``has_perm`` **False** ⟹ the restricted queryset yields no rows ⟹ the
      tool would return **zero data** if invoked.  Hiding it is honest: absence
      means "cannot reach data", not merely "not listed".
    * ``has_perm`` **True** ⟹ the queryset yields rows ⟹ the tool is invocable,
      so discovery must show it.  Enumerating ``get_all_permissions()`` instead
      would **hide a tool the caller can still invoke by name** whenever a host
      exemption is in play — an absence *lie*.  That under-report is the bug
      this class removes generically, rather than patching one host mechanism
      at a time.

    **Boundary of the guarantee.**  This parity holds on a host that gates
    object access through per-user queryset restriction in the ViewSet
    lifecycle.  A DRF host that does *not* scope querysets per user enforces
    only the MCP tier at invocation, so permission-aware discovery is advisory
    there, not a security boundary.  That limitation is inherent to the
    invocation path (which bypasses DRF's model-permission check by design) and
    is unchanged by this class — but it must be understood before relying on
    permission-aware discovery as an access-control boundary.

    **Fail-closed (V11-14 C6).**  Any exception from ``has_perm`` is treated as
    "not granted".  A broken or hostile permission backend can only ever *hide*
    tools, never reveal them.

    Results are memoised per instance; an instance is built once per request, so
    a permission is resolved at most once even though several discovery surfaces
    (tools/list, the dispatcher action enum, ``action="help"``) consult it.
    """

    # Provenance (V11-11 / V11-14, 2026-07-13): the queryset-restriction parity
    # above was verified directly in the source of the two host frameworks this
    # package targets — Nautobot (`core/authentication.py` has_perm vs
    # `core/models/querysets.py` restrict) and NetBox (`authentication/__init__.py`
    # vs `utilities/querysets.py`).  Both evaluate the identical predicate
    # `superuser OR exempt OR perm in get_all_permissions`, so has_perm is True
    # exactly when the restricted queryset is non-empty.  Kept as a comment, not
    # a docstring, per the package-neutrality rule.
    __slots__ = ("_cache", "_user")

    def __init__(self, user: Any) -> None:
        """Bind the adapter to *user*; nothing is resolved until first lookup."""
        self._user = user
        self._cache: dict[str, bool] = {}

    def __contains__(self, perm: object) -> bool:
        """Return ``True`` when *user* holds *perm*, per the host's own predicate."""
        if not isinstance(perm, str):
            return False
        cached = self._cache.get(perm)
        if cached is None:
            try:
                cached = bool(self._user.has_perm(perm))
            except Exception:  # pylint: disable=broad-exception-caught
                # C6: fail closed.  A backend that raises must not grant.
                cached = False
            self._cache[perm] = cached
        return cached

    def __repr__(self) -> str:
        """Return a debug repr that does not force resolution of every permission."""
        granted = sum(1 for v in self._cache.values() if v)
        return f"<HasPermCapabilities resolved={len(self._cache)} granted={granted}>"


class DjangoPermissionAdapter:
    """
    Default adapter: resolves capabilities through Django's ``user.has_perm()``.

    Works for any Django project using the standard authentication backend, and
    correctly honours host-specific authorization the permission *tables* do not
    record — view exemptions, custom auth backends — because it consults the
    same predicate the host itself authorizes with.  See
    :class:`HasPermCapabilities` for the full rationale and the boundary of the
    guarantee.

    A broken permission backend can only cause tools to be hidden, never
    exposed.
    """

    def get_capabilities(self, user: Any) -> Any:
        """Return a lazy capability set backed by ``user.has_perm()``."""
        return HasPermCapabilities(user)

    def is_unrestricted(self, user: Any) -> bool:
        """Return ``True`` when *user* is a superuser and should see all tools."""
        return bool(getattr(user, "is_superuser", False))
