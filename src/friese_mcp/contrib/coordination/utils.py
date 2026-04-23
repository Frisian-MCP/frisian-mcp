"""Tenant-scoping helpers for contrib.coordination."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import QuerySet


def get_tenant(request: Any) -> Any:
    """
    Return the tenant for *request*, or ``None`` for single-tenant installs.

    Resolution order:

    1. If ``FRIESE_MCP_COORDINATION_TENANT_GETTER`` is set, call the referenced
       callable with *request* and return its result.
    2. Otherwise return ``request.user.tenant`` when present, else ``None``.
    """
    getter_path = getattr(settings, "FRIESE_MCP_COORDINATION_TENANT_GETTER", None)
    if getter_path:
        from django.utils.module_loading import import_string  # pylint: disable=import-outside-toplevel

        return import_string(getter_path)(request)
    return getattr(getattr(request, "user", None), "tenant", None)


def scope_qs(qs: QuerySet, request: Any) -> QuerySet:
    """
    Filter *qs* by tenant when a tenant is present; return *qs* unchanged otherwise.

    Single-tenant installs (``get_tenant`` returns ``None``) receive the full
    queryset.  Multi-tenant installs receive only rows belonging to the tenant
    resolved from *request*.
    """
    tenant = get_tenant(request)
    return qs.filter(tenant=tenant) if tenant is not None else qs
