"""MCP server for gate-level netlist analysis with OpenROAD (OpenSTA + OpenDB).

Answers structural questions about flat gate-level netlists mapped onto the bundled
unit-delay library (every gate has delay 1, so timing arrival == number of gates): gate
counts, fanin/fanout cones, logic depth, path existence and dominance, cuts, constants,
floating signals, renaming and Verilog output. Runs one persistent ``openroad`` process
inside WSL (see ``openroad_common.session``); Tcl helper procs print ``__mcp_rec`` records
that are turned into JSON here.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from mcp_netlist.names import (
    USER_NAMES,
    USER_ORDER,
    VALID_NEW_NAME,
    default_lef,
    default_liberty,
    default_orhelp,
    group_port_bits,
    natural_key,
    normalize_cell_type,
    normalize_name,
    output_dir,
    resolve_netlist,
    resolve_output,
)
from mcp_netlist.tcl_procs import NETLIST_DRIVER_TCL
from openroad_common import Config, OpenRoadSession, parse_records, tcl_file, tcl_quote, to_wsl_path

CFG = Config.from_env("NETLIST")
SESSION = OpenRoadSession(CFG, "netlist", NETLIST_DRIVER_TCL)

QUERY_MAX_CHARS = 50_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tcl(template: str, **values: Any) -> str:
    """Fill ``@NAME@`` placeholders in a Tcl template (values must already be Tcl-safe)."""
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    return template


def q(name: str) -> str:
    """A user-supplied object name as a literal Tcl word."""
    return tcl_quote(normalize_name(name))


def by_kind(records: list[list[str]], kind: str) -> list[list[str]]:
    return [r for r in records if r and r[0] == kind]


def first(records: list[list[str]], kind: str) -> list[str] | None:
    matches = by_kind(records, kind)
    return matches[0] if matches else None


def nsort(names) -> list[str]:
    return sorted(names, key=natural_key)


def with_log(result: dict[str, Any], log: str) -> dict[str, Any]:
    log = "\n".join(line for line in log.splitlines() if "STA-0503" not in line).strip()
    if log:
        result["log"] = log
    return result


async def query(script: str, timeout: float | None = None) -> tuple[list[list[str]], str]:
    """Run a structured query. Records are never truncated (tools bound their output)."""
    SESSION.require_design()
    return parse_records(await SESSION.run(script, timeout=timeout, max_chars=QUERY_MAX_CHARS))


def seq_types(records: list[list[str]]) -> set[str]:
    return {r[1] for r in by_kind(records, "seq")}


def known_masters() -> list[str]:
    return list(getattr(SESSION.design, "masters", []) or [])


def cell_type_or_error(name: str) -> str:
    return normalize_cell_type(name, known_masters() or None)


def page(items: list, offset: int, limit: int) -> tuple[list, bool]:
    chunk = items[offset : offset + limit]
    return chunk, offset + len(chunk) < len(items)


def save_lines(path: str, lines: list[str]) -> str:
    target = resolve_output(path, None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


def user_name(cell: str) -> str:
    return USER_NAMES.get(cell, cell)


def a_an(word: str) -> str:
    return ("an " if word[:1].upper() in "AEIOUX" else "a ") + word


def name_list(names: list[str], cap: int = 50) -> str:
    return ", ".join(names[:cap]) + (" ..." if len(names) > cap else "")


# ------------------------------------------------------------------ path records


def paths_from(records: list[list[str]]) -> list[dict[str, Any]]:
    """Group ``path`` / ``pt`` records into paths with their points."""
    paths: dict[tuple[str, str], dict[str, Any]] = {}
    for r in records:
        if r[0] == "path":
            paths[(r[1], r[2])] = {
                "tag": r[1],
                "start": r[3],
                "end": r[4],
                "arrival": round(float(r[5])),
                "points": [],
            }
        elif r[0] == "pt":
            paths[(r[1], r[2])]["points"].append((r[3], round(float(r[4])), r[5]))
    return list(paths.values())


def meta_from(records: list[list[str]]) -> dict[str, str]:
    m = first(records, "meta") or ["meta", "", "", "", ""]
    m = m + [""] * (5 - len(m))
    return {"sink_kind": m[1], "sink": m[2], "from_kind": m[3], "from": m[4]}


def measure(path: dict[str, Any], meta: dict[str, str], seq: set[str] | None = None) -> dict[str, Any] | None:
    """Gate depth and gate list of one path between the requested source and sink."""
    pts = path["points"]
    names = [p[0] for p in pts]
    sink = meta["sink"]
    end_idx = len(pts) - 1 if meta["sink_kind"] == "output" else (names.index(sink) if sink in names else None)
    if end_idx is None:
        return None
    start_idx = 0
    base = 1 if path["tag"] == "reg" else 0
    if meta["from"] and meta["from_kind"] != "input":
        if meta["from"] not in names:
            return None
        start_idx = names.index(meta["from"])
        base = pts[start_idx][1]
    depth = pts[end_idx][1] - base
    gates = [g for _pin, _arr, g in pts[start_idx + 1 : end_idx + 1] if g]
    return {"depth": depth, "startpoint": path["start"], "gates": gates}


def best_measure(records: list[list[str]]) -> tuple[dict[str, str], dict[str, Any] | None]:
    meta = meta_from(records)
    best = None
    for path in paths_from(records):
        m = measure(path, meta)
        if m and (best is None or m["depth"] > best["depth"]):
            best = m
    return meta, best


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        await SESSION.close()


INSTRUCTIONS = """\
Gate-level netlist analysis on a persistent OpenROAD session (OpenSTA + OpenDB).

Start with load_design (a bundled case name like "test02", a file name, or a path; the
unit-delay library and LEF are used by default). Conventions:
- Gate = cell instance. Prompt gate names map to library cells: NOT=INV, AND=AND2, OR=OR2,
  NAND=NAND2, NOR=NOR2, XOR=XOR2, XNOR=XNOR2, BUF, DFF.
- Depth = number of gates on a path. Combinational paths start at primary inputs or DFF
  outputs and end at primary outputs or DFF D inputs; DFFs bound every cone.
- Names are literal: pass bus bits as n42[0] (no braces or escaping). A name may be a
  port, a net or a gate; a gate stands for its output signal.
