"""MCP server exposing the OpenROAD OpenDB (odb) physical design database.

Runs on Windows and drives one persistent ``openroad`` process inside WSL (see
``openroad_common.session``), reaching OpenDB through its SWIG Tcl API. Tcl helper
procs print tab-separated records (``__mcp_rec``) that are turned into JSON here.
All coordinates and sizes going in and out of the tools are in microns.
"""

from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from openroad_common import (
    Config,
    OpenRoadSession,
    parse_records,
    tcl_file,
    tcl_quote,
    to_wsl_path,
)

CFG = Config.from_env("ODB")

# ODB helper procs added to the shared Tcl driver.
ODB_DRIVER_TCL = r"""
proc __odb_null {obj} {
  expr {$obj eq "" || $obj eq "NULL"}
}

# Evaluate a script in the caller's scope; return a default instead of failing.
proc __odb_try {script {default ""}} {
  if {[catch {uplevel 1 $script} result]} {
    return $default
  }
  return $result
}

proc __odb_block {} {
  set block [ord::get_db_block]
  if {[__odb_null $block]} {
    error "No design block is loaded (read a DEF/ODB or link a Verilog netlist)."
  }
  return $block
}

# Rect or dbBox -> {xMin yMin xMax yMax} in DBU.
proc __odb_bbox {obj} {
  list [$obj xMin] [$obj yMin] [$obj xMax] [$obj yMax]
}

proc __odb_match {objs pattern} {
  set out {}
  foreach obj $objs {
    if {[string match $pattern [$obj getName]]} {
      lappend out $obj
    }
  }
  return $out
}

# Exact name first (fast), then glob match over the whole collection.
proc __odb_insts {pattern} {
  set block [__odb_block]
  set obj [$block findInst $pattern]
  if {![__odb_null $obj]} { return [list $obj] }
  return [__odb_match [$block getInsts] $pattern]
}

proc __odb_nets {pattern} {
  set block [__odb_block]
  set obj [$block findNet $pattern]
  if {![__odb_null $obj]} { return [list $obj] }
  return [__odb_match [$block getNets] $pattern]
}

proc __odb_bterms {pattern} {
  set block [__odb_block]
  set obj [$block findBTerm $pattern]
  if {![__odb_null $obj]} { return [list $obj] }
  return [__odb_match [$block getBTerms] $pattern]
}

proc __odb_inst {name} {
  set inst [[__odb_block] findInst $name]
  if {[__odb_null $inst]} { error "Instance not found: $name" }
  return $inst
}

proc __odb_net {name} {
  set net [[__odb_block] findNet $name]
  if {[__odb_null $net]} { error "Net not found: $name" }
  return $net
}

proc __odb_master {name} {
  set master [[ord::get_db] findMaster $name]
  if {[__odb_null $master]} { error "Master (library cell) not found: $name" }
  return $master
}

# Detailed-route wire length (microns) per net, as a dict net -> length.
# dbWire::getLength returns an unwrapped uint64_t in Tcl, so this uses OpenROAD's
# report_wire_length CSV output instead. Nets without routing are simply absent.
proc __odb_wirelengths {{pattern *}} {
  set path "/tmp/mcp_odb_wl_[pid].csv"
  file delete -force $path
  foreach id {239 240 241} { catch {suppress_message GRT $id} }
  set rc [catch {report_wire_length -net $pattern -detailed_route -file $path} err]
  foreach id {239 240 241} { catch {unsuppress_message GRT $id} }
  set lengths [dict create]
  if {$rc || ![file exists $path]} {
    return $lengths
  }
  set f [open $path]
  set lines [split [read $f] "\n"]
  close $f
  file delete -force $path
  foreach line [lrange $lines 1 end] {
    if {[regexp {^\S+\s+(.+)\s+(\S+)\s+(\S+)\s*$} $line -> net wl pins]} {
      dict set lengths $net $wl
    }
  }
  return $lengths
}

proc __odb_inst_rec {inst} {
  set loc [$inst getLocation]
  __mcp_rec inst [$inst getName] [[$inst getMaster] getName] [lindex $loc 0] [lindex $loc 1] \
    [$inst getOrient] [$inst getPlacementStatus] {*}[__odb_bbox [$inst getBBox]]
}

proc __odb_net_rec {net lengths} {
  set name [$net getName]
  set wl [expr {[dict exists $lengths $name] ? [dict get $lengths $name] : 0}]
  __mcp_rec net $name [$net getSigType] [llength [$net getITerms]] \
    [llength [$net getBTerms]] $wl [$net isSpecial] [llength [$net getSWires]] \
    [expr {[__odb_null [$net getWire]] ? 0 : 1}]
}
"""

SESSION = OpenRoadSession(CFG, "odb", ODB_DRIVER_TCL)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def tcl(template: str, **values: Any) -> str:
    """Fill ``@NAME@`` placeholders in a Tcl template (values must already be Tcl-safe)."""
    for key, value in values.items():
        template = template.replace(f"@{key}@", str(value))
    return template


def dbu_per_micron() -> int:
    return SESSION.design.dbu_per_micron or 1000


def um(value: str | int | float) -> float:
    """DBU -> microns."""
    return round(float(value) / dbu_per_micron(), 4)


def um2(value: str | int | float) -> float:
    """DBU^2 -> square microns."""
    return round(float(value) / dbu_per_micron() ** 2, 4)


def to_dbu(microns: float) -> int:
    return round(microns * dbu_per_micron())


def number(value: str) -> int | float | str:
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def flag(value: str) -> bool:
    return value in ("1", "true")


def bbox(fields: list[str]) -> list[float]:
    return [um(v) for v in fields[:4]]


def inst_dict(rec: list[str]) -> dict[str, Any]:
    # inst name master x y orient status x1 y1 x2 y2
    return {
        "name": rec[1],
        "master": rec[2],
        "location": [um(rec[3]), um(rec[4])],
        "orient": rec[5],
        "status": rec[6],
        "bbox": bbox(rec[7:11]),
    }


def net_dict(rec: list[str]) -> dict[str, Any]:
    # net name sig_type iterms bterms routed_um special swires has_wire
    return {
        "name": rec[1],
        "sig_type": rec[2],
        "inst_pins": int(rec[3]),
        "ports": int(rec[4]),
        "routed_length_um": round(float(rec[5]), 4),
        "special": flag(rec[6]),
        "special_wires": int(rec[7]),
        "has_detailed_wire": flag(rec[8]),
    }


