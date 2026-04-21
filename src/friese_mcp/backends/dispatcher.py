"""Runtime support for @mcp_dispatcher class-based tool dispatchers."""

from __future__ import annotations

import dataclasses
import difflib
from collections.abc import Callable
from typing import Any

import jsonschema
import jsonschema.exceptions
from django.http import HttpRequest

from friese_mcp.registry import ToolInputError


@dataclasses.dataclass
class ActionEntry:
    """Metadata and callable for a single dispatcher action."""

    name: str
    description: str
    params: dict[str, str]
    input_schema: dict[str, Any] | None
    method: Callable[..., Any]


@dataclasses.dataclass
class DispatcherMeta:
    """Aggregated metadata for a registered @mcp_dispatcher class."""

    name: str
    description: str
    actions: dict[str, ActionEntry]


def _build_dispatcher_input_schema(meta: DispatcherMeta) -> dict[str, Any]:
    """Return the compact inputSchema for a dispatcher tool."""
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(meta.actions.keys()),
                "description": (
                    "Operation to perform. Omit or use 'help' to list all available"
                    " actions and their required parameters."
                ),
            },
            "params": {
                "type": "object",
                "additionalProperties": True,
                "description": "Parameters for the chosen action. See help for details.",
            },
        },
    }


def _build_help_response(meta: DispatcherMeta) -> dict[str, Any]:
    """Return the structured help payload for a dispatcher."""
    return {
        "help": True,
        "dispatcher": meta.name,
        "actions": [
            {
                "name": e.name,
                "description": e.description,
                "params": e.params,
                "input_schema": e.input_schema,
            }
            for e in meta.actions.values()
        ],
    }


def _make_dispatcher_invoke(
    cls: type, meta: DispatcherMeta
) -> Callable[..., dict[str, Any]]:
    """Build the invoke callable for *cls*, closing over *meta*."""
    instance = cls()
    action_map = meta.actions

    def invoke(arguments: dict[str, Any], request: HttpRequest) -> dict[str, Any]:
        action: str | None = arguments.get("action")
        params: dict[str, Any] = arguments.get("params") or {}

        if action is None or action == "help":
            return _build_help_response(meta)

        if action not in action_map:
            matches = difflib.get_close_matches(action, action_map.keys(), n=1)
            hint = f" Did you mean: {matches[0]!r}?" if matches else ""
            raise LookupError(f"Unknown action {action!r}.{hint}")

        entry = action_map[action]
        if entry.input_schema is not None:
            try:
                jsonschema.validate(instance=params, schema=entry.input_schema)
            except jsonschema.exceptions.ValidationError as exc:
                raise ToolInputError(
                    f"Invalid params for action {action!r}: {exc.message}"
                ) from exc

        return entry.method(instance, request, params)  # type: ignore[return-value]

    return invoke