- Constants (1'b0/1'b1) are reported as such and do not block paths.
- "Now" in a question means the current in-memory design, after any renames.
- Lists are paginated (limit/offset); use save_to to write the full list to a file.
Tools by question: gate_counts, io_summary, list_gates, gate_info, fanout, fanout_ranking,
fanin_cone, fanout_cone, cone_intersection, logic_depth, depth_summary, output_cone_stats,
path_exists, articulation_points (also "does every path pass through gate G"),
cut_analysis, constant_inputs, floating_signals, rename_object, write_verilog.
"""

mcp = MCPServer(
    "openroad-netlist",
    title="OpenROAD netlist analysis",
    description="Structural and depth analysis of gate-level netlists with OpenROAD running in WSL.",
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(read_only_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

Name = Annotated[str, Field(description="Port, net or gate name, e.g. n42[0] or g7 (literal, no braces).")]
Limit = Annotated[int, Field(ge=1, le=1_000_000, description="Maximum number of list items to return.")]
Offset = Annotated[int, Field(ge=0, description="Skip this many list items (pagination).")]
SaveTo = Annotated[
    str | None,
    Field(description="Also write the full list to this file (relative paths go to the output directory)."),
]


# ----------------------------- session & files -----------------------------


@mcp.tool(annotations=MUTATING)
async def load_design(
    netlist: Annotated[
        str,
        Field(description='Netlist: a path, a file name, or a case name such as "test02" (bundled netlists).'),
    ],
    top_module: Annotated[str, Field(description="Top module name.")] = "top",
    liberty_files: Annotated[
        list[str], Field(description="Liberty files; default: the bundled unit_delay.lib.")
    ] = [],
    lef_files: Annotated[list[str], Field(description="LEF files; default: the bundled unit_delay.lef.")] = [],
    source_orhelp: Annotated[
        bool, Field(description="Also source the bundled orhelp.tcl helper procs (for run_tcl recipes).")
    ] = True,
    neutralize_constants: Annotated[
        bool,
        Field(description="Mark 1'b0/1'b1 pins as don't-care so constants don't block paths (structural analysis)."),
    ] = True,
) -> dict[str, Any]:
    """Load a gate-level Verilog netlist into a fresh OpenROAD session and summarize it.

    Reads LEF -> Liberty -> Verilog, links the top module and returns gate counts, I/O bit
    counts and constants. Any previously loaded design and unsaved edits are discarded.
    """
    path, resolved = resolve_netlist(netlist)
    libs = [Path(p) for p in liberty_files] or [default_liberty()]
    lefs = [Path(p) for p in lef_files] or [default_lef()]

    def step(command: str, file: Path) -> list[str]:
        return [f"puts {tcl_quote(f'== {command} {file}')}", f"{command} {tcl_file(str(file))}"]

    script: list[str] = []
    for lef in lefs:
        script += step("read_lef", lef)
    for lib in libs:
        script += step("read_liberty", lib)
    script += step("read_verilog", path)
    script.append(f"link_design {tcl_quote(top_module)}")
    if source_orhelp and default_orhelp().is_file():
        script.append(f"source {tcl_file(str(default_orhelp()))}")
    script.append(f"__nl_init {int(neutralize_constants)}")
    script.append("__nl_gate_counts\n__nl_summary")

    await SESSION.restart()
    records, log = parse_records(await SESSION.run("\n".join(script), max_chars=QUERY_MAX_CHARS))

    design = SESSION.design
    design.loaded = True
    design.top_module = top_module
    design.case = path.stem  # type: ignore[attr-defined]
    design.masters = sorted({r[1] for r in by_kind(records, "master")})  # type: ignore[attr-defined]
    design.add("verilog", str(path))
    design.add("liberty", *map(str, libs))
    design.add("lef", *map(str, lefs))

    counts = {r[1]: int(r[2]) for r in by_kind(records, "count")}
    io = first(records, "io") or ["io", "0", "0", "0"]
    seq = seq_types(records)
    constants = {r[2]: int(r[3]) for r in by_kind(records, "cnet")}
    warnings = [line for line in log.splitlines() if "[WARNING" in line or "[ERROR" in line]
    total = sum(counts.values())
    result = {
        "netlist": str(path),
        "resolved": resolved,
        "top_module": top_module,
        "cells": total,
        "by_type": dict(sorted(counts.items())),
        "sequential": sum(n for c, n in counts.items() if c in seq),
        "input_bits": int(io[1]),
        "output_bits": int(io[2]),
        "nets": int(io[3]),
        "constant_pins": constants,
        "constants_neutralized": int((first(records, "neutralized") or ["", "0"])[1]),
        "warnings": warnings,
        "answer": f"Loaded {path.name} (top {top_module}): {total} gates, {io[1]} input bits, {io[2]} output bits.",
    }
    return result


@mcp.tool(annotations=DESTRUCTIVE)
async def write_verilog(
    path: Annotated[
        str | None,
        Field(description="Output file. Default <case>_out.v; relative paths go to the output directory."),
    ] = None,
    restore_constants: Annotated[
        bool, Field(description="Write constants as 1'b0/1'b1 (OpenROAD writes an undeclared one_/zero_ net).")
    ] = True,
    overwrite: Annotated[bool, Field(description="Replace the file if it exists.")] = True,
) -> dict[str, Any]:
    """Write the current design (including renames) as a structural Verilog netlist."""
    SESSION.require_design()
    target = resolve_output(path, getattr(SESSION.design, "case", None))
    if not str(target).startswith("/"):
        target.parent.mkdir(parents=True, exist_ok=True)
    records, log = await query(
        f"__nl_write_verilog {tcl_quote(to_wsl_path(str(target)))} {int(overwrite)} {int(restore_constants)}"
    )
    rec = first(records, "written") or ["written", ""]
    restored = {rec[i]: int(rec[i + 1]) for i in range(2, len(rec) - 1, 2)}
    saved = list(SESSION.design.edits)
    SESSION.design.edits.clear()
    SESSION.design.add("written", str(target))
    return with_log(
        {
            "written": str(target),
            "wsl_path": rec[1],
            "constants_restored": restored,
            "edits_saved": saved,
            "answer": f"Wrote {target}" + (f" ({len(saved)} edit(s) included)." if saved else "."),
        },
        log,
    )


@mcp.tool(annotations=MUTATING)
async def rename_object(
    old_name: Annotated[str, Field(description="Current gate or net name.")],
    new_name: Annotated[str, Field(description="New name (letters, digits, _ and $; must be unused).")],
    kind: Annotated[
        Literal["auto", "gate", "net"], Field(description="What to rename; auto picks the only match.")
    ] = "auto",
) -> dict[str, Any]:
    """Rename a gate (instance) or a net; every reference to it is updated.

    Connectivity and function are unchanged. Port nets and constant nets cannot be renamed.
    The change is in memory until write_verilog.
    """
    old = normalize_name(old_name)
    new = normalize_name(new_name)
    if not VALID_NEW_NAME.match(new):
        raise ToolError(f"Invalid new name {new!r}: use letters, digits, _ and $, not starting with a digit.")
    if kind == "auto":
        records, _ = await query(f"__mcp_rec k [lindex [__nl_odb_object {tcl_quote(old)} auto] 0]")
        kind_tcl = (first(records, "k") or ["k", "net"])[1]
    else:
        kind_tcl = "instance" if kind == "gate" else "net"
    records, log = await query(f"__nl_rename {kind_tcl} {tcl_quote(old)} {tcl_quote(new)}")
    rec = first(records, "renamed")
    what = "gate" if kind_tcl == "instance" else "net"
    SESSION.design.edits.append(f"rename {what} {old} -> {new}")
    conns = [{"pin": r[1], "net" if what == "gate" else "direction": r[2]} for r in by_kind(records, "conn")]
    return with_log(
        {
            "kind": what,
            "old_name": old,
            "new_name": new,
            "connections": conns,
            "visible_to_timing": bool(rec and rec[4] == "1"),
            "unsaved_edits": len(SESSION.design.edits),
            "answer": f"Renamed {what} {old} to {new}; {len(conns)} pin connection(s) now refer to {new}.",
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def session_status(
    check_connection: Annotated[bool, Field(description="Start OpenROAD if needed and report its version.")] = False,
) -> dict[str, Any]:
    """Report the session state, loaded files, unsaved edits and configuration."""
    version = None
    if check_connection:
        version = await SESSION.run(
            'if {[catch {puts "openroad [ord::openroad_version]"}]} {puts "openroad (version unknown)"}'
        )
    design = SESSION.design
    return {
        "running": SESSION.running,
        "pid": SESSION.pid,
        "wsl_distro": CFG.wsl_distro,
        "binary": CFG.binary,
        "command": subprocess.list2cmdline(SESSION.command) if SESSION.command else None,
        "version": version,
        "design_loaded": design.loaded,
        "case": getattr(design, "case", None),
        "files": design.files,
        "unsaved_edits": design.edits,
        "liberty_default": str(default_liberty()),
        "lef_default": str(default_lef()),
        "output_dir": str(output_dir()),
        "raw_tcl_enabled": CFG.allow_raw_tcl,
    }


@mcp.tool(annotations=DESTRUCTIVE)
async def reset_session() -> str:
    """Stop the OpenROAD process, forgetting the loaded design and any unsaved edits."""
    lost = len(SESSION.design.edits)
    await SESSION.close()
    return f"OpenROAD session stopped ({lost} unsaved edits discarded). Call load_design to start again."


# -------------------------------- inventory --------------------------------


@mcp.tool(annotations=READ_ONLY)
async def gate_counts(
    cell_type: Annotated[
        str | None, Field(description="Only count this type (NOT, AND, NAND, DFF, ... or a library cell).")
    ] = None,
) -> dict[str, Any]:
    """Count the gates in the design, in total and per type (AND, OR, NOT, NAND, NOR, XOR, XNOR, BUF, DFF)."""
    records, log = await query("__nl_gate_counts")
    counts = {r[1]: int(r[2]) for r in by_kind(records, "count")}
    masters = sorted({r[1] for r in by_kind(records, "master")} | set(counts))
    seq = seq_types(records) or ({"DFF"} & set(masters))
    by_type = {m: counts.get(m, 0) for m in masters}
    by_user = {user_name(m): n for m, n in by_type.items()}
    by_user = {k: by_user[k] for k in USER_ORDER if k in by_user} | {
        k: v for k, v in by_user.items() if k not in USER_ORDER
    }
    total = sum(counts.values())
    result: dict[str, Any] = {
        "total": total,
        "combinational": sum(n for m, n in counts.items() if m not in seq),
        "sequential": sum(n for m, n in counts.items() if m in seq),
        "by_type": by_user,
        "by_library_cell": by_type,
    }
    if cell_type:
        cell = normalize_cell_type(cell_type, masters)
        result["query"] = {"requested": cell_type, "cell_type": cell, "count": by_type.get(cell, 0)}
        result["answer"] = f"{by_type.get(cell, 0)} {user_name(cell)} gate(s) ({cell})."
    else:
        parts = ", ".join(f"{k} {v}" for k, v in by_user.items())
        result["answer"] = f"{total} gates: {parts}."
    return with_log(result, log)


@mcp.tool(annotations=READ_ONLY)
async def io_summary(
    direction: Annotated[Literal["both", "input", "output"], Field(description="Which ports to report.")] = "both",
    list_bits: Annotated[bool, Field(description="Also list every port bit name.")] = False,
) -> dict[str, Any]:
    """Primary inputs and outputs: number of bits and the ports with their bit widths."""
    records, log = await query("__nl_ports")
    result: dict[str, Any] = {}
    answer = []
    for d in ("input", "output"):
        if direction not in ("both", d):
            continue
        bits = nsort(r[1] for r in by_kind(records, "port") if r[2] == d)
        ports = group_port_bits(bits)
        entry: dict[str, Any] = {"bits": len(bits), "ports": ports}
        if list_bits:
            entry["bit_names"] = bits
        result[d + "s"] = entry
        widths = ", ".join(
            f"{p['name']}[{p['msb']}:{p['lsb']}] ({p['width']} bits)" if p["msb"] is not None else f"{p['name']} (1 bit)"
            for p in ports
        )
        answer.append(f"{len(bits)} {d} bits in {len(ports)} port(s): {widths}")
    result["answer"] = "; ".join(answer) + "."
    return with_log(result, log)


@mcp.tool(annotations=READ_ONLY)
async def list_gates(
    cell_type: Annotated[str | None, Field(description="Gate type (NAND, XOR, DFF, ... or a library cell).")] = None,
    name_pattern: Annotated[str, Field(description="Glob on gate names (* and ?).")] = "*",
    clock_net: Annotated[
        str | None, Field(description="Only flip-flops whose clock pin is on this net (e.g. n0).")
    ] = None,
    include_pins: Annotated[bool, Field(description="Include each gate's pin -> net connections.")] = True,
    offset: Offset = 0,
    limit: Limit = 500,
    save_to: SaveTo = None,
) -> dict[str, Any]:
    """List gates, optionally of one type or clocked by a net, with their input/output signals."""
    cell = cell_type and cell_type_or_error(cell_type)
    clock = normalize_name(clock_net) if clock_net else ""
    records, log = await query(
        f"__nl_list_gates {tcl_quote(cell or '')} {tcl_quote(name_pattern)} {tcl_quote(clock)} {int(include_pins)}"
    )
    rows = sorted(by_kind(records, "gate"), key=lambda r: natural_key(r[1]))
    gates = []
    for r in rows:
        g: dict[str, Any] = {"name": r[1], "cell_type": r[2]}
        if include_pins:
            g["pins"] = dict(p.split("=", 1) for p in r[3:])
        gates.append(g)
    chunk, truncated = page(gates, offset, limit)
    saved = None
    if save_to:
        lines = [f"{g['name']} {g['cell_type']} " + " ".join(f"{k}={v}" for k, v in g.get("pins", {}).items()) for g in gates]
        saved = save_lines(save_to, lines)
    what = (user_name(cell) + " " if cell else "") + "gate" + ("s" if len(gates) != 1 else "")
    clocked = f" clocked by {clock}" if clock else ""
    return with_log(
        {
            "total": len(gates),
            "returned": len(chunk),
            "offset": offset,
            "truncated": truncated,
            "gates": chunk,
            "saved_to": saved,
            "answer": f"{len(gates)} {what}{clocked}"
            + (f": {name_list([g['name'] for g in chunk])}" if chunk else "")
            + (f" (showing {len(chunk)} from offset {offset})." if truncated or offset else "."),
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def gate_info(name: Annotated[str, Field(description="Gate (instance) name, e.g. g0.")]) -> dict[str, Any]:
    """Gate type and pin connections of one gate, with the driver of each input and the loads of the output."""
    records, log = await query(f"__nl_gate_info {q(name)}")
    g = first(records, "gate")
    if not g:
        raise ToolError(f"No gate named {name!r}.")
    drivers: dict[str, list[str]] = {}
    for r in by_kind(records, "drv"):
        drivers.setdefault(r[1], []).append(r[2] if r[3] == "port" else f"{r[2]} ({r[3]})")
    loads = {r[1]: {"gate_pins": int(r[2]), "ports": int(r[3])} for r in by_kind(records, "loads")}
    pins = []
    for r in by_kind(records, "pin"):
        pin = {"pin": r[1], "direction": r[2], "net": r[4] or r[3] or None}
        if r[4]:
            pin["constant"] = r[4]
        if r[1] in drivers:
            pin["driven_by"] = drivers[r[1]]
        if r[1] in loads:
            pin["fanout"] = loads[r[1]]
        pins.append(pin)
    inputs = ", ".join(f"{p['pin']}={p['net']}" for p in pins if p["direction"] == "input")
    outputs = ", ".join(f"{p['pin']}={p['net']}" for p in pins if p["direction"] != "input")
    return with_log(
        {
            "name": g[1],
            "cell_type": g[2],
            "gate_type": user_name(g[2]),
            "pins": pins,
            "answer": f"{g[1]} is {a_an(user_name(g[2]))} gate ({g[2]}); inputs {inputs}; output {outputs}.",
        },
        log,
    )


# ------------------------------ connectivity -------------------------------


@mcp.tool(annotations=READ_ONLY)
async def fanout(
    name: Annotated[str, Field(description="Gate (its output net is used), net, or primary input.")],
    kind: Annotated[Literal["auto", "gate", "net"], Field(description="Disambiguate a name.")] = "auto",
    limit: Limit = 1000,
) -> dict[str, Any]:
    """Direct fanout: the gates driven by a gate's output, by a net or by a primary input.

    Answers "gates driven by g0", "immediate successors of g0", "fanout of input n5",
    "gates connected to the output of g0" and "max fanout of n1".
    """
    records, log = await query(f"__nl_fanout {q(name)} {kind}")
    src = first(records, "source") or ["source", "net", normalize_name(name), ""]
    loads = [r for r in by_kind(records, "conn") if r[2] == "input"]
    drivers = [r for r in by_kind(records, "conn") if r[2] == "output"]
    ports_out = nsort({r[3] for r in by_kind(records, "connport") if r[2] == "output"})
    ports_in = nsort({r[3] for r in by_kind(records, "connport") if r[2] == "input"})
    nets = nsort({r[1] for r in by_kind(records, "conn")} | {r[1] for r in by_kind(records, "connport")})
    gates = nsort({r[3] for r in loads})
    load_list = sorted(({"gate": r[3], "pin": r[4], "cell_type": r[5]} for r in loads), key=lambda d: (natural_key(d["gate"]), d["pin"]))
    chunk, truncated = page(load_list, 0, limit)
    what = f"gate {src[2]} ({user_name(src[3])})" if src[1] == "gate" else src[2]
    via = f" via net {', '.join(nets)}" if src[1] == "gate" and nets else ""
    return with_log(
        {
            "source": {"kind": src[1], "name": src[2], "cell_type": src[3] or None},
            "nets": nets,
            "driver": [f"{r[3]}/{r[4]}" for r in drivers] + [f"input {p}" for p in ports_in],
            "gate_count": len(gates),
            "load_pin_count": len(loads),
            "gates": gates[:limit],
            "loads": chunk,
            "output_ports": ports_out,
            "truncated": truncated or len(gates) > limit,
            "answer": f"{what} drives {len(gates)} gate(s){via} ({len(loads)} load pin(s))"
            + (f" and output port(s) {', '.join(ports_out)}" if ports_out else "")
            + (f": {', '.join(gates[:50])}" + (" ..." if len(gates) > 50 else "") if gates else "")
            + ".",
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def fanout_ranking(
    scope: Annotated[
        Literal["inputs", "nets"], Field(description="Rank primary inputs only, or every net.")
    ] = "inputs",
    top_n: Annotated[int, Field(ge=1, le=10000, description="How many to list (ties at the top are always included).")] = 10,
    include_constants: Annotated[bool, Field(description="Include the 1'b0/1'b1 constant nets.")] = False,
) -> dict[str, Any]:
    """Rank nets or primary inputs by fanout (number of gate input pins they drive); reports ties."""
    records, log = await query(f"__nl_fanout_ranking {scope} {top_n} {int(include_constants)}")
    rows = [{"net": r[1], "fanout": int(r[2])} for r in by_kind(records, "fo")]
    top = rows[0]["fanout"] if rows else 0
    leaders = [r["net"] for r in rows if r["fanout"] == top] if rows else []
    what = "primary input" if scope == "inputs" else "net"
    if not rows:
        answer = f"No {what}s found."
    elif len(leaders) == 1:
        answer = f"The {what} with the highest fanout is {leaders[0]} ({top} gate input pins)."
    else:
        answer = f"Tie: {', '.join(leaders)} each drive {top} gate input pins."
    return with_log(
        {
            "scope": scope,
            "considered": int((first(records, "considered") or ["", "0"])[1]),
            "max_fanout": top,
            "leaders": leaders,
            "ranking": rows[: max(top_n, len(leaders))],
            "answer": answer,
        },
        log,
    )


def _cells(records: list[list[str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    comb, seq = [], []
    for r in by_kind(records, "cell"):
        (seq if r[3] == "1" else comb).append({"name": r[1], "cell_type": r[2]})
    key = lambda d: natural_key(d["name"])  # noqa: E731
    return sorted(comb, key=key), sorted(seq, key=key)


def _type_counts(cells: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in cells:
        k = user_name(c["cell_type"])
        counts[k] = counts.get(k, 0) + 1
    return {k: counts[k] for k in USER_ORDER if k in counts} | {k: v for k, v in counts.items() if k not in USER_ORDER}


@mcp.tool(annotations=READ_ONLY)
async def fanin_cone(
    node: Annotated[str, Field(description="Output port, net or gate whose transitive fanin to compute.")],
    include_boundary_dffs: Annotated[
        bool, Field(description="Also count the flip-flops where the cone stops (default: combinational only).")
    ] = False,
    list_gates: Annotated[bool, Field(description="Return the gate names (not just counts).")] = True,
    limit: Limit = 5000,
    save_to: SaveTo = None,
) -> dict[str, Any]:
    """Transitive fanin (logic) cone of a signal: its size, gates and per-type counts.

    The cone stops at primary inputs and flip-flops (DFF outputs act as inputs); boundary
    DFFs are reported separately. Answers fanin cone / cone size / gate types in a cone.
    """
    records, log = await query(f"__nl_fanin_cone {q(node)}")
    comb, seq = _cells(records)
    names = [c["name"] for c in comb] + ([c["name"] for c in seq] if include_boundary_dffs else [])
    names = nsort(names)
    starts = nsort(r[1] for r in by_kind(records, "start"))
    chunk, truncated = page(names, 0, limit)
    saved = save_lines(save_to, names) if save_to else None
    n = normalize_name(node)
    result: dict[str, Any] = {
        "node": n,
        "size": len(names),
        "combinational_gates": len(comb),
        "by_type": _type_counts(comb + (seq if include_boundary_dffs else [])),
        "boundary_dffs": {"count": len(seq), "gates": [c["name"] for c in seq][:limit]},
        "primary_inputs": starts[:limit],
        "primary_input_count": len(starts),
        "truncated": truncated,
        "saved_to": saved,
    }
    if list_gates:
        result["gates"] = chunk
    result["answer"] = (
        f"The fanin cone of {n} has {len(names)} gate(s)"
        + (f" (plus {len(seq)} boundary DFF(s))" if seq and not include_boundary_dffs else "")
        + (f": {', '.join(names[:50])}" + (" ..." if len(names) > 50 else "") if names else "")
        + "."
    )
    return with_log(result, log)


@mcp.tool(annotations=READ_ONLY)
async def fanout_cone(
    node: Annotated[str, Field(description="Primary input, net or gate whose transitive fanout to compute.")],
    include_boundary_dffs: Annotated[bool, Field(description="Also count the flip-flops where the cone stops.")] = False,
    list_gates: Annotated[bool, Field(description="Return the gate names (not just counts).")] = True,
    limit: Limit = 5000,
    save_to: SaveTo = None,
) -> dict[str, Any]:
    """Transitive fanout cone (all gates reachable) from a signal, stopping at flip-flops.

    The source's own driver gate is not included. Also lists the primary outputs reached.
    Answers "transitive fanout of n0" and "all gates reachable from n2".
    """
    records, log = await query(f"__nl_fanout_cone {q(node)}")
    node_rec = first(records, "node") or ["node", "", "", ""]
    driver = node_rec[3] if len(node_rec) > 3 else ""
    comb, seq = _cells(records)
    comb = [c for c in comb if c["name"] != driver]
    seq = [c for c in seq if c["name"] != driver]
    names = nsort([c["name"] for c in comb] + ([c["name"] for c in seq] if include_boundary_dffs else []))
    pos = nsort({r[1] for r in by_kind(records, "po")})
    chunk, truncated = page(names, 0, limit)
    saved = save_lines(save_to, names) if save_to else None
    n = normalize_name(node)
    result: dict[str, Any] = {
        "node": n,
        "size": len(names),
        "combinational_gates": len(comb),
        "by_type": _type_counts(comb + (seq if include_boundary_dffs else [])),
        "boundary_dffs": {"count": len(seq), "gates": [c["name"] for c in seq][:limit]},
        "output_ports": pos[:limit],
        "output_port_count": len(pos),
        "truncated": truncated,
        "saved_to": saved,
    }
    if list_gates:
        result["gates"] = chunk
    result["answer"] = (
        f"{len(names)} gate(s) are reachable from {n}"
        + (f" (plus {len(seq)} DFF(s) where the cone stops)" if seq and not include_boundary_dffs else "")
        + (f": {', '.join(names[:50])}" + (" ..." if len(names) > 50 else "") if names else "")
        + "."
    )
    return with_log(result, log)


@mcp.tool(annotations=READ_ONLY)
async def cone_intersection(
    nodes: Annotated[list[str], Field(min_length=2, description="Two or more signals.")],
    direction: Annotated[Literal["fanin", "fanout"], Field(description="Intersect fanin or fanout cones.")] = "fanin",
    limit: Limit = 5000,
) -> dict[str, Any]:
    """Gates shared by the (combinational) fanin or fanout cones of several signals."""
    proc = "__nl_fanin_cone" if direction == "fanin" else "__nl_fanout_cone"
    sets = []
    sizes = {}
    for node in nodes:
        records, _ = await query(f"{proc} {q(node)}")
        node_rec = first(records, "node") or ["node", "", "", ""]
        driver = node_rec[3] if direction == "fanout" and len(node_rec) > 3 else ""
        comb, _seq = _cells(records)
        names = {c["name"] for c in comb if c["name"] != driver}
        sets.append(names)
        sizes[normalize_name(node)] = len(names)
    shared = nsort(set.intersection(*sets))
    label = " and ".join(normalize_name(n) for n in nodes)
    return {
        "direction": direction,
        "cone_sizes": sizes,
        "shared_count": len(shared),
        "shared_gates": shared[:limit],
        "truncated": len(shared) > limit,
        "answer": f"The {direction} cones of {label} share {len(shared)} gate(s)"
        + (f": {', '.join(shared[:50])}" + (" ..." if len(shared) > 50 else "") if shared else "")
        + ".",
    }


# ---------------------------------- depth ----------------------------------


@mcp.tool(annotations=READ_ONLY)
async def logic_depth(
    to_node: Annotated[str, Field(description="Endpoint: output port, net or gate.")],
    from_node: Annotated[
        str | None, Field(description="Start signal; default: any primary input or flip-flop output.")
    ] = None,
) -> dict[str, Any]:
    """Maximum logic depth (number of gates) of the longest combinational path to a signal.

    With from_node: the longest path from that signal to to_node ("no path" if none).
    Without: the depth of to_node's fanin cone, from any primary input or DFF output.
    """
    to = normalize_name(to_node)
    frm = normalize_name(from_node) if from_node else ""
    if frm and frm == to:
        return {"to": to, "from": frm, "exists": True, "depth": 0, "gates_on_path": [], "answer": "Same signal: depth 0."}
    records, log = await query(f"__nl_depth {tcl_quote(to)} {tcl_quote(frm)}")
    meta, best = best_measure(records)
    if meta["sink_kind"] == "input" and not frm:
        best = {"depth": 0, "startpoint": to, "gates": []}
    exists = best is not None
    result: dict[str, Any] = {
        "to": to,
        "from": frm or None,
        "exists": exists,
        "depth": best["depth"] if best else None,
        "startpoint": best["startpoint"] if best else None,
        "gates_on_path": best["gates"] if best else [],
    }
    if not exists:
        result["answer"] = f"No combinational path {'from ' + frm + ' ' if frm else ''}to {to}."
    else:
        result["answer"] = (
            f"Maximum logic depth {'from ' + frm + ' ' if frm else ''}to {to}: {best['depth']} gate(s)"
            + (f" (longest path starts at {best['startpoint']})" if not frm else "")
            + "."
        )
    return with_log(result, log)


def _group(paths: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    best = None
    for p in paths:
        if p["tag"] != tag:
            continue
        adj = 1 if tag.startswith("reg") else 0
        gates = [g for _pin, _arr, g in p["points"][1:] if g]
        d = {"depth": p["arrival"] - adj, "startpoint": p["start"], "endpoint": p["end"], "gates": gates}
        if best is None or d["depth"] > best["depth"]:
            best = d
    return best


@mcp.tool(annotations=READ_ONLY)
async def depth_summary(
    through_gate: Annotated[
        str | None,
        Field(description="Also report the longest path through this gate and whether it is on a max-depth path."),
    ] = None,
) -> dict[str, Any]:
    """Maximum combinational depth of the design, per path group, with the critical path.

    Groups: primary input -> primary output, input -> DFF D pin, DFF -> DFF (register to
    register) and DFF -> output; "overall" is the maximum of all. Depth counts gates only
    (the flip-flop's own clock-to-Q stage is not counted).
    """
    records, log = await query("__nl_depth_summary {}")
    paths = paths_from(records)
    groups = {tag: _group(paths, tag) for tag in ("pi_to_po", "pi_to_reg", "reg_to_reg", "reg_to_po")}
    present = [g for g in groups.values() if g]
    overall = max(present, key=lambda g: g["depth"]) if present else None
    result: dict[str, Any] = {**groups, "overall": overall}
    answer = [f"Maximum combinational depth: {overall['depth'] if overall else 'none (no paths)'}"]
    for tag, label in (("pi_to_po", "input->output"), ("pi_to_reg", "input->DFF"), ("reg_to_reg", "DFF->DFF"), ("reg_to_po", "DFF->output")):
        if groups[tag]:
            answer.append(f"{label} {groups[tag]['depth']}")
    if through_gate:
        g = normalize_name(through_gate)
        t_records, t_log = await query(f"__nl_depth_summary {tcl_quote(g)}")
        log = "\n".join(x for x in (log, t_log) if x)
        t_paths = paths_from(t_records)
        through = [x for x in (_group(t_paths, t) for t in groups) if x]
        through_max = max((x["depth"] for x in through), default=None)
        on_max = overall is not None and through_max == overall["depth"]
        result["through"] = {
            "gate": g,
            "max_depth_through": through_max,
            "on_max_depth_path": on_max,
        }
        answer.append(
            f"gate {g}: longest path through it {through_max if through_max is not None else 'none'}, "
            + ("so it IS on a maximum-depth path" if on_max else "so it is NOT on a maximum-depth path")
        )
    result["answer"] = "; ".join(answer) + "."
    return with_log(result, log)


@mcp.tool(annotations=READ_ONLY)
async def output_cone_stats(
    metrics: Annotated[
        list[Literal["depth", "cone_size"]], Field(description="Per-output metrics to compute.")
    ] = ["depth"],
    threshold: Annotated[
        int | None, Field(description="Count outputs whose depth is greater than this.")
    ] = None,
    top_n: Annotated[int, Field(ge=1, le=100000, description="How many outputs to list per metric.")] = 20,
) -> dict[str, Any]:
    """Per-output-bit depth and/or fanin cone size: deepest outputs, largest cones, counts over a threshold."""
    result: dict[str, Any] = {}
    answer = []
    log_all = []
    if "depth" in metrics:
        records, log = await query(
            "set __n [llength [all_outputs]]\n"
            "__nl_endpoint_paths pi [all_inputs] [all_outputs] $__n\n"
            "__nl_endpoint_paths reg [all_registers -clock_pins] [all_outputs] $__n\n"
            "foreach __o [all_outputs] { __mcp_rec out [get_full_name $__o] }"
        )
        log_all.append(log)
        depth = {r[1]: -1 for r in by_kind(records, "out")}
        for p in paths_from(records):
            d = p["arrival"] - (1 if p["tag"] == "reg" else 0)
            depth[p["end"]] = max(depth.get(p["end"], -1), d)
        ranked = sorted(depth.items(), key=lambda kv: (-kv[1], natural_key(kv[0])))
        top = ranked[0][1] if ranked else -1
        deepest = [n for n, d in ranked if d == top and top >= 0]
        result["depth"] = {
            "max_depth": top if top >= 0 else None,
            "deepest_outputs": deepest,
            "per_output": [{"output": n, "depth": d} for n, d in ranked[:top_n]],
            "no_path_outputs": nsort(n for n, d in depth.items() if d < 0),
            "outputs": len(depth),
        }
        answer.append(f"deepest output(s): {', '.join(deepest) or 'none'} (depth {top})")
        if threshold is not None:
            over = nsort(n for n, d in depth.items() if d > threshold)
            result["depth"]["threshold"] = threshold
            result["depth"]["count_over_threshold"] = len(over)
            result["depth"]["outputs_over_threshold"] = over
            answer.append(f"{len(over)} of {len(depth)} output(s) have depth > {threshold}")
    if "cone_size" in metrics:
        records, log = await query("__nl_output_cones")
        log_all.append(log)
        sizes = {r[1]: int(r[2]) for r in by_kind(records, "cone")}
        ranked = sorted(sizes.items(), key=lambda kv: (-kv[1], natural_key(kv[0])))
        top = ranked[0][1] if ranked else 0
        largest = [n for n, s in ranked if s == top]
        result["cone_size"] = {
            "max_size": top,
            "largest_outputs": largest,
            "per_output": [{"output": n, "size": s} for n, s in ranked[:top_n]],
        }
        answer.append(f"largest fanin cone: {', '.join(largest)} ({top} gates)")
    result["answer"] = "; ".join(answer) + "."
    return with_log(result, "\n".join(x for x in log_all if x))


# ---------------------------------- paths ----------------------------------


@mcp.tool(annotations=READ_ONLY)
async def path_exists(
    from_node: Annotated[str, Field(description="Start signal (usually a primary input).")],
    to_node: Annotated[str, Field(description="End signal (usually a primary output).")],
    avoid: Annotated[
        list[str],
        Field(description="Signals or gates the path must not traverse (a net blocks the gate driving it)."),
    ] = [],
) -> dict[str, Any]:
    """Does a combinational path from one signal to another exist (optionally avoiding nodes)?

    Returns yes/no and, if yes, one such path (its gates) and the longest remaining depth.
    """
    frm, to = normalize_name(from_node), normalize_name(to_node)
    avoid_names = [normalize_name(a) for a in avoid]
    if frm == to and to not in avoid_names:
        return {"from": frm, "to": to, "exists": True, "depth": 0, "gates_on_path": [], "answer": "Yes (same signal)."}
    avoid_tcl = "[list " + " ".join(tcl_quote(a) for a in avoid_names) + "]"
    records, log = await query(f"__nl_path_exists {tcl_quote(frm)} {tcl_quote(to)} {avoid_tcl}")
    avoided = [
        {"name": r[1], "blocks": r[2], "object": r[3]} for r in by_kind(records, "avoid")
    ]
    blocked_source = any(r[2] == "input" and len(r) > 4 and r[4] == "1" for r in by_kind(records, "avoid"))
    _meta, best = best_measure(records)
    exists = (first(records, "reach") or ["reach", "0"])[1] == "1" and not blocked_source
    avoiding = f" avoiding {', '.join(avoid_names)}" if avoid_names else ""
    return with_log(
        {
            "from": frm,
            "to": to,
            "avoid": avoided,
            "exists": exists,
            "depth": best["depth"] if exists and best else None,
            "gates_on_path": best["gates"] if exists and best else [],
            "answer": (
                f"Yes: a combinational path from {frm} to {to}{avoiding} exists"
                + (
                    f" (e.g. through {', '.join(best['gates'][:20]) or 'no gates'}; longest such path {best['depth']} gate(s))."
                    if best
                    else "."
                )
                if exists
                else f"No: there is no combinational path from {frm} to {to}{avoiding}."
            ),
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def articulation_points(
    from_node: Annotated[str, Field(description="Start signal.")],
    to_node: Annotated[str, Field(description="End signal.")],
    gate: Annotated[
        str | None,
        Field(description='Only check this gate: "does every path from -> to pass through it?" (path dominance).'),
    ] = None,
) -> dict[str, Any]:
    """Gates whose removal disconnects from_node from to_node (they lie on every path).

    With gate: answers whether every combinational path from from_node to to_node passes
    through that gate. Reports when there is no combinational path at all.
    """
    frm, to = normalize_name(from_node), normalize_name(to_node)
    g = normalize_name(gate) if gate else ""
    records, log = await query(f"__nl_artic {tcl_quote(frm)} {tcl_quote(to)} {tcl_quote(g)}")
    if first(records, "nopath"):
        return with_log(
            {
                "from": frm,
                "to": to,
                "path_exists": False,
                "articulation_points": [],
                "answer": f"There is no combinational path from {frm} to {to}"
                + (f", so not every path passes through {g} (there are none)." if g else "; no articulation points."),
            },
            log,
        )
    cands = [(r[1], r[2] == "1") for r in by_kind(records, "cand")]
    points = nsort(name for name, blocked in cands if blocked)
    result: dict[str, Any] = {"from": frm, "to": to, "path_exists": True}
    if g:
        every = bool(cands) and cands[0][1]
        result["gate_check"] = {"gate": cands[0][0] if cands else g, "every_path_passes": every}
        result["answer"] = (
            f"Yes: every combinational path from {frm} to {to} passes through {g}."
            if every
            else f"No: some combinational path from {frm} to {to} avoids {g}."
        )
    else:
        result["articulation_points"] = points
        result["candidates_checked"] = len(cands)
        result["answer"] = (
            f"{len(points)} articulation point(s) between {frm} and {to}: {', '.join(points)}."
            if points
            else f"No single gate disconnects {frm} from {to}."
        )
    return with_log(result, log)


@mcp.tool(annotations=READ_ONLY)
async def cut_analysis(
    wire: Annotated[str, Field(description="Net (wire) to test.")],
    stop_at_first: Annotated[bool, Field(description="Stop at the first disconnected pair (yes/no answer).")] = False,
    limit: Limit = 500,
    time_budget_s: Annotated[float, Field(gt=0, le=36000, description="Give up after this many seconds.")] = 300,
) -> dict[str, Any]:
    """Is a wire a cut between primary inputs and outputs?

    Lists the input->output pairs that are connected but lose every path when the wire is
    blocked (the wire itself is not counted as an output). Empty = not a cut.
    """
    w = normalize_name(wire)
    records, log = await query(
        f"__nl_cut {tcl_quote(w)} {int(stop_at_first)} {int(time_budget_s * 1000)}",
        timeout=max(CFG.timeout, time_budget_s + 120),
    )
    pairs = [f"{r[1]}->{r[2]}" for r in by_kind(records, "pair")]
    done = first(records, "done") or ["done", "0", "0", "1"]
    complete = done[3] == "1"
    is_cut = bool(pairs)
    return with_log(
        {
            "wire": w,
            "is_cut": is_cut,
            "pairs_count": len(pairs),
            "pairs": pairs[:limit],
            "inputs_checked": int(done[1]),
            "inputs_in_fanin": int(done[2]),
            "complete": complete,
            "answer": (
                f"Yes: {w} is a cut; blocking it disconnects {len(pairs)} input->output pair(s)"
                + (f" (e.g. {', '.join(pairs[:5])})" if pairs else "")
                + "."
                if is_cut
                else f"No: {w} is not a cut between any primary input and primary output."
                + ("" if complete else " (time budget exceeded; the check is incomplete)")
            ),
        },
        log,
    )


# --------------------------------- checks ----------------------------------


@mcp.tool(annotations=READ_ONLY)
async def constant_inputs(
    value: Annotated[Literal["0", "1", "any"], Field(description="Constant value to look for.")] = "any",
    cell_type: Annotated[str | None, Field(description="Only this gate type (NAND, AND, OR, NOR, DFF, ...).")] = None,
    limit: Limit = 1000,
) -> dict[str, Any]:
    """Gates with one or more input pins tied to constant 0 (1'b0) or 1 (1'b1)."""
    cell = cell_type_or_error(cell_type) if cell_type else ""
    records, log = await query(f"__nl_constant_inputs {value} {tcl_quote(cell)}")
    per_gate: dict[str, dict[str, Any]] = {}
    for r in by_kind(records, "cload"):
        g = per_gate.setdefault(r[1], {"gate": r[1], "cell_type": r[2], "pins": {}})
        g["pins"][r[3]] = r[4]
    gates = sorted(per_gate.values(), key=lambda d: natural_key(d["gate"]))
    by_type: dict[str, int] = {}
    for g in gates:
        by_type[user_name(g["cell_type"])] = by_type.get(user_name(g["cell_type"]), 0) + 1
    label = {"0": "constant 0", "1": "constant 1", "any": "a constant"}[value]
    what = (user_name(cell) + " " if cell else "") + "gates"
    names = [g["gate"] for g in gates]
    return with_log(
        {
            "value": value,
            "cell_type": cell or None,
            "count": len(gates),
            "by_type": by_type,
            "gates": gates[:limit],
            "truncated": len(gates) > limit,
            "answer": (
                f"{len(gates)} {what} have an input tied to {label}"
                + (f": {', '.join(names[:50])}" + (" ..." if len(names) > 50 else "") if names else "")
                + "."
                if gates
                else f"No {what} have an input tied to {label}."
            ),
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def floating_signals(limit: Limit = 1000) -> dict[str, Any]:
    """Floating / unconnected signals: unused inputs, undriven outputs, dangling nets, open pins."""
    records, log = await query("__nl_floating")
    groups = {
        "unused_inputs": nsort(r[1] for r in by_kind(records, "unused_input")),
        "undriven_outputs": nsort(r[1] for r in by_kind(records, "undriven_output")),
        "dangling_nets": nsort(r[1] for r in by_kind(records, "dangling")),
        "undriven_nets": nsort(r[1] for r in by_kind(records, "undriven_net")),
        "unconnected_pins": nsort(f"{r[1]}/{r[2]}" for r in by_kind(records, "unconnected")),
    }
    result: dict[str, Any] = {k: {"count": len(v), "names": v[:limit]} for k, v in groups.items()}
    parts = [f"{len(v)} {k.replace('_', ' ')}" + (f" ({', '.join(v[:20])}{' ...' if len(v) > 20 else ''})" if v else "") for k, v in groups.items()]
    clean = not any(groups.values())
    result["clean"] = clean
    result["answer"] = "No floating or unconnected signals." if clean else "; ".join(parts) + "."
    return with_log(result, log)


if CFG.allow_raw_tcl:

    @mcp.tool(annotations=DESTRUCTIVE)
    async def run_tcl(
        script: Annotated[str, Field(description="Tcl script to evaluate in the OpenROAD session.")],
    ) -> str:
        """Run an arbitrary OpenROAD Tcl script in the live session (escape hatch).

        The orhelp.tcl procs (node, inst_names, gdepth, ...) are available after load_design.
        Do not call `exit`. Edits made here are not tracked as unsaved edits.
        """
        output = await SESSION.run(script)
        return output or "(no output)"


# --------------------------------- prompts ---------------------------------


@mcp.prompt()
def netlist_question_guide() -> str:
    """Which tool answers which kind of netlist question."""
    return """Answer questions about the gate-level netlist loaded in the OpenROAD session.

- New testcase / load: load_design (case name, e.g. test02); write: write_verilog (<case>_out.v).
- Gate counts (total, per type, "how many NOT gates"): gate_counts.
- Inputs/outputs and bit widths: io_summary.
- List gates of a type / flip-flops on a clock: list_gates (cell_type, clock_net).
- Type and pins of a gate: gate_info.
- Gates driven by a gate/net/input, successors, "connected to the output of": fanout.
- Highest-fanout input: fanout_ranking.
- Fanin cone, cone size, gate types in a cone: fanin_cone. Fanout cone / reachable gates: fanout_cone.
- Gates shared by two cones: cone_intersection.
- Depth from A to B, depth of a cone: logic_depth. Design-wide max depth, input->DFF,
  register->register, "is gate G on a max-depth path": depth_summary.
- Outputs deeper than N, deepest output, largest fanin cone: output_cone_stats.
- Path from A to B avoiding X: path_exists. "Does every path pass through G", articulation
  points: articulation_points. "Is wire W a cut": cut_analysis.
- Gates with constant inputs: constant_inputs. Floating inputs/outputs: floating_signals.
- Rename a gate or wire: rename_object (then later questions refer to the new name).

Report the tool's answer, including ties and "no path" cases."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
