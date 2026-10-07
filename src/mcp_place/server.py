"""MCP server for OpenROAD floorplanning and placement: floorplan (ifp), I/O pins (ppl),
macro placement (mpl), tap/endcap cells (tap), power grid (pdn), global placement (gpl)
and detailed placement (dpl).

One of four stage servers (place -> opt -> route -> signoff) that hand designs to each other
through checkpoints. Technology values (site, tracks, tapcell arguments, PDN script, pin
layers, densities, padding, filler cells, ...) come from the active platform (see
``openroad_common.platform``) and can be overridden per call.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from openroad_common.parsing import (
    compact,
    messages,
    parse_check_placement,
    parse_dpl,
    parse_filler,
    parse_gpl,
    parse_ifp,
    parse_improve_placement,
    parse_optimize_mirroring,
    parse_pdn,
    parse_ppl,
    parse_tapcell,
)
from openroad_common.platform import tcl_split
from openroad_common.records import by_kind, first, query
from openroad_common.session import Config, OpenRoadSession
from openroad_common.stage import (
    DESTRUCTIVE,
    MUTATING,
    READ_ONLY,
    STAGE_DRIVER_TCL,
    active_platform,
    design_status,
    record_step,
    register_stage_tools,
    routing_setup_tcl,
    run_step,
    tail,
)
from openroad_common.tcl import tcl_file, tcl_list, tcl_quote, to_wsl_path

PLACE_DRIVER_TCL = STAGE_DRIVER_TCL + r"""
proc __place_dbu {} { [__stage_block] getDbUnitsPerMicron }

# Floorplan facts the place tools check before acting.
proc __place_state {} {
  set block [__stage_block]
  set placed 0
  set total 0
  foreach bt [$block getBTerms] {
    incr total
    if {[$bt getFirstPinPlacementStatus] ni {NONE UNPLACED}} { incr placed }
  }
  set macros 0
  set taps 0
  set fillers 0
  foreach inst [$block getInsts] {
    set type [[$inst getMaster] getType]
    if {[[$inst getMaster] isBlock]} { incr macros }
    if {$type eq "CORE_WELLTAP" || [string match ENDCAP* $type]} { incr taps }
    if {$type eq "CORE_SPACER"} { incr fillers }
  }
  set pdn_wires 0
  foreach net [$block getNets] {
    if {[$net isSpecial]} { foreach sw [$net getSWires] { incr pdn_wires [llength [$sw getWires]] } }
  }
  __mcp_rec state [llength [$block getRows]] [llength [$block getTrackGrids]] $total $placed $macros $taps $fillers $pdn_wires
}

proc __place_macros {} {
  set block [__stage_block]
  foreach inst [$block getInsts] {
    set m [$inst getMaster]
    if {![$m isBlock]} { continue }
    set bb [$inst getBBox]
    __mcp_rec macro [$inst getName] [$m getName] [$inst getPlacementStatus] [$inst getOrient] {*}[__stage_bbox $bb]
  }
}

proc __place_snets {} {
  foreach net [[__stage_block] getNets] {
    if {![$net isSpecial]} { continue }
    set wires 0
    foreach sw [$net getSWires] { incr wires [llength [$sw getWires]] }
    __mcp_rec snet [$net getName] [$net getSigType] [llength [$net getSWires]] $wires
  }
}

