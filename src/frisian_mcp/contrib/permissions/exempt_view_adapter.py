"""
Permission adapter that honours ``EXEMPT_VIEW_PERMISSIONS``.

Some Django applications mark certain models as globally readable via an
``EXEMPT_VIEW_PERMISSIONS`` setting.  Models listed there are implicitly
viewable by all authenticated users without an explicit object permission
being assigned, so their ``"view_<model>"`` capability must be synthesized
for ``FRISIAN_MCP_PERMISSION_AWARE_DISCOVERY`` to include the corresponding
tools in ``tools/list``.

This subclass of :class:`~frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter`
adds that synthesis on top of the standard ``user.get_all_permissions()`` lookup.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter


class ExemptViewPermissionAdapter(DjangoPermissionAdapter):
    """
    Django permission adapter with ``EXEMPT_VIEW_PERMISSIONS`` support.

    Extends :class:`DjangoPermissionAdapter` by synthesizing
    ``"app_label.view_<model>"`` capabilities for every model listed in
    ``settings.EXEMPT_VIEW_PERMISSIONS``.  This ensures that tools backed
    by globally-readable models appear in ``tools/list`` for all authenticated
    users, matching the implicit read-access semantics of that setting.

    Supports both the ``"__all__"`` / ``"*"`` shorthand (all installed models
    become view-capable) and an explicit list of ``"app_label.model_name"``
    strings.  When the wildcard form is used, models listed in
    ``settings.EXEMPT_EXCLUDE_MODELS`` are excluded from synthesis — matching
    the semantics of the host application's own permission enforcement.
    ``EXEMPT_EXCLUDE_MODELS`` is expected to be a sequence of
    ``(app_label, model_name)`` tuples (e.g. ``[("auth", "group")]``).
    """

    def get_capabilities(self, user: Any) -> frozenset[str]:
        """Return capabilities from Django permissions plus synthesized EXEMPT_VIEW_PERMISSIONS."""
        base = super().get_capabilities(user)
        extra: set[str] = set()
        exempt: list[str] | str = getattr(settings, "EXEMPT_VIEW_PERMISSIONS", [])
        if exempt in ("__all__", "*"):
            # All view permissions are globally exempt — add view_<model> for every
            # installed model so no tool is filtered out on a view-action basis,
            # but honour EXEMPT_EXCLUDE_MODELS so protected models stay gated.
            from django.apps import apps  # pylint: disable=import-outside-toplevel

            exclude: set[tuple[str, str]] = set(
                getattr(settings, "EXEMPT_EXCLUDE_MODELS", None) or ()
            )
            for model in apps.get_models():
                meta = model._meta  # pylint: disable=protected-access
                if (meta.app_label, meta.model_name) not in exclude:
                    extra.add(f"{meta.app_label}.view_{meta.model_name}")
        else:
            for model_label in exempt or []:
                parts = str(model_label).split(".", 1)
                if len(parts) == 2:
                    app_label, model_name = parts
                    extra.add(f"{app_label}.view_{model_name}")
        return base | extra