def db_status(status: str) -> str:
    """OpenDB has no FIXED status: DEF FIXED is stored as FIRM."""
    return "FIRM" if status == "FIXED" else status


def by_kind(records: list[list[str]], kind: str) -> list[list[str]]:
    return [r for r in records if r and r[0] == kind]


def first(records: list[list[str]], kind: str) -> list[str]:
    matches = by_kind(records, kind)
    if not matches:
        raise ToolError(f"OpenROAD returned no '{kind}' data.")
    return matches[0]


def total(records: list[list[str]]) -> int:
    matches = by_kind(records, "total")
    return int(matches[0][1]) if matches else 0


def with_log(result: dict[str, Any], log: str) -> dict[str, Any]:
    if log:
        result["log"] = log
    return result


QUERY_MAX_CHARS = 20_000_000


async def query(script: str) -> tuple[list[list[str]], str]:
    """Run a structured query. Records are never truncated (tools bound them with limits)."""
    SESSION.require_design()
    return parse_records(await SESSION.run(script, max_chars=QUERY_MAX_CHARS))


def record_edit(description: str) -> None:
    SESSION.design.edits.append(description)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        await SESSION.close()


mcp = MCPServer(
    "openroad-odb",
    title="OpenROAD OpenDB",
    description="Inspect and edit the OpenROAD physical design database (OpenDB) running in WSL.",
    instructions=(
        "Physical design database (OpenDB) on a persistent OpenROAD session. Start with load_design "
        "(LEF + DEF/ODB, or LEF + Verilog). All coordinates and sizes are in microns. Inspect with "
        "design_summary, find_instances, net_info, placement_report, wirelength_report. Edits "
        "(place_instance, move_instances, swap_master, connect_pin, ...) stay in memory until "
        "write_def or write_db; session_status lists unsaved edits."
    ),
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(read_only_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

# FIXED is accepted for convenience and stored as FIRM (OpenDB's name for DEF FIXED).
PlacementStatus = Literal["PLACED", "FIXED", "FIRM", "LOCKED", "UNPLACED", "SUGGESTED", "COVER", "NONE"]
Orient = Literal["R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"]
SigType = Literal["SIGNAL", "POWER", "GROUND", "CLOCK", "ANALOG", "RESET", "SCAN", "TIEOFF"]

PathList = Annotated[list[str], Field(description="Absolute file paths (Windows or WSL).")]
Pattern = Annotated[str, Field(description="Exact name or Tcl glob pattern (* and ?; escape [ ] with \\).")]
Limit = Annotated[int, Field(ge=1, le=10000, description="Maximum number of results to return.")]
Microns = Annotated[float, Field(description="Coordinate in microns.")]


# ----------------------------- session & files -----------------------------


@mcp.tool(annotations=MUTATING)
async def load_design(
    lef_files: Annotated[
        list[str], Field(description="LEF files, technology LEF first. Required unless db_file is given.")
    ] = [],
    def_file: Annotated[str | None, Field(description="DEF file (netlist + placement/routing).")] = None,
    db_file: Annotated[str | None, Field(description="OpenROAD .odb database.")] = None,
    verilog_files: Annotated[list[str], Field(description="Gate-level Verilog netlist files.")] = [],
    top_module: Annotated[str | None, Field(description="Top module name (with verilog_files).")] = None,
    liberty_files: Annotated[list[str], Field(description="Optional Liberty files.")] = [],
) -> dict[str, Any]:
    """Load a design into a fresh OpenROAD session and return its summary.

    Give exactly one netlist source: def_file, db_file, or verilog_files + top_module.
    Reads LEF -> Liberty -> netlist. Any previously loaded design and unsaved edits are discarded.
    """
    sources = sum([def_file is not None, db_file is not None, bool(verilog_files)])
    if sources != 1:
        raise ToolError("Give exactly one netlist source: def_file, db_file, or verilog_files (+ top_module).")
    if verilog_files and not top_module:
        raise ToolError("top_module is required with verilog_files.")
    if not lef_files and db_file is None:
        raise ToolError("LEF files are required (technology LEF first) unless db_file is given.")

    def step(command: str, path: str) -> list[str]:
        return [f"puts {tcl_quote(f'== {command} {path}')}", f"{command} {tcl_file(path)}"]

    script: list[str] = []
    for lef in lef_files:
        script += step("read_lef", lef)
    for lib in liberty_files:
        script += step("read_liberty", lib)
    if db_file:
        script += step("read_db", db_file)
    elif def_file:
        script += step("read_def", def_file)
    else:
        for v in verilog_files:
            script += step("read_verilog", v)
        script += [f"puts {tcl_quote('== link_design ' + str(top_module))}", f"link_design {tcl_quote(top_module)}"]
    script.append("__odb_block")
    script.append("__mcp_rec dbu [[ord::get_db_tech] getDbUnitsPerMicron]")

    await SESSION.restart()
    records, log = parse_records(await SESSION.run("\n".join(script), max_chars=QUERY_MAX_CHARS))

    design = SESSION.design
    design.loaded = True
    design.top_module = top_module
    design.dbu_per_micron = int(first(records, "dbu")[1])
    design.add("lef", *lef_files)
    design.add("liberty", *liberty_files)
    design.add("verilog", *verilog_files)
    for kind, path in (("def", def_file), ("db", db_file)):
        if path:
            design.add(kind, path)
    return with_log(await _design_summary(), log)


async def _write(command: str, path: str, overwrite: bool) -> dict[str, Any]:
    SESSION.require_design()
    target = tcl_quote(to_wsl_path(path))
    output = await SESSION.run(
        tcl(
            """
set __p [file normalize @PATH@]
if {!@OVERWRITE@ && [file exists $__p]} {
  error "File already exists: $__p (pass overwrite=true to replace it)"
}
if {![file isdirectory [file dirname $__p]]} {
  error "Directory does not exist: [file dirname $__p]"
}
@COMMAND@ $__p
puts "Wrote $__p"
""",
            PATH=target,
            OVERWRITE=int(overwrite),
            COMMAND=command,
        )
    )
    saved = list(SESSION.design.edits)
    SESSION.design.edits.clear()
    SESSION.design.add(command, path)
    return {"written": to_wsl_path(path), "edits_saved": saved, "log": output}


@mcp.tool(annotations=DESTRUCTIVE)
async def write_def(
    path: Annotated[str, Field(description="Output DEF path (Windows or WSL).")],
    overwrite: Annotated[bool, Field(description="Replace the file if it already exists.")] = False,
) -> dict[str, Any]:
    """Write the current design (including unsaved edits) to a DEF file."""
    return await _write("write_def", path, overwrite)


@mcp.tool(annotations=DESTRUCTIVE)
async def write_db(
    path: Annotated[str, Field(description="Output .odb path (Windows or WSL).")],
    overwrite: Annotated[bool, Field(description="Replace the file if it already exists.")] = False,
) -> dict[str, Any]:
    """Write the whole database (tech, libraries, design, edits) to an OpenROAD .odb file."""
    return await _write("write_db", path, overwrite)


@mcp.tool(annotations=READ_ONLY)
async def session_status(
    check_connection: Annotated[
        bool, Field(description="Start OpenROAD if needed and report its version.")
    ] = False,
) -> dict[str, Any]:
    """Report the OpenROAD session state, loaded files, unsaved edits and configuration."""
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
        "dbu_per_micron": design.dbu_per_micron,
        "files": design.files,
        "unsaved_edits": design.edits,
        "raw_tcl_enabled": CFG.allow_raw_tcl,
    }


@mcp.tool(annotations=DESTRUCTIVE)
async def reset_session() -> str:
    """Stop the OpenROAD process, forgetting the loaded design and any unsaved edits."""
    lost = len(SESSION.design.edits)
    await SESSION.close()
    return f"OpenROAD session stopped ({lost} unsaved edits discarded). Call load_design to start again."


# ------------------------------- technology --------------------------------


@mcp.tool(annotations=READ_ONLY)
async def tech_info() -> dict[str, Any]:
    """Technology and library summary: units, LEF version, layer/via counts, libraries and sites."""
    records, log = await query(
        """
set __tech [ord::get_db_tech]
__mcp_rec tech [$__tech getName] [$__tech getDbUnitsPerMicron] [__odb_try {$__tech getLefVersionStr}] \
  [__odb_try {$__tech getManufacturingGrid}] [$__tech getLayerCount] [$__tech getRoutingLayerCount] \
  [__odb_try {$__tech getViaCount}]
foreach __lib [[ord::get_db] getLibs] {
  __mcp_rec lib [$__lib getName] [llength [$__lib getMasters]]
  foreach __site [$__lib getSites] {
    __mcp_rec site [$__lib getName] [$__site getName] [$__site getWidth] [$__site getHeight] \
      [__odb_try {$__site getClass}]
  }
}
"""
    )
    t = first(records, "tech")
    return with_log(
        {
            "tech": t[1],
            "dbu_per_micron": int(t[2]),
            "lef_version": t[3],
            "manufacturing_grid_um": um(t[4]) if t[4] else None,
            "layers": int(t[5]),
            "routing_layers": int(t[6]),
            "vias": number(t[7]) if t[7] else None,
            "libraries": [{"name": r[1], "masters": int(r[2])} for r in by_kind(records, "lib")],
            "sites": [
                {"library": r[1], "name": r[2], "width_um": um(r[3]), "height_um": um(r[4]), "class": r[5]}
                for r in by_kind(records, "site")
            ],
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_layers(
    kind: Annotated[Literal["routing", "all"], Field(description="Only routing layers, or all layers.")] = "routing",
) -> dict[str, Any]:
    """Technology layers: type, direction, routing level, pitch, width, spacing, R and C per unit."""
    records, log = await query(
        tcl(
            """
foreach __l [[ord::get_db_tech] getLayers] {
  set __type [$__l getType]
  if {@ROUTING_ONLY@ && $__type ne "ROUTING"} { continue }
  __mcp_rec layer [$__l getName] $__type [$__l getDirection] [__odb_try {$__l getRoutingLevel}] \
    [__odb_try {$__l getPitch}] [__odb_try {$__l getWidth}] [__odb_try {$__l getSpacing}] \
    [__odb_try {$__l getResistance}] [__odb_try {$__l getCapacitance}]
}
""",
            ROUTING_ONLY=int(kind == "routing"),
        )
    )
    layers = [
        {
            "name": r[1],
            "type": r[2],
            "direction": r[3],
            "routing_level": number(r[4]) if r[4] else None,
            "pitch_um": um(r[5]) if r[5] else None,
            "width_um": um(r[6]) if r[6] else None,
            "spacing_um": um(r[7]) if r[7] else None,
            "resistance": number(r[8]) if r[8] else None,
            "capacitance": number(r[9]) if r[9] else None,
        }
        for r in by_kind(records, "layer")
    ]
    return with_log({"count": len(layers), "layers": layers}, log)


@mcp.tool(annotations=READ_ONLY)
async def list_masters(
    pattern: Pattern = "*",
    master_type: Annotated[
        str | None, Field(description="Filter by master type, e.g. CORE, BLOCK, PAD, CORE_SPACER, ENDCAP.")
    ] = None,
    limit: Limit = 100,
) -> dict[str, Any]:
    """Library cells (masters): type, size, area and pin count."""
    records, log = await query(
        tcl(
            """
set __n 0
foreach __lib [[ord::get_db] getLibs] {
  foreach __m [$__lib getMasters] {
    if {![string match @PATTERN@ [$__m getName]]} { continue }
    if {@TYPE@ ne "" && [$__m getType] ne @TYPE@} { continue }
    incr __n
    if {$__n <= @LIMIT@} {
      __mcp_rec master [$__m getName] [$__m getType] [$__m getWidth] [$__m getHeight] \
        [llength [$__m getMTerms]] [$__lib getName]
    }
  }
}
__mcp_rec total $__n
""",
            PATTERN=tcl_quote(pattern),
            TYPE=tcl_quote((master_type or "").upper()),
            LIMIT=limit,
        )
    )
    masters = [
        {
            "name": r[1],
            "type": r[2],
            "width_um": um(r[3]),
            "height_um": um(r[4]),
            "area_um2": round(um(r[3]) * um(r[4]), 4),
            "pins": int(r[5]),
            "library": r[6],
        }
        for r in by_kind(records, "master")
    ]
    return with_log({"total_matches": total(records), "returned": len(masters), "masters": masters}, log)


@mcp.tool(annotations=READ_ONLY)
async def master_info(name: Annotated[str, Field(description="Master (library cell) name.")]) -> dict[str, Any]:
    """One library cell: size, type, site, symmetry and its pins."""
    records, log = await query(
        tcl(
            """
set __m [__odb_master @NAME@]
set __site [__odb_try {$__m getSite}]
__mcp_rec master [$__m getName] [$__m getType] [$__m getWidth] [$__m getHeight] \
  [expr {[__odb_null $__site] ? "" : [$__site getName]}] [__odb_try {$__m getSymmetryX}] \
  [__odb_try {$__m getSymmetryY}] [__odb_try {$__m getSymmetryR90}] [[$__m getLib] getName]
foreach __mt [$__m getMTerms] {
  __mcp_rec pin [$__mt getName] [$__mt getIoType] [$__mt getSigType]
}
""",
            NAME=tcl_quote(name),
        )
    )
    m = first(records, "master")
    return with_log(
        {
            "name": m[1],
            "type": m[2],
            "width_um": um(m[3]),
            "height_um": um(m[4]),
            "site": m[5] or None,
            "symmetry": {"x": flag(m[6]), "y": flag(m[7]), "r90": flag(m[8])},
            "library": m[9],
            "pins": [{"name": r[1], "io_type": r[2], "sig_type": r[3]} for r in by_kind(records, "pin")],
        },
        log,
    )


# --------------------------------- design ----------------------------------


DESIGN_SUMMARY_TCL = """
set __block [__odb_block]
__mcp_rec block [$__block getName] {*}[__odb_bbox [$__block getDieArea]] {*}[__odb_bbox [$__block getCoreArea]]
set __insts [$__block getInsts]
set __nets [$__block getNets]
foreach {__k __v} [list instances [llength $__insts] nets [llength $__nets] ports [llength [$__block getBTerms]] \
    rows [llength [$__block getRows]] blockages [llength [$__block getBlockages]] \
    obstructions [llength [$__block getObstructions]]] {
  __mcp_rec count $__k $__v
}
array unset __status
array unset __mtype
set __std 0
set __macro 0
foreach __inst $__insts {
  set __m [$__inst getMaster]
  incr __status([$__inst getPlacementStatus])
  incr __mtype([$__m getType])
  set __a [expr {wide([$__m getWidth]) * [$__m getHeight]}]
  if {[$__m isBlock]} { incr __macro $__a } else { incr __std $__a }
}
foreach __k [array names __status] { __mcp_rec status $__k $__status($__k) }
foreach __k [array names __mtype] { __mcp_rec mtype $__k $__mtype($__k) }
__mcp_rec area $__std $__macro
set __wl 0.0
dict for {__k __v} [__odb_wirelengths] { set __wl [expr {$__wl + $__v}] }
__mcp_rec wirelength $__wl
__mcp_rec routed [__odb_try {$__block designIsRouted 0}]
"""


async def _design_summary() -> dict[str, Any]:
    records, log = await query(DESIGN_SUMMARY_TCL)
    b = first(records, "block")
    die, core = bbox(b[2:6]), bbox(b[6:10])
    core_area = round((core[2] - core[0]) * (core[3] - core[1]), 4)
    area = first(records, "area")
    std_area, macro_area = um2(area[1]), um2(area[2])
    routed = first(records, "routed")[1]
    return with_log(
        {
            "block": b[1],
            "dbu_per_micron": dbu_per_micron(),
            "die_area_um": die,
            "core_area_um": core,
            "counts": {r[1]: int(r[2]) for r in by_kind(records, "count")},
            "placement_status": {r[1]: int(r[2]) for r in by_kind(records, "status")},
            "master_types": {r[1]: int(r[2]) for r in by_kind(records, "mtype")},
            "area_um2": {
                "core": core_area,
                "std_cells": std_area,
                "macros": macro_area,
                "utilization": round((std_area + macro_area) / core_area, 4) if core_area else None,
            },
            "total_routed_wirelength_um": round(float(first(records, "wirelength")[1]), 4),
            "routed": flag(routed) if routed else None,
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def design_summary() -> dict[str, Any]:
    """Block overview: die/core area, object counts, placement status and cell type histograms,
    utilization, total routed wirelength and whether the design is routed."""
    return await _design_summary()


@mcp.tool(annotations=READ_ONLY)
async def find_instances(
    pattern: Pattern = "*",
    master_pattern: Annotated[str | None, Field(description="Only instances of masters matching this glob.")] = None,
    status: Annotated[PlacementStatus | None, Field(description="Only instances with this placement status.")] = None,
    limit: Limit = 100,
) -> dict[str, Any]:
    """Find instances by name; returns master, location, orientation, status and bounding box."""
    records, log = await query(
        tcl(
            """
set __n 0
foreach __inst [__odb_insts @PATTERN@] {
  if {@MPAT@ ne "" && ![string match @MPAT@ [[$__inst getMaster] getName]]} { continue }
  if {@STATUS@ ne "" && [$__inst getPlacementStatus] ne @STATUS@} { continue }
  incr __n
  if {$__n <= @LIMIT@} { __odb_inst_rec $__inst }
}
__mcp_rec total $__n
""",
            PATTERN=tcl_quote(pattern),
            MPAT=tcl_quote(master_pattern or ""),
            STATUS=tcl_quote(db_status(status) if status else ""),
            LIMIT=limit,
        )
    )
    insts = [inst_dict(r) for r in by_kind(records, "inst")]
    return with_log({"total_matches": total(records), "returned": len(insts), "instances": insts}, log)


@mcp.tool(annotations=READ_ONLY)
async def instance_info(name: Annotated[str, Field(description="Exact instance name.")]) -> dict[str, Any]:
    """One instance: master, placement, bounding box, and each pin with its net."""
    records, log = await query(
        tcl(
            """
set __inst [__odb_inst @NAME@]
__odb_inst_rec $__inst
__mcp_rec flags [__odb_try {$__inst isDoNotTouch}] [__odb_try {[$__inst getMaster] isBlock}]
foreach __it [$__inst getITerms] {
  set __net [$__it getNet]
  __mcp_rec pin [[$__it getMTerm] getName] [expr {[__odb_null $__net] ? "" : [$__net getName]}] \
    [$__it getIoType] [$__it getSigType]
}
""",
            NAME=tcl_quote(name),
        )
    )
    flags = first(records, "flags")
    return with_log(
        {
            **inst_dict(first(records, "inst")),
            "dont_touch": flag(flags[1]),
            "is_macro": flag(flags[2]),
            "pins": [
                {"pin": r[1], "net": r[2] or None, "io_type": r[3], "sig_type": r[4]}
                for r in by_kind(records, "pin")
            ],
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def find_nets(
    pattern: Pattern = "*",
    sig_type: Annotated[SigType | None, Field(description="Only nets with this signal type.")] = None,
    limit: Limit = 100,
) -> dict[str, Any]:
    """Find nets by name; returns signal type, terminal counts and routed length."""
    records, log = await query(
        tcl(
            """
set __n 0
set __lengths [__odb_wirelengths @PATTERN@]
foreach __net [__odb_nets @PATTERN@] {
  if {@SIG@ ne "" && [$__net getSigType] ne @SIG@} { continue }
  incr __n
  if {$__n <= @LIMIT@} { __odb_net_rec $__net $__lengths }
}
__mcp_rec total $__n
""",
            PATTERN=tcl_quote(pattern),
            SIG=tcl_quote(sig_type or ""),
            LIMIT=limit,
        )
    )
    nets = [net_dict(r) for r in by_kind(records, "net")]
    return with_log({"total_matches": total(records), "returned": len(nets), "nets": nets}, log)


@mcp.tool(annotations=READ_ONLY)
async def net_info(name: Annotated[str, Field(description="Exact net name.")]) -> dict[str, Any]:
    """One net: driver, every connected instance pin and port with location, routed length and HPWL."""
    records, log = await query(
        tcl(
            """
set __net [__odb_net @NAME@]
__odb_net_rec $__net [__odb_wirelengths [$__net getName]]
__mcp_rec wiretype [__odb_try {$__net getWireType}]
foreach __it [$__net getITerms] {
  __mcp_rec term [[$__it getInst] getName] [[$__it getMTerm] getName] [$__it getIoType] \
    {*}[__odb_bbox [$__it getBBox]]
}
foreach __bt [$__net getBTerms] {
  __mcp_rec port [$__bt getName] [$__bt getIoType] {*}[__odb_bbox [$__bt getBBox]]
}
""",
            NAME=tcl_quote(name),
        )
    )
    terms = [
        {"instance": r[1], "pin": r[2], "io_type": r[3], "bbox": bbox(r[4:8])} for r in by_kind(records, "term")
    ]
    ports = [{"port": r[1], "io_type": r[2], "bbox": bbox(r[3:7])} for r in by_kind(records, "port")]
    drivers = [f"{t['instance']}/{t['pin']}" for t in terms if t["io_type"] == "OUTPUT"]
    drivers += [p["port"] for p in ports if p["io_type"] == "INPUT"]
    centers = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in [t["bbox"] for t in terms + ports]]
    hpwl = None
    if len(centers) > 1:
        xs, ys = [c[0] for c in centers], [c[1] for c in centers]
        hpwl = round(max(xs) - min(xs) + max(ys) - min(ys), 4)
    wiretype = first(records, "wiretype")
    return with_log(
        {
            **net_dict(first(records, "net")),
            "wire_type": wiretype[1] or None,
            "drivers": drivers,
            "hpwl_um": hpwl,
            "terminals": terms,
            "ports": ports,
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_ports(pattern: Pattern = "*", limit: Limit = 200) -> dict[str, Any]:
    """Top-level ports (block terminals): direction, signal type, net, pin bounding box and status."""
    records, log = await query(
        tcl(
            """
set __n 0
foreach __bt [__odb_bterms @PATTERN@] {
  incr __n
  if {$__n > @LIMIT@} { continue }
  set __net [$__bt getNet]
  __mcp_rec port [$__bt getName] [$__bt getIoType] [$__bt getSigType] \
    [expr {[__odb_null $__net] ? "" : [$__net getName]}] \
    [__odb_try {$__bt getFirstPinPlacementStatus}] {*}[__odb_bbox [$__bt getBBox]]
}
__mcp_rec total $__n
""",
            PATTERN=tcl_quote(pattern),
            LIMIT=limit,
        )
    )
    ports = [
        {"name": r[1], "io_type": r[2], "sig_type": r[3], "net": r[4] or None, "status": r[5] or None,
         "bbox": bbox(r[6:10])}
        for r in by_kind(records, "port")
    ]
    return with_log({"total_matches": total(records), "returned": len(ports), "ports": ports}, log)


@mcp.tool(annotations=READ_ONLY)
async def list_rows(limit: Limit = 100) -> dict[str, Any]:
    """Placement rows: site, origin, orientation, direction and site count."""
    records, log = await query(
        tcl(
            """
set __rows [[__odb_block] getRows]
foreach __row [lrange $__rows 0 [expr {@LIMIT@ - 1}]] {
  set __o [$__row getOrigin]
  __mcp_rec row [$__row getName] [[$__row getSite] getName] [lindex $__o 0] [lindex $__o 1] \
    [$__row getOrient] [$__row getDirection] [$__row getSiteCount] [__odb_try {$__row getSpacing}]
}
__mcp_rec total [llength $__rows]
""",
            LIMIT=limit,
        )
    )
    rows = [
        {"name": r[1], "site": r[2], "origin": [um(r[3]), um(r[4])], "orient": r[5], "direction": r[6],
         "site_count": int(r[7]), "spacing_um": um(r[8]) if r[8] else None}
        for r in by_kind(records, "row")
    ]
    return with_log({"total_rows": total(records), "returned": len(rows), "rows": rows}, log)


@mcp.tool(annotations=READ_ONLY)
async def list_blockages() -> dict[str, Any]:
    """Placement blockages and routing obstructions."""
    records, log = await query(
        """
set __block [__odb_block]
foreach __b [$__block getBlockages] {
  set __inst [__odb_try {$__b getInstance} NULL]
  __mcp_rec blockage {*}[__odb_bbox [$__b getBBox]] [__odb_try {$__b isSoft}] \
    [__odb_try {$__b getMaxDensity}] [expr {[__odb_null $__inst] ? "" : [$__inst getName]}]
}
foreach __o [$__block getObstructions] {
  set __box [$__o getBBox]
  set __inst [__odb_try {$__o getInstance} NULL]
  __mcp_rec obstruction [[$__box getTechLayer] getName] {*}[__odb_bbox $__box] \
    [expr {[__odb_null $__inst] ? "" : [$__inst getName]}]
}
"""
    )
    blockages = [
        {"bbox": bbox(r[1:5]), "soft": flag(r[5]), "max_density": number(r[6]) if r[6] else None,
         "instance": r[7] or None}
        for r in by_kind(records, "blockage")
    ]
    obstructions = [
        {"layer": r[1], "bbox": bbox(r[2:6]), "instance": r[6] or None} for r in by_kind(records, "obstruction")
    ]
    return with_log({"blockages": blockages, "obstructions": obstructions}, log)


@mcp.tool(annotations=READ_ONLY)
async def placement_report(
    limit: Annotated[int, Field(ge=1, le=1000, description="Maximum instances listed per category.")] = 50,
) -> dict[str, Any]:
    """Placement health: status counts, unplaced instances, placed instances outside the core, and macros."""
    records, log = await query(
        tcl(
            """
set __block [__odb_block]
lassign [__odb_bbox [$__block getCoreArea]] __cx1 __cy1 __cx2 __cy2
array unset __status
set __unplaced 0
set __outside 0
set __macros 0
foreach __inst [$__block getInsts] {
  set __s [$__inst getPlacementStatus]
  incr __status($__s)
  if {$__s eq "NONE" || $__s eq "UNPLACED"} {
    incr __unplaced
    if {$__unplaced <= @LIMIT@} { __mcp_rec unplaced [$__inst getName] [[$__inst getMaster] getName] }
    continue
  }
  if {[[$__inst getMaster] isBlock]} {
    incr __macros
    if {$__macros <= @LIMIT@} { __odb_inst_rec $__inst }
  }
  lassign [__odb_bbox [$__inst getBBox]] __x1 __y1 __x2 __y2
  if {$__x1 < $__cx1 || $__y1 < $__cy1 || $__x2 > $__cx2 || $__y2 > $__cy2} {
    incr __outside
    if {$__outside <= @LIMIT@} { __mcp_rec outside [$__inst getName] $__x1 $__y1 $__x2 $__y2 }
  }
}
foreach __k [array names __status] { __mcp_rec status $__k $__status($__k) }
__mcp_rec totals $__unplaced $__outside $__macros
""",
            LIMIT=limit,
        )
    )
    t = first(records, "totals")
    return with_log(
        {
            "placement_status": {r[1]: int(r[2]) for r in by_kind(records, "status")},
            "unplaced": {"count": int(t[1]), "instances": [{"name": r[1], "master": r[2]}
                                                            for r in by_kind(records, "unplaced")]},
            "outside_core": {"count": int(t[2]), "instances": [{"name": r[1], "bbox": bbox(r[2:6])}
                                                                for r in by_kind(records, "outside")]},
            "macros": {"count": int(t[3]), "instances": [inst_dict(r) for r in by_kind(records, "inst")]},
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def wirelength_report(
    top_n: Annotated[int, Field(ge=1, le=1000, description="Number of longest nets to list.")] = 20,
) -> dict[str, Any]:
    """Total routed wirelength, the longest routed nets, and OpenROAD's total HPWL report."""
    records, log = await query(
        tcl(
            """
set __lens {}
set __total 0.0
set __routed 0
dict for {__name __len} [__odb_wirelengths] {
  if {$__len > 0} {
    incr __routed
    set __total [expr {$__total + $__len}]
    lappend __lens [list $__len $__name]
  }
}
foreach __item [lrange [lsort -real -decreasing -index 0 $__lens] 0 [expr {@TOP@ - 1}]] {
  __mcp_rec longest [lindex $__item 1] [lindex $__item 0]
}
__mcp_rec totals $__total $__routed
catch {ord::report_hpwl}
""",
            TOP=top_n,
        )
    )
    t = first(records, "totals")
    # ord::report_hpwl prints the total half-perimeter wirelength (microns) as a bare number.
    hpwl = number(log.splitlines()[-1].strip()) if log else None
    if isinstance(hpwl, (int, float)):
        log = "\n".join(log.splitlines()[:-1]).strip()
    else:
        hpwl = None
    return with_log(
        {
            "total_hpwl_um": hpwl,
            "total_routed_wirelength_um": round(float(t[1]), 4),
            "routed_nets": int(t[2]),
            "longest_nets": [
                {"net": r[1], "routed_length_um": round(float(r[2]), 4)} for r in by_kind(records, "longest")
            ],
        },
        log,
    )


@mcp.tool(annotations=READ_ONLY)
async def region_query(x1: Microns, y1: Microns, x2: Microns, y2: Microns, limit: Limit = 200) -> dict[str, Any]:
    """Instances whose bounding box intersects the rectangle (x1, y1)-(x2, y2), in microns."""
    lo_x, hi_x = sorted((to_dbu(x1), to_dbu(x2)))
    lo_y, hi_y = sorted((to_dbu(y1), to_dbu(y2)))
    records, log = await query(
        tcl(
            """
set __n 0
foreach __inst [[__odb_block] getInsts] {
  lassign [__odb_bbox [$__inst getBBox]] __x1 __y1 __x2 __y2
  if {$__x2 < @X1@ || $__x1 > @X2@ || $__y2 < @Y1@ || $__y1 > @Y2@} { continue }
  incr __n
  if {$__n <= @LIMIT@} { __odb_inst_rec $__inst }
}
__mcp_rec total $__n
""",
            X1=lo_x,
            X2=hi_x,
            Y1=lo_y,
            Y2=hi_y,
            LIMIT=limit,
        )
    )
    insts = [inst_dict(r) for r in by_kind(records, "inst")]
    return with_log({"total_matches": total(records), "returned": len(insts), "instances": insts}, log)


# --------------------------------- editing ---------------------------------


async def _edit(script: str, description: str) -> dict[str, Any]:
    records, log = await query(script)
    changed = total(records)
    record_edit(description)
    return with_log(
        {"done": description, "changed": changed, "unsaved_edits": len(SESSION.design.edits)}, log
    )


@mcp.tool(annotations=MUTATING)
async def place_instance(
    name: Annotated[str, Field(description="Instance name.")],
    x: Microns,
    y: Microns,
    orient: Annotated[Orient, Field(description="Orientation.")] = "R0",
    status: Annotated[PlacementStatus, Field(description="Placement status to set.")] = "PLACED",
    master: Annotated[
        str | None, Field(description="If given and the instance does not exist, create it with this master.")
    ] = None,
) -> dict[str, Any]:
    """Place (or create and place) an instance with its lower-left corner at (x, y) microns."""
    cell = f"-cell {tcl_quote(master)}" if master else ""
    script = tcl(
        """
if {@CELL@ eq "" && [__odb_null [[__odb_block] findInst @NAME@]]} {
  error "Instance not found: [set __name @NAME@] (pass master to create it)"
}
place_inst -name @NAME@ -location [list @X@ @Y@] -orientation @ORIENT@ -status @STATUS@ @CELLARG@
__odb_inst_rec [__odb_inst @NAME@]
__mcp_rec total 1
""",
        NAME=tcl_quote(name),
        X=x,
        Y=y,
        ORIENT=orient,
        STATUS=db_status(status),
        CELL=tcl_quote(cell),
        CELLARG=cell,
    )
    records, log = await query(script)
    record_edit(f"place_instance {name} at ({x}, {y}) {orient} {status}" + (f" as {master}" if master else ""))
    return with_log(
        {"instance": inst_dict(first(records, "inst")), "unsaved_edits": len(SESSION.design.edits)}, log
    )


@mcp.tool(annotations=MUTATING)
async def move_instances(
    pattern: Pattern,
    dx: Annotated[float, Field(description="Shift in x, microns.")],
    dy: Annotated[float, Field(description="Shift in y, microns.")],
    include_fixed: Annotated[bool, Field(description="Also move FIXED/FIRM/LOCKED/COVER instances.")] = False,
) -> dict[str, Any]:
    """Move matching instances by (dx, dy) microns. Fixed instances are skipped unless include_fixed."""
    return await _edit(
        tcl(
            """
set __insts [__odb_insts @PATTERN@]
if {[llength $__insts] == 0} { error "No instance matches [set __p @PATTERN@]" }
set __n 0
set __skipped 0
foreach __inst $__insts {
  if {!@FIXED@ && [$__inst getPlacementStatus] in {FIXED FIRM LOCKED COVER}} {
    incr __skipped
    continue
  }
  set __loc [$__inst getLocation]
  $__inst setLocation [expr {[lindex $__loc 0] + @DX@}] [expr {[lindex $__loc 1] + @DY@}]
  incr __n
}
puts "moved $__n instance(s), skipped $__skipped fixed"
__mcp_rec total $__n
""",
            PATTERN=tcl_quote(pattern),
            FIXED=int(include_fixed),
            DX=to_dbu(dx),
            DY=to_dbu(dy),
        ),
        f"move_instances {pattern} by ({dx}, {dy})",
    )


@mcp.tool(annotations=MUTATING)
async def set_placement_status(
    pattern: Pattern,
    status: Annotated[PlacementStatus, Field(description="New placement status.")],
) -> dict[str, Any]:
    """Set the placement status (e.g. FIXED to lock cells) of matching instances."""
    return await _edit(
        tcl(
            """
set __insts [__odb_insts @PATTERN@]
if {[llength $__insts] == 0} { error "No instance matches [set __p @PATTERN@]" }
foreach __inst $__insts { $__inst setPlacementStatus @STATUS@ }
__mcp_rec total [llength $__insts]
""",
            PATTERN=tcl_quote(pattern),
            STATUS=db_status(status),
        ),
        f"set_placement_status {pattern} {status}",
    )


@mcp.tool(annotations=MUTATING)
async def swap_master(
    instance: Annotated[str, Field(description="Instance name.")],
    new_master: Annotated[str, Field(description="Replacement master (must have compatible pins).")],
) -> dict[str, Any]:
    """Replace an instance's master cell (e.g. resize a gate), keeping its connections."""
    return await _edit(
        tcl(
            """
set __inst [__odb_inst @INST@]
set __old [[$__inst getMaster] getName]
if {![$__inst swapMaster [__odb_master @MASTER@]]} {
  error "swapMaster failed: [set __m @MASTER@] is not pin-compatible with $__old"
}
puts "swapped $__old -> [[$__inst getMaster] getName]"
__mcp_rec total 1
""",
            INST=tcl_quote(instance),
            MASTER=tcl_quote(new_master),
        ),
        f"swap_master {instance} -> {new_master}",
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_instance(name: Annotated[str, Field(description="Exact instance name.")]) -> dict[str, Any]:
    """Delete an instance (its pins are disconnected from their nets)."""
    return await _edit(
        tcl("odb::dbInst_destroy [__odb_inst @NAME@]\n__mcp_rec total 1", NAME=tcl_quote(name)),
        f"delete_instance {name}",
    )


@mcp.tool(annotations=MUTATING)
async def rename_object(
    kind: Annotated[Literal["instance", "net"], Field(description="Object type.")],
    old_name: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename an instance or a net."""
    finder = "__odb_inst" if kind == "instance" else "__odb_net"
    return await _edit(
        tcl(
            """
set __obj [@FIND@ @OLD@]
set __ok [$__obj rename @NEW@]
if {$__ok ne "" && !$__ok} { error "Rename failed (is the new name already used?)" }
__mcp_rec total 1
""",
            FIND=finder,
            OLD=tcl_quote(old_name),
            NEW=tcl_quote(new_name),
        ),
        f"rename {kind} {old_name} -> {new_name}",
    )


@mcp.tool(annotations=MUTATING)
async def create_net(
    name: Annotated[str, Field(description="New net name.")],
    sig_type: Annotated[SigType, Field(description="Signal type.")] = "SIGNAL",
) -> dict[str, Any]:
    """Create a new, unconnected net."""
    return await _edit(
        tcl(
            """
set __block [__odb_block]
if {![__odb_null [$__block findNet @NAME@]]} { error "Net already exists: [set __n @NAME@]" }
set __net [odb::dbNet_create $__block @NAME@]
$__net setSigType @SIG@
__mcp_rec total 1
""",
            NAME=tcl_quote(name),
            SIG=sig_type,
        ),
        f"create_net {name} ({sig_type})",
    )


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_net(name: Annotated[str, Field(description="Exact net name.")]) -> dict[str, Any]:
    """Delete a net (all its pins become unconnected)."""
    return await _edit(
        tcl("odb::dbNet_destroy [__odb_net @NAME@]\n__mcp_rec total 1", NAME=tcl_quote(name)),
        f"delete_net {name}",
    )


@mcp.tool(annotations=MUTATING)
async def connect_pin(
    instance: Annotated[str, Field(description="Instance name.")],
    pin: Annotated[str, Field(description="Pin (master terminal) name, e.g. A or ZN.")],
    net: Annotated[str, Field(description="Net to connect to.")],
) -> dict[str, Any]:
    """Connect an instance pin to a net (disconnecting it from any previous net)."""
    return await _edit(
        tcl(
            """
set __it [[__odb_inst @INST@] findITerm @PIN@]
if {[__odb_null $__it]} { error "Pin not found: [set __p @PIN@] on [set __i @INST@]" }
set __net [__odb_net @NET@]
if {![__odb_null [$__it getNet]]} { $__it disconnect }
$__it connect $__net
__mcp_rec total 1
""",
            INST=tcl_quote(instance),
            PIN=tcl_quote(pin),
            NET=tcl_quote(net),
        ),
        f"connect_pin {instance}/{pin} -> {net}",
    )


@mcp.tool(annotations=MUTATING)
async def disconnect_pin(
    instance: Annotated[str, Field(description="Instance name.")],
    pin: Annotated[str, Field(description="Pin (master terminal) name.")],
) -> dict[str, Any]:
    """Disconnect an instance pin from its net."""
    return await _edit(
        tcl(
            """
set __it [[__odb_inst @INST@] findITerm @PIN@]
if {[__odb_null $__it]} { error "Pin not found: [set __p @PIN@] on [set __i @INST@]" }
$__it disconnect
__mcp_rec total 1
""",
            INST=tcl_quote(instance),
            PIN=tcl_quote(pin),
        ),
        f"disconnect_pin {instance}/{pin}",
    )


@mcp.tool(annotations=MUTATING)
async def create_blockage(
    x1: Microns,
    y1: Microns,
    x2: Microns,
    y2: Microns,
    soft: Annotated[bool, Field(description="Soft blockage (only buffers may be placed).")] = False,
    max_density: Annotated[
        float | None, Field(ge=0, le=100, description="Partial blockage: maximum placement density %.")
    ] = None,
) -> dict[str, Any]:
    """Create a placement blockage over the rectangle (microns)."""
    options = (" -soft" if soft else "") + (f" -max_density {max_density}" if max_density is not None else "")
    return await _edit(
        f"create_blockage -region [list {x1} {y1} {x2} {y2}]{options}\n__mcp_rec total 1",
        f"create_blockage ({x1}, {y1})-({x2}, {y2}){options}",
    )


@mcp.tool(annotations=MUTATING)
async def create_obstruction(
    layer: Annotated[str, Field(description="Routing layer name, e.g. metal3.")],
    x1: Microns,
    y1: Microns,
    x2: Microns,
    y2: Microns,
) -> dict[str, Any]:
    """Create a routing obstruction on one layer over the rectangle (microns)."""
    return await _edit(
        f"create_obstruction -region [list {x1} {y1} {x2} {y2}] -layer {tcl_quote(layer)}\n__mcp_rec total 1",
        f"create_obstruction {layer} ({x1}, {y1})-({x2}, {y2})",
    )


if CFG.allow_raw_tcl:

    @mcp.tool(annotations=DESTRUCTIVE)
    async def run_tcl(
        script: Annotated[str, Field(description="Tcl script to evaluate in the OpenROAD session.")],
    ) -> str:
        """Run an arbitrary OpenROAD/OpenDB Tcl script in the live session (escape hatch).

        Do not call `exit`; it ends the session. Edits made here are not tracked as unsaved edits.
        """
        output = await SESSION.run(script)
        return output or "(no output)"


# --------------------------------- prompts ---------------------------------


@mcp.prompt()
def design_overview() -> str:
    """Describe the loaded physical design."""
    return """Give me an overview of the physical design loaded in the OpenROAD OpenDB session.

1. tech_info: technology, units, routing layers, sites.
2. design_summary: die/core size, instance/net/port counts, utilization, placement and routing state.
3. placement_report: unplaced instances, instances outside the core, macros.
4. wirelength_report: total wirelength and the longest nets.

Summarize in plain language: what stage of the flow the design is at (floorplanned, placed,
routed), how full it is, and anything unusual."""


@mcp.prompt()
def placement_health_check() -> str:
    """Check the placement for problems."""
    return """Check the placement of the design loaded in the OpenROAD OpenDB session.

1. design_summary: utilization and placement-status counts.
2. placement_report (limit=50): unplaced instances, instances outside the core area, macros.
3. list_blockages: placement blockages and obstructions.
4. For any macro, check its distance to the core boundary and to other macros.

Report a ranked issue list (for example unplaced cells, cells outside the core, utilization
above ~80%, macros too close to the edge) with the evidence and a suggested fix for each."""


@mcp.prompt()
def inspect_net(net: str) -> str:
    """Explain one net's connectivity and routing."""
    return f"""Explain net `{net}` in the design loaded in the OpenROAD OpenDB session.

1. net_info with name="{net}".
2. For the driver and the two farthest loads, call instance_info.

Explain what drives the net, what it feeds, how spread out the pins are (HPWL), whether it is
routed and how its routed length compares with its HPWL (a ratio well above ~1.5 suggests detours)."""


@mcp.prompt()
def macro_placement_review() -> str:
    """Review macro (block) placement."""
    return """Review the macro placement of the design loaded in the OpenROAD OpenDB session.

1. find_instances with status unset and check master types via list_masters master_type="BLOCK".
2. placement_report: the macros section lists every placed BLOCK instance with its position.
3. design_summary: core area.
4. list_blockages: halos or blockages around macros.

For each macro report its position, orientation and distance to the core edges. Flag overlapping
macros, macros that block routing channels, and macros that are not FIXED."""


@mcp.prompt()
def eco_move(instance: str, x: str, y: str) -> str:
    """Safely move one instance to a new location."""
    return f"""Move instance `{instance}` to ({x}, {y}) microns in the OpenROAD OpenDB session.

1. instance_info name="{instance}": note its current location, size and status.
2. region_query around the target (the target point +/- the instance width and height):
   is there room, or would it overlap other cells?
3. If the spot is free, place_instance name="{instance}" x={x} y={y} with its current orientation.
4. instance_info again to confirm.

Report the old and new location and any overlap risk. Remind me that the change is only in memory
until write_def or write_db is called."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
