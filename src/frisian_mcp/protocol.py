"""MCP protocol constants and JSON-RPC 2.0 type aliases."""

from typing import Any

# MCP protocol version advertised during the initialize handshake.
MCP_PROTOCOL_VERSION: str = "2025-11-25"

# JSON-RPC 2.0 type aliases.
JsonRpcId = int | str | None
JsonDict = dict[str, Any]

# ---- JSON-RPC 2.0 standard error codes ----
PARSE_ERROR: int = -32700  # Invalid JSON was received.
INVALID_REQUEST: int = -32600  # The JSON is not a valid Request object.
METHOD_NOT_FOUND: int = -32601  # The method does not exist.
INVALID_PARAMS: int = -32602  # Invalid method parameters.
INTERNAL_ERROR: int = -32603  # Internal JSON-RPC error.
