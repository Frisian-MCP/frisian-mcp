"""
Nautobot-specific permission adapter.

Nautobot's ``ObjectPermissionBackend`` populates ``user.get_all_permissions()``
with ``"app_label.action_model"`` strings (e.g. ``"dcim.view_device"``), so
:class:`~frisian_mcp.contrib.permissions.base.DjangoPermissionAdapter` works
directly.

This subclass extends the base adapter to honour Nautobot's
``EXEMPT_VIEW_PERMISSIONS`` setting: models listed there are implicitly
readable by all authenticated users, so their ``"view_<model>"`` capability is
synthesized even when the user holds no explicit object permission.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from frisian_mcp.contrib.permissions.base import DjangoPermissionAdapter


class NautobotPermissionAdapter(DjangoPermissionAdapter):
    """
    Nautobot adapter that adds ``EXEMPT_VIEW_PERMISSIONS`` support.

    Nautobot's ``ObjectPermissionBackend`` stores permissions in
    ``user.get_all_permissions()`` using the standard Django ``"app_label.action_model"``
    format, so the base adapter handles the common case.  This subclass
    additionally synthesizes ``"app_label.view_<model>"`` capabilities for models
    in ``settings.EXEMPT_VIEW_PERMISSIONS`` so that tools backed by those models
    appear in ``tools/list`` for all authenticated users, matching the behaviour
    of Nautobot's ``RestrictedQuerySet``.
    """

    def get_capabilities(self, user: Any) -> frozenset[str]:
        """Return capabilities from Django permissions plus synthesized EXEMPT_VIEW_PERMISSIONS."""
        base = super().get_capabilities(user)
        extra: set[str] = set()
        exempt: list[str] | str = getattr(settings, "EXEMPT_VIEW_PERMISSIONS", [])
        if exempt == "__all__":
            # All view permissions are globally exempt — add view_<model> for every
            # installed model so no tool is filtered out on a view-action basis.
            from django.apps import apps  # pylint: disable=import-outside-toplevel

            for model in apps.get_models():
                meta = model._meta  # pylint: disable=protected-access
                extra.add(f"{meta.app_label}.view_{meta.model_name}")
        else:
            for model_label in (exempt or []):
                parts = str(model_label).split(".", 1)
                if len(parts) == 2:
                    app_label, model_name = parts
                    extra.add(f"{app_label}.view_{model_name}")
        return base | extra
