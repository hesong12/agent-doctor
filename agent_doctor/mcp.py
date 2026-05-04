"""Minimal MCP placeholder for the MVP."""

from __future__ import annotations

import json
from typing import Any


def placeholder_payload() -> dict[str, Any]:
    return {
        "name": "agent-doctor-mcp",
        "status": "placeholder",
        "write_tools_enabled": False,
        "privacy": "local-only; no network calls",
        "tools": [
            {
                "name": "scan",
                "description": "Use the CLI command `agent-doctor scan`; MCP write tools are disabled in the MVP.",
            }
        ],
    }


def main() -> None:
    print(json.dumps(placeholder_payload(), indent=2))


if __name__ == "__main__":
    main()