proc __place_pins {limit} {
  set n 0
  foreach bt [[__stage_block] getBTerms] {
    if {$limit >= 0 && $n >= $limit} { break }
    incr n
    set layer ""
    set box {}
    foreach bp [$bt getBPins] {
      foreach b [$bp getBoxes] {
        set layer [[$b getTechLayer] getName]
        set box [__stage_bbox $b]
        break
      }
      if {$layer ne ""} { break }
    }
    __mcp_rec pin [$bt getName] [$bt getIoType] [$bt getSigType] [$bt getFirstPinPlacementStatus] $layer {*}$box
  }
}
"""

CFG = Config.from_env("PLACE", default_timeout=1800)
SESSION = OpenRoadSession(CFG, "place", PLACE_DRIVER_TCL)


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        await SESSION.close()


mcp = MCPServer(
    "openroad-place",
    title="OpenROAD Floorplan & Placement",
    description="Floorplan, I/O pins, macro placement, tap cells, power grid, global and detailed placement.",
    instructions=(
        "Floorplanning and placement on a persistent OpenROAD session. Start with load_design (Verilog + SDC; "
        "LEF/Liberty come from the platform). Typical order: initialize_floorplan -> macro_placement (if any "
        "macros) -> insert_tapcells -> generate_pdn -> global_placement (I/Os skipped while unplaced) -> "
        "place_io_pins -> global_placement -> detailed_placement, or place_design for all of it. Then "
        "save_checkpoint('placed') for the optimization server. Technology values default from the platform "
        "(list_platforms / platform_info). Distances are in microns."
    ),
    lifespan=lifespan,
)

register_stage_tools(mcp, SESSION, CFG, "place")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def place_state() -> dict[str, int]:
    SESSION.require_design()
    records, _ = await query(SESSION, "__place_state")
    keys = ("rows", "tracks", "pins", "pins_placed", "macros", "tap_cells", "fillers", "pdn_wires")
    return dict(zip(keys, (int(v) for v in first(records, "state")[1:])))


async def require_floorplan() -> dict[str, int]:
    state = await place_state()
    if not state["rows"]:
        raise ToolError("The design has no rows yet. Call initialize_floorplan first.")
    return state


async def list_macros() -> list[dict[str, Any]]:
    records, _ = await query(SESSION, "__place_macros\n__mcp_rec dbu [__place_dbu]")
    dbu = int(first(records, "dbu")[1])
    return [
        {"name": r[1], "master": r[2], "status": r[3], "orient": r[4],
         "bbox_um": [round(int(v) / dbu, 4) for v in r[5:9]]}
        for r in by_kind(records, "macro")
    ]


def estimate_tcl() -> str:
    """Re-estimate placement parasitics when the platform defines wire RC, so timing follows placement."""
    return "estimate_parasitics -placement" if active_platform(SESSION).get("wire_rc_layer") else ""


def after_placement() -> None:
    if active_platform(SESSION).get("wire_rc_layer"):
        SESSION.design.parasitics = "placement estimate"


def pad_sites(value: float | None) -> int:
    return int(round(value or 0))


def check_tcl() -> str:
    return "if {[catch {check_placement -verbose} __err]} { puts \"CHECK_PLACEMENT_FAILED: $__err\" }"


def um_list(values: list[float] | None, n: int, name: str) -> str | None:
    if values is None:
        return None
    if len(values) != n:
        raise ToolError(f"{name} must have {n} values in microns.")
    return tcl_list(values)


# ---------------------------------------------------------------------------
# Floorplan
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def initialize_floorplan(
    utilization: Annotated[float | None, Field(gt=0, lt=100, description="Core utilization % (sizes the die from the cell area).")] = None,
    aspect_ratio: Annotated[float, Field(gt=0, description="Core height / width (with utilization).")] = 1.0,
    core_space: Annotated[list[float] | None, Field(description="Die-to-core margin in microns: [all] or [bottom, top, left, right] (with utilization).")] = None,
    die_area: Annotated[list[float] | None, Field(description="Die [x1, y1, x2, y2] in microns (instead of utilization).")] = None,
    core_area: Annotated[list[float] | None, Field(description="Core [x1, y1, x2, y2] in microns (with die_area).")] = None,
    site: Annotated[str | None, Field(description="Row site (default: platform site).")] = None,
    additional_sites: Annotated[list[str] | None, Field(description="Extra sites to make rows for (e.g. double-height cells).")] = None,
    remove_synthesis_buffers: Annotated[bool, Field(description="Remove buffers inserted by synthesis (flow default).")] = True,
) -> dict[str, Any]:
    """Create the die, core rows and routing tracks (platform tracks file, else make_tracks), then apply the
    platform's routing-layer settings. Size the die by utilization or give die_area + core_area."""
    plat = active_platform(SESSION)
    state = await place_state()
    if state["rows"]:
        raise ToolError("The design is already floorplanned (it has rows). Load the netlist again to start over.")
    if (utilization is None) == (die_area is None):
        raise ToolError("Give either utilization (+ aspect_ratio, core_space) or die_area + core_area.")
    if die_area is not None and core_area is None:
        raise ToolError("core_area is required with die_area.")
    args = ["initialize_floorplan", f"-site {tcl_quote(site or plat.require('site'))}"]
    if utilization is not None:
        args += [f"-utilization {utilization}", f"-aspect_ratio {aspect_ratio}"]
        if core_space is not None:
            if len(core_space) not in (1, 4):
                raise ToolError("core_space must be [all] or [bottom, top, left, right] in microns.")
            args.append(f"-core_space {tcl_list(core_space) if len(core_space) == 4 else core_space[0]}")
    else:
        args += [f"-die_area {um_list(die_area, 4, 'die_area')}", f"-core_area {um_list(core_area, 4, 'core_area')}"]
    if additional_sites:
        args.append(f"-additional_sites {tcl_list(additional_sites)}")
    lines = [" ".join(args)]
    lines.append(f"source {tcl_file(plat.get('tracks_file'))}" if plat.get("tracks_file") else "make_tracks")
    if remove_synthesis_buffers:
        lines.append("remove_buffers")
    lines.append(routing_setup_tcl(plat))

    def parse(log: str) -> dict[str, Any]:
        return {**parse_ifp(log), "tracks": "platform tracks_file" if plat.get("tracks_file") else "make_tracks"}

    result = await run_step(SESSION, "\n".join(lines), parse, "initialize_floorplan")
    result["summary"]["track_grids"] = (await place_state())["tracks"]
    return result


