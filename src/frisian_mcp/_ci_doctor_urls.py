"""Empty root URLconf for the CI ``mcp_doctor --strict`` gate (V11-21).

The doctor reads ``FRISIAN_MCP_ROUTES`` directly and does not depend on the
routes being mounted, so this fixture provides a valid but empty URL surface —
just enough for Django to resolve ``ROOT_URLCONF`` while the gate runs.
"""

from __future__ import annotations

from typing import Any

urlpatterns: list[Any] = []
