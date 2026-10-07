"""Path, quoting and output helpers shared by the OpenROAD MCP servers."""

from __future__ import annotations

import os
import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_MAX_OUTPUT = 60000
RECORD_PREFIX = "@@\t"

_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]*(.*)$", re.DOTALL)
_WSL_UNC_RE = re.compile(r"^[\\/]{2}wsl(?:\$|\.localhost)[\\/][^\\/]+[\\/]?(.*)$", re.IGNORECASE)


def to_wsl_path(path: str) -> str:
    r"""Convert a Windows path to the path openroad sees inside WSL.

    ``C:\a\b.lib`` -> ``/mnt/c/a/b.lib``; ``\\wsl$\Ubuntu\home\x`` -> ``/home/x``.
    POSIX paths (``/...`` or ``~/...``) are passed through unchanged, and relative
    Windows paths are resolved against the server's working directory.
    """
    p = path.strip().strip('"').strip("'")
    if not p:
        raise ToolError("Empty file path.")
    m = _WSL_UNC_RE.match(p)
    if m:
        return "/" + m.group(1).replace("\\", "/")
    if p.startswith("/") or p.startswith("~"):
        return p
    if not _DRIVE_RE.match(p):
        p = os.path.abspath(p)
    m = _DRIVE_RE.match(p)
    if not m:
        raise ToolError(f"Cannot map path to WSL (use an absolute path): {path}")
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def tcl_quote(value: Any) -> str:
    """Quote any value as a single Tcl word with no substitution."""
    text = str(value)
    for char, escaped in (
        ("\\", "\\\\"),
        ('"', '\\"'),
        ("$", "\\$"),
        ("[", "\\["),
        ("]", "\\]"),
        ("\n", "\\n"),
        ("\r", "\\r"),
    ):
        text = text.replace(char, escaped)
    return f'"{text}"'


def tcl_list(items: list[Any]) -> str:
    """Build a Tcl ``[list ...]`` expression from Python values."""
    return "[list " + " ".join(tcl_quote(item) for item in items) + "]"


def tcl_file(path: str) -> str:
    """Tcl expression for a checked, normalized WSL path (the file must exist)."""
    return f"[__mcp_file {tcl_quote(to_wsl_path(path))}]"


def truncate(text: str, limit: int = DEFAULT_MAX_OUTPUT) -> str:
    """Keep the head and tail of very long reports within ``limit`` characters."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    dropped = len(text) - limit
    return (
        text[:head]
        + f"\n\n... [{dropped} characters truncated; narrow the query or lower the counts] ...\n\n"
        + text[-tail:]
    )


def join_lines(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def parse_records(output: str) -> tuple[list[list[str]], str]:
    """Split driver output into ``__mcp_rec`` records and the remaining log text.

    A record line is ``@@<TAB>field<TAB>field...``; every other line is log text.
    """
    records: list[list[str]] = []
    log: list[str] = []
    for line in output.splitlines():
        if line.startswith(RECORD_PREFIX):
            records.append(line[len(RECORD_PREFIX) :].split("\t"))
        else:
            log.append(line)
    return records, "\n".join(log).strip()