# ---------------------------------------------------------------------------
# I/O pins
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def place_io_pins(
    hor_layers: Annotated[list[str] | None, Field(description="Layers for pins on the left/right edges (default: platform io_placer_hor_layer).")] = None,
    ver_layers: Annotated[list[str] | None, Field(description="Layers for pins on the top/bottom edges (default: platform io_placer_ver_layer).")] = None,
    min_distance: Annotated[float | None, Field(gt=0, description="Minimum pin spacing (microns, or tracks with min_distance_in_tracks).")] = None,
    min_distance_in_tracks: Annotated[bool, Field(description="min_distance is in routing tracks.")] = False,
    corner_avoidance: Annotated[float | None, Field(ge=0, description="Keep pins this far from die corners (microns).")] = None,
    exclude: Annotated[list[str] | None, Field(description="Excluded edge intervals, e.g. ['top:*', 'left:0-20'].")] = None,
    group_pins: Annotated[list[list[str]] | None, Field(description="Pin groups kept together, e.g. [['a[0]', 'a[1]']].")] = None,
    annealing: Annotated[bool, Field(description="Use simulated annealing (better HPWL for constrained pins).")] = False,
    random_seed: Annotated[int | None, Field(description="Seed for annealing.")] = None,
    write_pin_placement: Annotated[str | None, Field(description="Also write the placement as place_pin commands to this file.")] = None,
) -> dict[str, Any]:
    """Place the top-level I/O pins on the die boundary, minimizing I/O net wirelength (uses cell positions
    when the design is already placed)."""
    plat = active_platform(SESSION)
    state = await require_floorplan()
    if not state["tracks"]:
        raise ToolError("The design has no routing tracks. Call initialize_floorplan (it creates tracks) first.")
    hor = hor_layers or [plat.require("io_placer_hor_layer")]
    ver = ver_layers or [plat.require("io_placer_ver_layer")]
    args = ["place_pins", f"-hor_layers {tcl_list(hor)}", f"-ver_layers {tcl_list(ver)}"]
    if min_distance is not None:
        if min_distance_in_tracks and min_distance != int(min_distance):
            raise ToolError("With min_distance_in_tracks, min_distance must be a whole number of tracks.")
        args.append(f"-min_distance {int(min_distance) if min_distance_in_tracks else min_distance}")
    if min_distance_in_tracks:
        args.append("-min_distance_in_tracks")
    if corner_avoidance is not None:
        args.append(f"-corner_avoidance {corner_avoidance}")
    for region in exclude or []:
        args.append(f"-exclude {tcl_quote(region)}")
    for group in group_pins or []:
        args.append(f"-group_pins {tcl_list(group)}")
    if annealing:
        args.append("-annealing")
    if random_seed is not None:
        args.append(f"-random_seed {random_seed}")
    if write_pin_placement:
        args.append(f"-write_pin_placement {tcl_quote(to_wsl_path(write_pin_placement))}")
    result = await run_step(SESSION, " ".join(args), parse_ppl, "place_io_pins")
    after = await place_state()
    result["summary"]["pins_placed"] = f"{after['pins_placed']}/{after['pins']}"
    return result


@mcp.tool(annotations=MUTATING)
async def place_pin(
    pin_name: Annotated[str, Field(description="Top-level port name.")],
    layer: Annotated[str, Field(description="Routing layer of the pin shape.")],
    location: Annotated[list[float], Field(description="Pin center [x, y] in microns.")],
    pin_size: Annotated[list[float], Field(description="Pin shape [width, height] in microns (at least the layer's minimum width).")],
    force_to_die_boundary: Annotated[bool, Field(description="Snap the pin to the nearest die edge.")] = False,
    fixed: Annotated[bool, Field(description="Mark the pin FIXED so place_io_pins keeps it (else PLACED).")] = True,
) -> dict[str, Any]:
    """Place one I/O pin at an exact location (place_io_pins keeps fixed pins)."""
    await require_floorplan()
    args = [
        "place_pin",
        f"-pin_name {tcl_quote(pin_name)}",
        f"-layer {tcl_quote(layer)}",
        f"-location {um_list(location, 2, 'location')}",
        f"-pin_size {um_list(pin_size, 2, 'pin_size')}",
    ]
    if force_to_die_boundary:
        args.append("-force_to_die_boundary")
    if not fixed:
        args.append("-placed_status")
    output = await SESSION.run(" ".join(args))
    record_step(SESSION, f"place_pin {pin_name}")
    pins = await report_io_pins(name_filter=pin_name)
    return {"pin": next((p for p in pins["pins"] if p["name"] == pin_name), None), "warnings": messages(output)}


