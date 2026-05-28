"""
friese_mcp.backends — pluggable discovery and invocation backends.

Public API::

    from friese_mcp.backends import (
        BaseDiscoveryBackend,
        BaseInvocationBackend,
        DRFSyncDiscovery,
        SyncInvocation,
        ToolDefinition,
        ToolResult,
        get_discovery_backend,
        get_invocation_backend,
    )
"""

from __future__ import annotations

import importlib
from typing import Any

from django.conf import settings

from friese_mcp.backends.base import (
    BaseDiscoveryBackend,
    BaseInvocationBackend,
    ToolDefinition,
    ToolResult,
)
from friese_mcp.backends.discovery import DRFSyncDiscovery
from friese_mcp.backends.invocation import SyncInvocation


def _load_class(class_path: str) -> type:
    """Import and return a class given its dotted Python path."""
    module_path, _, class_name = class_path.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


def get_discovery_backend(
    default: type[BaseDiscoveryBackend] | None = None,
) -> BaseDiscoveryBackend:
    """
    Load and instantiate the configured discovery backend.

    Reads ``settings.FRIESE_MCP_DISCOVERY_BACKEND``.  Falls back to *default*,
    then to :class:`DRFSyncDiscovery` if *default* is ``None``.

    Args:
        default: Default backend class used when the setting is absent.

    Returns:
        An instantiated :class:`BaseDiscoveryBackend`.

    """
    class_path: str | None = getattr(settings, "FRIESE_MCP_DISCOVERY_BACKEND", None)
    if class_path is not None:
        cls = _load_class(class_path)
        return cls()  # type: ignore[no-any-return]
    if default is not None:
        return default()
    return DRFSyncDiscovery()


def get_discovery_backends() -> list[BaseDiscoveryBackend]:
    """
    Load and instantiate all configured discovery backends.

    Resolution order:

    1. ``FRIESE_MCP_DISCOVERY_BACKENDS`` — a list of dotted-path strings, each
       pointing to a :class:`BaseDiscoveryBackend` subclass.  All are loaded
       and their results are merged in order (later entries win on name clash).
    2. ``FRIESE_MCP_DISCOVERY_BACKEND`` (singular, legacy) — wrapped in a
       single-element list so existing configurations are unaffected.
    3. :class:`DRFSyncDiscovery` — the built-in default.

    Returns:
        A list of instantiated :class:`BaseDiscoveryBackend` objects.  Always
        contains at least one element.

    """
    plural: list[str] | None = getattr(settings, "FRIESE_MCP_DISCOVERY_BACKENDS", None)
    if plural is not None:
        return [_load_class(path)() for path in plural]

    singular: str | None = getattr(settings, "FRIESE_MCP_DISCOVERY_BACKEND", None)
    if singular is not None:
        cls = _load_class(singular)
        return [cls()]

    return [DRFSyncDiscovery()]


def get_invocation_backend(
    default: type[BaseInvocationBackend] | None = None,
) -> BaseInvocationBackend:
    """
    Load and instantiate the configured invocation backend.

    Reads ``settings.FRIESE_MCP_INVOCATION_BACKEND``.  Falls back to *default*,
    then to :class:`SyncInvocation` if *default* is ``None``.

    Args:
        default: Default backend class used when the setting is absent.

    Returns:
        An instantiated :class:`BaseInvocationBackend`.

    """
    class_path: str | None = getattr(settings, "FRIESE_MCP_INVOCATION_BACKEND", None)
    if class_path is not None:
        cls = _load_class(class_path)
        return cls()  # type: ignore[no-any-return]
    if default is not None:
        return default()
    return SyncInvocation()


# Silence "imported but unused" for re-exported names.
__all__ = [
    "BaseDiscoveryBackend",
    "BaseInvocationBackend",
    "DRFSyncDiscovery",
    "SyncInvocation",
    "ToolDefinition",
    "ToolResult",
    "get_discovery_backend",
    "get_discovery_backends",
    "get_invocation_backend",
]

# Avoid "imported but unused" linter warnings for re-exports.
_REEXPORTED: tuple[Any, ...] = (
    BaseDiscoveryBackend,
    BaseInvocationBackend,
    DRFSyncDiscovery,
    SyncInvocation,
    ToolDefinition,
    ToolResult,
)
del _REEXPORTED
