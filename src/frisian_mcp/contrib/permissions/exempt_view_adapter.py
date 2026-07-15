"""
DEPRECATED — retained only so existing dotted-path settings keep importing.

``ExemptViewPermissionAdapter`` existed because the default adapter used to
derive capabilities by *enumerating* ``user.get_all_permissions()``, which
cannot see a host's ``EXEMPT_VIEW_PERMISSIONS`` setting.  On a host with a view
exemption that produced an **absence lie**: the tool was hidden from discovery
while the caller could still invoke it by name and receive every row.  This
adapter patched that gap by synthesizing the missing ``view_*`` capabilities —
but only for that one host mechanism, and only for that one setting.

Since v1.1.0 the default
:class:`~frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter` resolves
capabilities through ``user.has_perm()``, the same predicate the host itself
authorizes with.  View exemptions — and custom auth backends, and any future
host authorization mechanism ``has_perm`` honours — are therefore respected
**natively**, with no adapter to select and no wildcard to parse.

Migration
---------
Delete the setting.  Nothing replaces it::

    # before
    FRISIAN_MCP_PERMISSION_ADAPTER = (
        "frisian_mcp.contrib.permissions.exempt_view_adapter.ExemptViewPermissionAdapter"
    )

    # after — omit it entirely; the default adapter is now correct on
    #         exemption-using hosts.

.. deprecated:: 1.1.0
   Subclasses :class:`DjangoPermissionAdapter` and adds nothing.  It will be
   removed in the next minor release.
"""

from __future__ import annotations

import warnings

from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter

__all__ = ["ExemptViewPermissionAdapter"]

_DEPRECATION_MESSAGE = (
    "ExemptViewPermissionAdapter is deprecated and no longer does anything: the "
    "default DjangoPermissionAdapter now resolves capabilities via user.has_perm(), "
    "which honours EXEMPT_VIEW_PERMISSIONS (and custom auth backends) natively. "
    "Remove FRISIAN_MCP_PERMISSION_ADAPTER from your settings; nothing replaces it. "
    "This class will be removed in the next minor release."
)


class ExemptViewPermissionAdapter(DjangoPermissionAdapter):
    """
    Deprecated no-op alias of :class:`DjangoPermissionAdapter`.

    Behaviour is identical to the default adapter.  It is kept only so that a
    host whose settings still name this class by dotted path keeps booting; it
    emits a :class:`DeprecationWarning` on instantiation.

    Its historical behaviour is not merely redundant now — it would be *wrong*.
    Synthesizing ``view_*`` capabilities from a wildcard exemption handed every
    authenticated principal read access to every model, silently defeating
    per-principal discovery scoping.  ``has_perm`` asks the host for its answer
    about *this* principal instead.
    """

    def __init__(self) -> None:
        """Warn that the class is a deprecated no-op, then behave as the default."""
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)

    # No method overrides: capability resolution is inherited verbatim from
    # DjangoPermissionAdapter.  The absence of an override IS the deprecation —
    # this class adds nothing and exists only to keep a stale dotted-path
    # setting importable for one release.