@mcp.tool(annotations=MUTATING)
async def set_io_pin_constraint(
    region: Annotated[str | None, Field(description="Edge interval 'top|bottom|left|right:start-end' (microns) or ':*' for the whole edge, e.g. 'left:10-50', 'top:*'.")] = None,
    pin_names: Annotated[list[str] | None, Field(description="Ports the constraint applies to.")] = None,
    direction: Annotated[Literal["input", "output", "inout", "feedthru"] | None, Field(description="Constrain all ports of this direction instead of naming them.")] = None,
    group: Annotated[bool, Field(description="Keep the pins together as a group.")] = False,
    order: Annotated[bool, Field(description="Keep the group in the given order (with group).")] = False,
    mirrored_pins: Annotated[list[str] | None, Field(description="Pairs of ports placed mirrored: [a, b, c, d] mirrors a/b and c/d.")] = None,
    clear: Annotated[bool, Field(description="Remove all I/O pin constraints instead.")] = False,
) -> dict[str, Any]:
    """Restrict where place_io_pins may put pins (call before place_io_pins)."""
    await require_floorplan()
    if clear:
        await SESSION.run("clear_io_pin_constraints")
        record_step(SESSION, "clear_io_pin_constraints")
        return {"done": "clear_io_pin_constraints"}
    if mirrored_pins:
        if len(mirrored_pins) % 2:
            raise ToolError("mirrored_pins needs pairs of ports.")
        script = f"set_io_pin_constraint -mirrored_pins {tcl_list(mirrored_pins)}"
    else:
        if not region or not (pin_names or direction):
            raise ToolError("Give region and pin_names or direction (or mirrored_pins, or clear=true).")
        args = ["set_io_pin_constraint", f"-region {tcl_quote(region)}"]
        if pin_names:
            args.append(f"-pin_names {tcl_list(pin_names)}")
        if direction:
            args.append(f"-direction {direction}")
        if group:
            args.append("-group")
        if order:
            args.append("-order")
        script = " ".join(args)
    output = await SESSION.run(script)
    record_step(SESSION, script)
    return {"done": script, "warnings": messages(output)}


@mcp.tool(annotations=READ_ONLY)
async def report_io_pins(
    limit: Annotated[int, Field(ge=1, le=100000, description="Maximum pins listed.")] = 200,
    name_filter: Annotated[str | None, Field(description="Only pins whose name contains this text.")] = None,
) -> dict[str, Any]:
    """Top-level pins: direction, signal type, placement status, layer and location (microns)."""
    SESSION.require_design()
    records, _ = await query(SESSION, "__place_pins -1\n__mcp_rec dbu [__place_dbu]")
    dbu = int(first(records, "dbu")[1])
    pins = []
    for r in by_kind(records, "pin"):
        if name_filter and name_filter not in r[1]:
            continue
        pin = {"name": r[1], "direction": r[2], "signal": r[3], "status": r[4]}
        if r[5]:
            box = [int(v) / dbu for v in r[6:10]]
            pin.update(layer=r[5], center_um=[round((box[0] + box[2]) / 2, 4), round((box[1] + box[3]) / 2, 4)])
        pins.append(pin)
    placed = sum(1 for p in pins if p["status"] not in ("NONE", "UNPLACED"))
    return {"pins_total": len(pins), "pins_placed": placed, "pins": pins[:limit], "truncated": len(pins) > limit}


