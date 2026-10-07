"""Small helpers for tools that exchange ``__mcp_rec`` records with the Tcl driver."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from openroad_common.session import OpenRoadSession
from openroad_common.tcl import parse_records

QUERY_MAX_CHARS = 50_000_000


def tcl(template: str, **values: Any) -> str:
    """Fill ``@NAME@`` placeholders in a Tcl template (values must already be Tcl-safe)."""
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    return template


def by_kind(records: list[list[str]], kind: str) -> list[list[str]]:
    return [r for r in records if r and r[0] == kind]


def first(records: list[list[str]], kind: str, required: bool = True) -> list[str] | None:
    matches = by_kind(records, kind)
    if matches:
        return matches[0]
    if required:
        raise ToolError(f"OpenROAD returned no '{kind}' data.")
    return None


def total(records: list[list[str]]) -> int:
    matches = by_kind(records, "total")
    return int(matches[0][1]) if matches else 0


def with_log(result: dict[str, Any], log: str) -> dict[str, Any]:
    if log.strip():
        result["log"] = log.strip()
    return result


async def query(
    session: OpenRoadSession, script: str, timeout: float | None = None, require_design: bool = True
) -> tuple[list[list[str]], str]:
    """Run a structured query; records are never truncated (tools bound their output)."""
    if require_design:
        session.require_design()
    return parse_records(await session.run(script, timeout=timeout, max_chars=QUERY_MAX_CHARS))
