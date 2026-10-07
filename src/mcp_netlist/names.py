"""Pure helpers for the netlist server: names, cell types, ports and file locations."""

from __future__ import annotations

import os
import re
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

# --------------------------------------------------------------------------- files


def bundle_dir() -> Path:
    """The openroad_bundle directory: unit-delay library, LEF, helper Tcl and netlists."""
    env = os.environ.get("NETLIST_BUNDLE_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "openroad_bundle"


def _setting(name: str, default: Path) -> Path:
    value = os.environ.get(f"NETLIST_{name}")
    return Path(value) if value else default


def default_liberty() -> Path:
    return _setting("LIB", bundle_dir() / "unit_delay.lib")


def default_lef() -> Path:
    return _setting("LEF", bundle_dir() / "unit_delay.lef")


def default_orhelp() -> Path:
    return _setting("ORHELP", bundle_dir() / "orhelp.tcl")


def netlist_dir() -> Path:
    return _setting("DIR", bundle_dir() / "netlists")


def output_dir() -> Path:
    return _setting("OUTPUT_DIR", bundle_dir() / "out")


_CASE_RE = re.compile(r"^[A-Za-z_][\w-]*$")


def resolve_netlist(spec: str) -> tuple[Path, str]:
    """Find a netlist from a path, a file name or a bare case name.

    Tries the path as given, then the netlist directory (also ``<dir>/<case>/<file>``), so
    "testcase/test02/test02.v", "test02.v" and "test02" all find the bundled netlist.
    Returns (path, how it was resolved).
    """
    raw = spec.strip().strip('"').strip("'")
    if not raw:
        raise ToolError("Empty netlist name.")
    given = Path(raw)
    if given.is_file():
        return given.resolve(), "path as given"
    base = given.name
    if _CASE_RE.match(base) and not base.endswith(".v"):
        base += ".v"
    root = netlist_dir()
    for candidate in (root / base, root / given.stem / base):
        if candidate.is_file():
            return candidate.resolve(), f"found in {root}"
    raise ToolError(
        f"Netlist not found: {spec}. Looked for the path itself and for {base} in {root} "
        "(set NETLIST_DIR to change the directory)."
    )


def resolve_output(spec: str | None, case: str | None) -> Path:
    """Output path: absolute paths as given; names and relative paths go to the output dir."""
    if not spec:
        if not case:
            raise ToolError("Give an output path (no design name to derive <case>_out.v from).")
        spec = f"{case}_out.v"
    raw = spec.strip().strip('"').strip("'")
    path = Path(raw)
    if path.is_absolute() or raw.startswith("/"):
        return path
    return output_dir() / path


# --------------------------------------------------------------------------- names


def normalize_name(name: str) -> str:
    r"""Strip quoting the user may add: braces, quotes, backslash-escaped brackets, \escaped ids."""
    n = name.strip()
    while len(n) >= 2 and ((n[0], n[-1]) in {("{", "}"), ('"', '"'), ("'", "'"), ("`", "`")}):
        n = n[1:-1].strip()
    n = n.replace("\\[", "[").replace("\\]", "]")
    if n.startswith("\\"):
        n = n[1:].strip()
    if not n:
        raise ToolError("Empty name.")
    return n


_BIT_RE = re.compile(r"^(.*)\[(\d+)\]$")


def split_bit(name: str) -> tuple[str, int | None]:
    m = _BIT_RE.match(name)
    return (m.group(1), int(m.group(2))) if m else (name, None)


def natural_key(name: str) -> list:
    """Sort key that puts g2 before g10 and n5[2] before n5[10]."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def group_port_bits(bits: list[str]) -> list[dict]:
    """Group port bit names into ports with widths: n5[0..24] -> {name n5, width 25, msb 24, lsb 0}."""
    groups: dict[str, list[int | None]] = {}
    for bit in bits:
        base, idx = split_bit(bit)
        groups.setdefault(base, []).append(idx)
    out = []
    for base in sorted(groups, key=natural_key):
        idxs = groups[base]
        if idxs == [None]:
            out.append({"name": base, "width": 1, "msb": None, "lsb": None})
        else:
            nums = [i for i in idxs if i is not None]
            out.append({"name": base, "width": len(idxs), "msb": max(nums), "lsb": min(nums)})
    return out


VALID_NEW_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# --------------------------------------------------------------------------- cell types

# Contest gate names -> unit_delay.lib cells.
CELL_ALIASES = {
    "NOT": "INV",
    "INV": "INV",
    "INVERTER": "INV",
    "BUF": "BUF",
    "BUFFER": "BUF",
    "AND": "AND2",
    "OR": "OR2",
    "NAND": "NAND2",
    "NOR": "NOR2",
    "XOR": "XOR2",
    "XNOR": "XNOR2",
    "DFF": "DFF",
    "FF": "DFF",
    "FLIPFLOP": "DFF",
    "FLIP-FLOP": "DFF",
    "REGISTER": "DFF",
}

# Library cell -> the gate name used in the task prompts.
USER_NAMES = {"INV": "NOT", "BUF": "BUF", "AND2": "AND", "OR2": "OR", "NAND2": "NAND", "NOR2": "NOR",
              "XOR2": "XOR", "XNOR2": "XNOR", "DFF": "DFF"}
USER_ORDER = ["AND", "OR", "NOT", "NAND", "NOR", "XOR", "XNOR", "BUF", "DFF"]


def normalize_cell_type(name: str, known: list[str] | None = None) -> str:
    """Map NOT/AND/... (any case, optional plural 's' or 'gate(s)') to a library cell name."""
    raw = name.strip()
    if known and raw in known:
        return raw
    key = re.sub(r"\s*(gates?|cells?)$", "", raw, flags=re.IGNORECASE).strip().upper()
    if key.endswith("S") and key[:-1] in CELL_ALIASES:
        key = key[:-1]
    cell = CELL_ALIASES.get(key, key)
    if known and cell not in known:
        raise ToolError(f"Unknown gate type {name!r}. Library cells: {', '.join(known)} (NOT = INV, AND = AND2, ...).")
    return cell