# ---------------------------------------------------------------------------
# Macros, taps, power grid
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def macro_placement(
    halo: Annotated[list[float] | None, Field(description="Macro halo [x, y] in microns (default: platform macro_place_halo).")] = None,
    target_util: Annotated[float | None, Field(gt=0, le=1, description="Target utilization of the std-cell clusters (0-1).")] = None,
    fence: Annotated[list[float] | None, Field(description="Keep macros inside [x1, y1, x2, y2] microns.")] = None,
    boundary_weight: Annotated[float | None, Field(ge=0, description="Weight pushing macros to the core boundary.")] = None,
    max_num_level: Annotated[int | None, Field(ge=1, description="Clustering hierarchy levels.")] = None,
    report_directory: Annotated[str | None, Field(description="Directory for the placer's reports (WSL path).")] = None,
) -> dict[str, Any]:
    """Place hard macros with the hierarchical RTL macro placer (does nothing if the design has no macros)."""
    plat = active_platform(SESSION)
    state = await require_floorplan()
    if not state["macros"]:
        return {"summary": {"macros": 0, "message": "The design has no macros (BLOCK masters); nothing to place."}}
    if halo is not None and len(halo) != 2:
        raise ToolError("halo must be [x, y] in microns.")
    hx, hy = halo or ([float(v) for v in plat.get("macro_place_halo", [])] + [0.0, 0.0])[:2]
    args = ["rtl_macro_placer", f"-halo_width {hx}", f"-halo_height {hy}",
            f"-report_directory {tcl_quote(to_wsl_path(report_directory) if report_directory else '/tmp/mcp_place_rtlmp')}"]
    if target_util is not None:
        args.append(f"-target_util {target_util}")
    if fence is not None:
        if len(fence) != 4:
            raise ToolError("fence must be [x1, y1, x2, y2] in microns.")
        args += [f"-fence_{k} {v}" for k, v in zip(("lx", "ly", "ux", "uy"), fence)]
    if boundary_weight is not None:
        args.append(f"-boundary_weight {boundary_weight}")
    if max_num_level is not None:
        args.append(f"-max_num_level {max_num_level}")
    result = await run_step(SESSION, " ".join(args), lambda log: {"warnings": messages(log)}, "macro_placement")
    macros = await list_macros()
    result["summary"].update(macros=len(macros), placed=sum(m["status"] not in ("NONE", "UNPLACED") for m in macros),
                             macro_list=macros[:100])
    return result


@mcp.tool(annotations=MUTATING)
async def place_macro(
    name: Annotated[str, Field(description="Macro instance name.")],
    location: Annotated[list[float], Field(description="Lower-left [x, y] in microns.")],
    orientation: Annotated[Literal["R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"], Field(description="Orientation.")] = "R0",
    exact: Annotated[bool, Field(description="Use the location exactly (no snapping to the placement grid).")] = False,
    allow_overlap: Annotated[bool, Field(description="Allow overlapping other macros.")] = False,
) -> dict[str, Any]:
    """Place one macro by hand (e.g. before macro_placement places the rest, or to move one it placed;
    a LOCKED/FIRM macro is unlocked for the move)."""
    await require_floorplan()
    before = next((m for m in await list_macros() if m["name"] == name), None)
    if before is None:
        raise ToolError(f"No macro instance named '{name}'.")
    args = ["place_macro", f"-macro_name {tcl_quote(name)}", f"-location {um_list(location, 2, 'location')}",
            f"-orientation {orientation}"]
    if exact:
        args.append("-exact")
    if allow_overlap:
        args.append("-allow_overlap")
    unlock = ""
    if before["status"] in ("LOCKED", "FIRM", "COVER"):
        unlock = f"[[__stage_block] findInst {tcl_quote(name)}] setPlacementStatus PLACED\n"
    output = await SESSION.run(unlock + " ".join(args))
    record_step(SESSION, f"place_macro {name}")
    return {"macro": next((m for m in await list_macros() if m["name"] == name), None),
            "previous": before, "warnings": messages(output)}


@mcp.tool(annotations=MUTATING)
async def insert_tapcells(
    distance: Annotated[float | None, Field(gt=0, description="Tap cell spacing in microns (default: from platform tapcell_args).")] = None,
    tapcell_master: Annotated[str | None, Field(description="Tap cell master (default: from platform tapcell_args).")] = None,
    endcap_master: Annotated[str | None, Field(description="Endcap master (default: from platform tapcell_args).")] = None,
) -> dict[str, Any]:
    """Insert well-tap and endcap cells using the platform's tapcell arguments (each can be overridden)."""
    plat = active_platform(SESSION)
    state = await require_floorplan()
    if state["tap_cells"]:
        raise ToolError(f"The design already has {state['tap_cells']} tap/endcap cells.")
    tokens = tcl_split(plat.get("tapcell_args", ""))
    for flag, value in (("-distance", distance), ("-tapcell_master", tapcell_master), ("-endcap_master", endcap_master)):
        if value is None:
            continue
        if flag in tokens and tokens.index(flag) + 1 < len(tokens):
            tokens[tokens.index(flag) + 1] = str(value)
        else:
            tokens += [flag, str(value)]
    if not tokens:
        raise ToolError(f"Platform '{plat.name}' defines no tapcell_args; pass distance and tapcell_master.")
    script = "tapcell " + " ".join(tcl_quote(t) for t in tokens)
    result = await run_step(SESSION, script, parse_tapcell, "insert_tapcells")
    result["summary"]["command"] = script
    return result


@mcp.tool(annotations=MUTATING)
async def generate_pdn(
    pdn_cfg: Annotated[str | None, Field(description="PDN Tcl script (default: platform pdn_cfg).")] = None,
    replace: Annotated[bool, Field(description="Rip up an existing power grid first.")] = False,
) -> dict[str, Any]:
    """Build the power distribution network: source the PDN script (global connections, voltage domains,
    grids) and run pdngen. Returns the grids built and the special-net wire counts."""
    plat = active_platform(SESSION)
    state = await require_floorplan()
    cfg = pdn_cfg or plat.require("pdn_cfg")
    if state["pdn_wires"] and not replace:
        raise ToolError("The design already has a power grid. Pass replace=true to rip it up and build it again.")
    lines = ["pdngen -ripup", "pdngen -reset"] if state["pdn_wires"] else []
    lines += [f"source {tcl_file(cfg)}", "pdngen"]
    result = await run_step(SESSION, "\n".join(lines), parse_pdn, "generate_pdn")
    records, _ = await query(SESSION, "__place_snets")
    result["summary"]["special_nets"] = [
        {"net": r[1], "type": r[2], "swires": int(r[3]), "shapes": int(r[4])} for r in by_kind(records, "snet")
    ]
    return result


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def global_placement(
    density: Annotated[float | None, Field(gt=0, le=1, description="Target placement density (default: platform global_place_density).")] = None,
    routability_driven: Annotated[bool, Field(description="Inflate cells in congested areas (uses the router's layer settings).")] = True,
    timing_driven: Annotated[bool, Field(description="Weight critical nets (needs the platform's wire RC).")] = False,
    skip_io: Annotated[bool | None, Field(description="Ignore I/O pins (default: automatically while pins are unplaced).")] = None,
    incremental: Annotated[bool, Field(description="Start from the current placement.")] = False,
    pad: Annotated[int | None, Field(ge=0, description="Cell padding in sites on each side (default: platform global_place_pad).")] = None,
    overflow: Annotated[float | None, Field(gt=0, lt=1, description="Stop at this overflow (default 0.1).")] = None,
) -> dict[str, Any]:
    """Global placement (Nesterov). Returns iterations, final overflow and HPWL, area and congestion."""
    plat = active_platform(SESSION)
    state = await require_floorplan()
    auto_skip = state["pins_placed"] < state["pins"]
    skip = auto_skip if skip_io is None else skip_io
    if not skip and auto_skip:
        raise ToolError("Some I/O pins are not placed; call place_io_pins first or use skip_io=true.")
    if timing_driven and not plat.get("wire_rc_layer"):
        raise ToolError(f"Timing-driven placement needs wire RC; platform '{plat.name}' has no wire_rc_layer.")
    dens = density if density is not None else plat.get("global_place_density")
    padding = pad if pad is not None else pad_sites(plat.get("global_place_pad"))
    args = ["global_placement", f"-density {dens}", f"-pad_left {padding}", f"-pad_right {padding}"]
    if routability_driven:
        args.append("-routability_driven")
    if timing_driven:
        args.append("-timing_driven")
    if skip:
        args.append("-skip_io")
    if incremental:
        args.append("-incremental")
    if overflow is not None:
        args.append(f"-overflow {overflow}")
    lines = [routing_setup_tcl(plat)] if routability_driven else []
    lines += [" ".join(args), estimate_tcl()]
    result = await run_step(SESSION, "\n".join(lines), parse_gpl, f"global_placement{' skip_io' if skip else ''}")
    after_placement()
    if skip:
        result["summary"]["skip_io"] = True
        if auto_skip:
            result["summary"]["next"] = "I/O pins were ignored: call place_io_pins, then global_placement again."
    return result


@mcp.tool(annotations=MUTATING)
async def detailed_placement(
    pad: Annotated[int | None, Field(ge=0, description="Padding in sites on each side (default: platform detail_place_pad).")] = None,
    max_displacement: Annotated[list[float] | None, Field(description="Max move in microns: [both] or [x, y].")] = None,
    disallow_one_site_gaps: Annotated[bool, Field(description="Forbid one-site gaps between cells.")] = False,
) -> dict[str, Any]:
    """Legalize the placement onto rows and sites, then check_placement. Returns displacement and HPWL change."""
    plat = active_platform(SESSION)
    await require_floorplan()
    padding = pad if pad is not None else pad_sites(plat.get("detail_place_pad"))
    args = ["detailed_placement"]
    if max_displacement is not None:
        if len(max_displacement) not in (1, 2):
            raise ToolError("max_displacement must be [both] or [x, y] in microns.")
        args.append(f"-max_displacement {tcl_list(max_displacement) if len(max_displacement) == 2 else max_displacement[0]}")
    if disallow_one_site_gaps:
        args.append("-disallow_one_site_gaps")
    script = "\n".join([f"set_placement_padding -global -left {padding} -right {padding}", " ".join(args), check_tcl(),
                        estimate_tcl()])
    result = await run_step(SESSION, script, lambda log: {**parse_dpl(log), **parse_check_placement(log)},
                            f"detailed_placement pad={padding}")
    after_placement()
    return result


@mcp.tool(annotations=MUTATING)
async def optimize_placement(
    mirroring: Annotated[bool, Field(description="Flip cells to shorten wires (optimize_mirroring).")] = True,
    improve: Annotated[bool, Field(description="Swap/reorder cells to shorten wires (improve_placement).")] = True,
    max_displacement: Annotated[float | None, Field(gt=0, description="Max move for improve_placement, microns.")] = None,
) -> dict[str, Any]:
    """Wirelength clean-up after legalization (mirroring and detailed improvement), then check_placement."""
    if not mirroring and not improve:
        raise ToolError("Enable mirroring and/or improve.")
    await require_floorplan()
    lines = []
    if mirroring:
        lines += ['puts "== mirroring"', "optimize_mirroring"]
    if improve:
        lines += ['puts "== improve"', "improve_placement" + (f" -max_displacement {max_displacement}" if max_displacement else "")]
    lines += [check_tcl(), estimate_tcl()]

    def parse(log: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if mirroring:
            out["mirroring"] = parse_optimize_mirroring(log)
        if improve:
            out["improve"] = parse_improve_placement(log)
        return {**out, **parse_check_placement(log), "warnings": messages(log, tool="DPL")}

    result = await run_step(SESSION, "\n".join(lines), parse, "optimize_placement")
    after_placement()
    return result


@mcp.tool(annotations=READ_ONLY)
async def check_placement(
    disallow_one_site_gaps: Annotated[bool, Field(description="Also flag one-site gaps.")] = False,
) -> dict[str, Any]:
    """Check legality: every cell on a row/site, no overlaps, padding and edge spacing respected."""
    await require_floorplan()
    script = "if {[catch {check_placement -verbose" + (" -disallow_one_site_gaps" if disallow_one_site_gaps else "") + \
        "} __err]} { puts \"CHECK_PLACEMENT_FAILED: $__err\" }"
    output = await SESSION.run(script, max_chars=50_000_000)
    return {**parse_check_placement(output), "report": tail(output, 4000) or "(clean)"}


@mcp.tool(annotations=MUTATING)
async def filler_placement(
    masters: Annotated[list[str] | None, Field(description="Filler masters or patterns (default: platform filler_cells).")] = None,
    prefix: Annotated[str | None, Field(description="Instance name prefix.")] = None,
) -> dict[str, Any]:
    """Fill the gaps in the rows with filler cells (normally after detailed routing is planned; remove_fillers undoes it)."""
    plat = active_platform(SESSION)
    await require_floorplan()
    fill = masters or tcl_split(plat.require("filler_cells"))
    script = "filler_placement " + (f"-prefix {tcl_quote(prefix)} " if prefix else "") + tcl_list(fill)
    result = await run_step(SESSION, script, parse_filler, "filler_placement")
    result["summary"]["fillers_in_design"] = (await place_state())["fillers"]
    return result


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_fillers() -> dict[str, Any]:
    """Remove all filler cells."""
    before = (await require_floorplan())["fillers"]
    result = await run_step(SESSION, "remove_fillers", None, "remove_fillers", before=False)
    result["summary"] = {"fillers_removed": before - (await place_state())["fillers"]}
    return result


@mcp.tool(annotations=READ_ONLY)
async def floorplan_report() -> dict[str, Any]:
    """Floorplan overview: die/core, rows, tracks, pins placed, macros, tap cells, power grid, fillers, stage."""
    state = await place_state()
    status = await design_status(SESSION)
    records, _ = await query(SESSION, "__place_snets")
    return compact({
        "stage": status.get("stage"),
        "die_um": status.get("die_um"),
        "core_um": status.get("core_um"),
        "utilization_pct": status.get("utilization_pct"),
        "area_um2": status.get("area_um2"),
        **{k: v for k, v in state.items()},
        "power_grid": [{"net": r[1], "type": r[2], "shapes": int(r[4])} for r in by_kind(records, "snet")],
        "macro_list": (await list_macros())[:50] if state["macros"] else None,
        "placement_status": status.get("placement_status"),
    })


# ---------------------------------------------------------------------------
# Whole flow
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def place_design(
    utilization: Annotated[float | None, Field(gt=0, lt=100, description="Core utilization % (or give die_area + core_area).")] = None,
    aspect_ratio: Annotated[float, Field(gt=0, description="Core height / width (with utilization).")] = 1.0,
    core_space: Annotated[list[float] | None, Field(description="Die-to-core margin, microns: [all] or [bottom, top, left, right].")] = None,
    die_area: Annotated[list[float] | None, Field(description="Die [x1, y1, x2, y2] in microns.")] = None,
    core_area: Annotated[list[float] | None, Field(description="Core [x1, y1, x2, y2] in microns.")] = None,
    density: Annotated[float | None, Field(gt=0, le=1, description="Global placement density (default: platform).")] = None,
    timing_driven: Annotated[bool, Field(description="Timing-driven final global placement.")] = False,
    optimize: Annotated[bool, Field(description="Run optimize_placement after legalization.")] = False,
) -> dict[str, Any]:
    """The reference flow's placement stage in one call: floorplan -> macros -> tap cells -> power grid ->
    global placement (I/Os skipped) -> I/O pins -> routability-driven global placement -> detailed placement.
    Returns each step's summary and the final status."""
    steps: dict[str, Any] = {}

    async def step(name: str, coro) -> None:
        try:
            result = await coro
        except ToolError as exc:
            raise ToolError(f"place_design stopped at {name}: {exc}. Completed: {list(steps) or 'none'}.") from exc
        steps[name] = result.get("summary", result)

    await step("initialize_floorplan", initialize_floorplan(
        utilization=utilization, aspect_ratio=aspect_ratio, core_space=core_space, die_area=die_area, core_area=core_area))
    await step("macro_placement", macro_placement())
    if active_platform(SESSION).get("tapcell_args"):
        await step("insert_tapcells", insert_tapcells())
    await step("generate_pdn", generate_pdn())
    await step("global_placement_skip_io", global_placement(density=density, routability_driven=False, skip_io=True))
    await step("place_io_pins", place_io_pins())
    await step("global_placement", global_placement(density=density, routability_driven=True,
                                                    timing_driven=timing_driven, skip_io=False))
    await step("detailed_placement", detailed_placement())
    if optimize:
        await step("optimize_placement", optimize_placement())
    return {"steps": steps, "status": await design_status(SESSION),
            "next": "save_checkpoint name='placed', then continue in the optimization server."}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def floorplan_and_place(utilization: str = "40") -> str:
    """Floorplan and place a netlist step by step."""
    return f"""Floorplan and place the design in the OpenROAD placement session.

1. session_status / design_status. If nothing is loaded, ask for the Verilog, top module and SDC, and the
   platform (list_platforms), then load_design.
2. initialize_floorplan utilization={utilization}; report die/core size, rows and utilization.
3. macro_placement (it reports when there are no macros), insert_tapcells, generate_pdn.
4. global_placement (I/O pins are skipped automatically), place_io_pins, then global_placement again.
5. detailed_placement; check that check_placement passed and report displacement and HPWL change.
6. snapshot to show the result, and design_status for utilization and estimated setup/hold slack.
7. save_checkpoint name="placed" with a short note, so the optimization server can continue.

Finish with a table of the key numbers after each step (utilization, overflow, HPWL, displacement)."""


@mcp.prompt()
def placement_review() -> str:
    """Review the quality of the current placement."""
    return """Review the placement in the OpenROAD placement session.

1. floorplan_report: rows, pins placed, macros, tap cells, power grid, fillers, utilization.
2. check_placement: legality.
3. report_io_pins: are pins spread sensibly over the edges?
4. snapshot (whole die), then snapshot with show={"rudy": true} if available, to spot congestion.
5. design_status: estimated setup/hold slack after placement.

Say whether the placement is ready for optimization and what to change if not (density, utilization,
pin constraints, padding, macro halo)."""


@mcp.prompt()
def io_pin_planning(edges: str = "inputs left, outputs right") -> str:
    """Constrain and place the I/O pins."""
    return f"""Plan the I/O pins in the OpenROAD placement session ({edges}).

1. report_io_pins: list the ports and their directions.
2. set_io_pin_constraint for each requested edge (direction=... or pin_names=..., region like 'left:*').
3. place_io_pins (annealing=true works well with constraints); report I/O HPWL.
4. If the cells are already placed, global_placement incremental=true then detailed_placement.
5. report_io_pins and snapshot to confirm.

Report which pins went where and the I/O HPWL before/after."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
