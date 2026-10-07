"""MCP server for OpenROAD routing: pin access, global routing (grt), antenna checking and
repair (ant/grt) and detailed routing (drt), with DRC reports.

One of four stage servers (place -> opt -> route -> signoff) that hand designs to each other
through checkpoints. Technology values (routing layers, layer adjustments, macro extension,
diode cell, filler cells) come from the active platform (see ``openroad_common.platform``) and
can be overridden per call.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from openroad_common.parsing import (
    compact,
    messages,
    parse_antennas,
    parse_check_placement,
    parse_drc_report,
    parse_drt,
    parse_filler,
    parse_grt,
    parse_wire_length_table,
)
from openroad_common.platform import tcl_split
from openroad_common.records import by_kind, first, query
from openroad_common.session import Config, OpenRoadSession
from openroad_common.stage import (
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
from openroad_common.tcl import parse_records, tcl_list, tcl_quote, to_wsl_path

ROUTE_DRIVER_TCL = STAGE_DRIVER_TCL + r"""
proc __route_layer_name {level} {
  if {$level <= 0} { return "" }
  set layer [[ord::get_db_tech] findRoutingLayer $level]
  if {[__stage_null $layer]} { return "" }
  return [$layer getName]
}

# Routing facts the route tools check before acting.
proc __route_state {} {
  set block [__stage_block]
  set guides 0
  foreach net [$block getNets] { incr guides [llength [$net getGuides]] }
  set placed 0
  set total 0
  foreach inst [$block getInsts] {
    incr total
    if {[$inst getPlacementStatus] ni {NONE UNPLACED}} { incr placed }
  }
  set clocks 0
  set propagated 0
  catch {
    set clocks [llength [all_clocks]]
    foreach clk [all_clocks] { if {[get_property $clk is_propagated]} { incr propagated } }
  }
  __mcp_rec rstate [llength [$block getRows]] $total $placed $guides [grt::have_routes] \
    [$block designIsRouted 0] $clocks $propagated
  catch {
    __mcp_rec layers \
      [__route_layer_name [$block getMinRoutingLayer]] [__route_layer_name [$block getMaxRoutingLayer]] \
      [__route_layer_name [$block getMinLayerForClock]] [__route_layer_name [$block getMaxLayerForClock]]
  }
}

proc __route_read {path} {
  if {![file exists $path]} { error "Report file not found: $path" }
  set f [open $path]
  set text [read $f]
  close $f
  foreach line [split $text "\n"] { __mcp_rec line $line }
}

proc __route_report_dir {} {
  set dir [file join $::env(HOME) openroad_mcp reports]
  file mkdir $dir
  return $dir
}
"""

CFG = Config.from_env("ROUTE", default_timeout=7200)
MAX_DRT_ITERATIONS = 64
SESSION = OpenRoadSession(CFG, "route", ROUTE_DRIVER_TCL)

# Per OpenROAD process: whether pin access ran, the last DRC report and applied adjustments.
_STATE: dict[str, Any] = {"pid": None, "pin_access": False, "drc_report": None, "adjustments": []}


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        await SESSION.close()


mcp = MCPServer(
    "openroad-route",
    title="OpenROAD Routing",
    description="Pin access, global routing, antenna repair, detailed routing and DRC reports.",
    instructions=(
        "Routing on a persistent OpenROAD session. Load a placed design with load_checkpoint (e.g. 'cts' from the "
        "optimization server). Typical order: global_route -> repair_antennas -> detailed_route -> check_antennas "
        "(repair + detailed_route again if needed) -> drc_report / routing_report, or route_design for all of it. "
        "Then save_checkpoint('routed') for the signoff server. Routing layers and adjustments default from the "
        "platform (configure_routing to change them). Distances are in microns."
    ),
    lifespan=lifespan,
)

register_stage_tools(mcp, SESSION, CFG, "route")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def route_flags() -> dict[str, Any]:
    """Per-process flags; reset whenever OpenROAD was restarted (load_design / load_checkpoint)."""
    if _STATE["pid"] != SESSION.pid:
        _STATE.update(pid=SESSION.pid, pin_access=False, drc_report=None, adjustments=[])
    return _STATE


async def route_state() -> dict[str, Any]:
    SESSION.require_design()
    records, _ = await query(SESSION, "__route_state")
    values = [int(v) for v in first(records, "rstate")[1:]]
    keys = ("rows", "instances", "placed", "guides", "global_routes", "routed", "clocks", "propagated_clocks")
    state: dict[str, Any] = dict(zip(keys, values))
    layers = first(records, "layers", required=False)
    if layers:
        state["layers"] = compact({"signal": "-".join(v for v in layers[1:3] if v) or None,
                                   "clock": "-".join(v for v in layers[3:5] if v) or None})
    return state


async def require_placed() -> dict[str, Any]:
    state = await route_state()
    if not state["rows"] or not state["instances"] or state["placed"] < state["instances"]:
        raise ToolError(
            f"The design is not fully placed ({state['placed']}/{state['instances']} instances placed). "
            "Route a placed design: load_checkpoint a 'placed' or 'cts' checkpoint."
        )
    return state


async def report_path(path: str | None, kind: str) -> str:
    """WSL path for a report: the given one, else $HOME/openroad_mcp/reports/mcp_route_<kind>.rpt
    (WSL clears /tmp when the distro goes idle, so reports are kept under $HOME)."""
    if path:
        return to_wsl_path(path)
    records, _ = await query(SESSION, "__mcp_rec dir [__route_report_dir]", require_design=False)
    return f"{first(records, 'dir')[1]}/mcp_route_{kind}.rpt"


def timed(script: str) -> str:
    return f"set __t0 [clock milliseconds]\n{script}\nputs \"== elapsed_ms [expr {{[clock milliseconds] - $__t0}}]\""


def elapsed_s(log: str) -> float | None:
    for line in reversed(log.splitlines()):
        if line.startswith("== elapsed_ms "):
            return round(int(line.split()[-1]) / 1000, 2)
    return None


def pin_access_tcl() -> str:
    flags = route_flags()
    if flags["pin_access"]:
        return ""
    return "pin_access"


class LayerAdjustment(BaseModel):
    layers: str = Field(description="Layer or layer range, e.g. 'L2' or 'L2-L6'.")
    adjustment: float = Field(ge=0, le=1, description="Fraction of routing resources to remove (0-1).")


class RegionAdjustment(BaseModel):
    area: list[float] = Field(description="Region [x1, y1, x2, y2] in microns.")
    layer: str = Field(description="Layer name.")
    adjustment: float = Field(ge=0, le=1, description="Fraction of routing resources to remove (0-1).")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def configure_routing(
    signal_layers: Annotated[str | None, Field(description="Signal routing layer range 'min-max' (default: platform global_routing_layers).")] = None,
    clock_layers: Annotated[str | None, Field(description="Clock routing layer range 'min-max' (default: platform global_routing_clock_layers).")] = None,
    layer_adjustments: Annotated[list[LayerAdjustment] | None, Field(description="Capacity reductions per layer (or range).")] = None,
    region_adjustments: Annotated[list[RegionAdjustment] | None, Field(description="Capacity reductions inside regions.")] = None,
    macro_extension: Annotated[int | None, Field(ge=0, description="GCells around macros that are blocked (default: platform macro_extension).")] = None,
    reset_to_platform: Annotated[bool, Field(description="Apply the platform's routing settings first.")] = False,
) -> dict[str, Any]:
    """Set routing layers, global-routing capacity adjustments and macro extension. Call before global_route."""
    plat = active_platform(SESSION)
    state = await require_placed()
    if state["global_routes"] and state["routed"]:
        raise ToolError("The design is already routed; routing settings only affect a new global_route.")
    flags = route_flags()
    lines: list[str] = []
    applied: list[dict[str, Any]] = []
    if reset_to_platform:
        lines.append(routing_setup_tcl(plat))
        applied += [{"layers": lay, "adjustment": adj} for lay, adj in plat.get("global_routing_layer_adjustments", [])]
    if signal_layers or clock_layers:
        signal = signal_layers or plat.require("global_routing_layers")
        cmd = f"set_routing_layers -signal {tcl_quote(signal)}"
        clock = clock_layers or plat.get("global_routing_clock_layers")
        if clock:
            cmd += f" -clock {tcl_quote(clock)}"
        lines.append(cmd)
    for adj in layer_adjustments or []:
        lines.append(f"set_global_routing_layer_adjustment {tcl_quote(adj.layers)} {adj.adjustment}")
        applied.append({"layers": adj.layers, "adjustment": adj.adjustment})
    for reg in region_adjustments or []:
        if len(reg.area) != 4:
            raise ToolError("Region area must be [x1, y1, x2, y2] in microns.")
        lines.append(f"set_global_routing_region_adjustment {tcl_list(reg.area)} -layer {tcl_quote(reg.layer)} "
                     f"-adjustment {reg.adjustment}")
        applied.append({"region": reg.area, "layer": reg.layer, "adjustment": reg.adjustment})
    if macro_extension is not None:
        lines.append(f"set_macro_extension {macro_extension}")
    if not lines:
        raise ToolError("Nothing to change: give layers, adjustments, macro_extension or reset_to_platform=true.")
    output = await SESSION.run("\n".join(lines))
    flags["adjustments"] = (flags["adjustments"] if not reset_to_platform else []) + applied
    record_step(SESSION, "configure_routing")
    return compact({"routing_layers": (await route_state()).get("layers"), "adjustments": flags["adjustments"],
                    "macro_extension": macro_extension, "warnings": messages(output)})


# ---------------------------------------------------------------------------
# Global routing and antennas
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def global_route(
    congestion_iterations: Annotated[int, Field(ge=0, description="Rip-up-and-reroute iterations to remove overflow.")] = 100,
    allow_congestion: Annotated[bool, Field(description="Finish even if overflow remains (otherwise it is an error).")] = False,
    critical_nets_percentage: Annotated[float | None, Field(ge=0, le=100, description="Share of timing-critical nets routed first.")] = None,
    skip_large_fanout_nets: Annotated[int | None, Field(ge=1, description="Do not route nets with more pins than this.")] = None,
    congestion_report_file: Annotated[str | None, Field(description="Write a congestion report (WSL path).")] = None,
    guide_file: Annotated[str | None, Field(description="Also write route guides to this file (WSL path).")] = None,
    estimate_parasitics: Annotated[bool, Field(description="Estimate parasitics from the global routes afterwards.")] = True,
) -> dict[str, Any]:
    """Global routing (after pin access): routed nets, wirelength, vias and the per-layer congestion report."""
    await require_placed()
    args = ["global_route", f"-congestion_iterations {congestion_iterations}", "-verbose"]
    if allow_congestion:
        args.append("-allow_congestion")
    if critical_nets_percentage is not None:
        args.append(f"-critical_nets_percentage {critical_nets_percentage}")
    if skip_large_fanout_nets is not None:
        args.append(f"-skip_large_fanout_nets {skip_large_fanout_nets}")
    if congestion_report_file:
        args.append(f"-congestion_report_file {tcl_quote(to_wsl_path(congestion_report_file))}")
    if guide_file:
        args.append(f"-guide_file {tcl_quote(to_wsl_path(guide_file))}")
    lines = [pin_access_tcl(), " ".join(args)]
    if estimate_parasitics:
        lines.append("estimate_parasitics -global_routing")

    def parse(log: str) -> dict[str, Any]:
        return {**parse_grt(log), "elapsed_s": elapsed_s(log)}

    try:
        result = await run_step(SESSION, timed("\n".join(lines)), parse, "global_route")
    except ToolError as exc:
        text = str(exc)
        if "GRT-0116" in text or "congestion" in text.lower():
            raise ToolError(
                f"Global routing could not remove all overflow. {parse_grt(text).get('congestion', '')}\n"
                "Options: global_route allow_congestion=true to inspect it, configure_routing with smaller layer "
                "adjustments or more layers, or re-place at lower density (mcp-place).\n" + text
            ) from exc
        raise
    route_flags()["pin_access"] = True
    if estimate_parasitics:
        SESSION.design.parasitics = "global routing estimate"
    return result


@mcp.tool(annotations=READ_ONLY)
async def report_wire_length(
    source: Annotated[str, Field(pattern="^(global|detailed)$", description="'global' (route guides) or 'detailed' (wires).")] = "detailed",
    nets: Annotated[list[str] | None, Field(description="Report these nets instead of the per-layer summary.")] = None,
) -> dict[str, Any]:
    """Wirelength per routing layer (or per net) from the global or detailed routes."""
    state = await route_state()
    if source == "global" and not state["global_routes"]:
        raise ToolError("There are no global routes. Call global_route first.")
    if source == "detailed" and not state["routed"]:
        raise ToolError("The design has no detailed routing. Call detailed_route first.")
    flag = "-global_route" if source == "global" else "-detailed_route"
    if nets:
        output = await SESSION.run(f"report_wire_length -net {tcl_list(nets)} {flag}")
        lengths = {n: float(v) for n, v in re.findall(r"GRT-02(?:37|40)\] Net (\S+) \w+ route wire length: (\S+)um", output)}
        return {"source": source, "nets": lengths, "missing": [n for n in nets if n not in lengths]}
    output = await SESSION.run(f"report_wire_length {flag} -summary")
    return {"source": source, **parse_wire_length_table(output)}


@mcp.tool(annotations=READ_ONLY)
async def check_antennas(
    net: Annotated[str | None, Field(description="Check only this net.")] = None,
    verbose: Annotated[bool, Field(description="Include the per-pin antenna ratio report.")] = False,
) -> dict[str, Any]:
    """Antenna-rule check on the global or detailed routes: violating nets and pins."""
    state = await route_state()
    if not state["global_routes"] and not state["routed"]:
        raise ToolError("Nothing is routed yet. Call global_route first.")
    args = ["check_antennas"] + (["-verbose"] if verbose else []) + ([f"-net {tcl_quote(net)}"] if net else [])
    output = await SESSION.run(" ".join(args), max_chars=50_000_000)
    _, log = parse_records(output)
    result = parse_antennas(log)
    result["checked"] = "detailed routes" if state["routed"] else "global routes"
    if verbose:
        result["report"] = tail(log, 12000)
    return result


@mcp.tool(annotations=MUTATING)
async def repair_antennas(
    iterations: Annotated[int, Field(ge=1, le=20, description="Repair iterations.")] = 5,
    diode_cell: Annotated[str | None, Field(description="Diode master (default: platform diode_cell, else the library's antenna cell).")] = None,
    ratio_margin: Annotated[float | None, Field(ge=0, lt=100, description="Extra antenna ratio margin %.")] = None,
    jumper_only: Annotated[bool, Field(description="Fix only by layer jumpers.")] = False,
    diode_only: Annotated[bool, Field(description="Fix only by diodes.")] = False,
    allow_congestion: Annotated[bool, Field(description="Allow congestion while re-routing repaired nets.")] = False,
) -> dict[str, Any]:
    """Fix antenna violations with jumpers and/or diodes and re-route the affected nets (needs global routes).
    Run detailed_route afterwards when the design was already detail-routed."""
    plat = active_platform(SESSION)
    state = await route_state()
    if not state["global_routes"]:
        raise ToolError("repair_antennas needs global routes. Call global_route first.")
    if jumper_only and diode_only:
        raise ToolError("Choose jumper_only or diode_only, not both.")
    diode = diode_cell or plat.get("diode_cell")
    args = ["repair_antennas"] + ([tcl_quote(diode)] if diode else []) + [f"-iterations {iterations}"]
    if ratio_margin is not None:
        args.append(f"-ratio_margin {ratio_margin}")
    if jumper_only:
        args.append("-jumper_only")
    if diode_only:
        args.append("-diode_only")
    if allow_congestion:
        args.append("-allow_congestion")
    script = " ".join(args) + "\ncheck_antennas"

    def parse(log: str) -> dict[str, Any]:
        out = parse_antennas(log)
        if out.get("no_diode"):
            out["message"] = ("No diode cell is available (the platform has no diode_cell and the library has no "
                              "ANTENNACELL), so only jumpers can fix antennas. Pass diode_cell if the library has one.")
        out["warnings"] = messages(log)
        return out

    result = await run_step(SESSION, script, parse, "repair_antennas")
    if state["routed"] and result["summary"].get("diodes_inserted"):
        result["summary"]["next"] = "Diodes were inserted after detailed routing: run detailed_route again."
    return result


# ---------------------------------------------------------------------------
# Detailed routing and DRC
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def detailed_route(
    end_iteration: Annotated[int | None, Field(ge=1, le=64, description="Stop after this optimization iteration (default: until clean, at most 64).")] = None,
    drc_report_file: Annotated[str | None, Field(description="DRC report path (default: $HOME/openroad_mcp/reports/mcp_route_drc.rpt in WSL).")] = None,
    or_seed: Annotated[int | None, Field(description="Random seed for the router.")] = None,
    via_in_pin_bottom_layer: Annotated[str | None, Field(description="Lowest layer for vias inside pins.")] = None,
    via_in_pin_top_layer: Annotated[str | None, Field(description="Highest layer for vias inside pins.")] = None,
    min_access_points: Annotated[int | None, Field(ge=1, description="Minimum pin access points per pin.")] = None,
) -> dict[str, Any]:
    """Detailed routing along the global-route guides: per-iteration violations, final DRC by type and layer,
    wirelength and vias. Writes a DRC report that drc_report reads.

    Running it again continues from the existing wires (needed after repair_antennas inserts diodes); it
    does not start over. To route from scratch, load the unrouted checkpoint again."""
    state = await require_placed()
    if not state["global_routes"] and not state["guides"]:
        raise ToolError("There are no global routes or route guides. Call global_route first.")
    flags = route_flags()
    args = ["detailed_route", f"-output_drc $__drc", "-verbose 1"]
    if flags["pin_access"]:
        args.append("-no_pin_access")
    # Always explicit: the router keeps the last -droute_end_iter it was given (64 is its maximum).
    args.append(f"-droute_end_iter {end_iteration if end_iteration is not None else MAX_DRT_ITERATIONS}")
    if or_seed is not None:
        args.append(f"-or_seed {or_seed}")
    if via_in_pin_bottom_layer:
        args.append(f"-via_in_pin_bottom_layer {tcl_quote(via_in_pin_bottom_layer)}")
    if via_in_pin_top_layer:
        args.append(f"-via_in_pin_top_layer {tcl_quote(via_in_pin_top_layer)}")
    if min_access_points is not None:
        args.append(f"-min_access_points {min_access_points}")
    drc_path = await report_path(drc_report_file, "drc")
    script = (
        f"set __drc {tcl_quote(drc_path)}\n"
        + timed(" ".join(args))
        + "\nputs \"== drvs [detailed_route_num_drvs]\"\nputs \"== routed [design_is_routed]\""
    )

    def parse(log: str) -> dict[str, Any]:
        out = parse_drt(log)
        drvs = re.findall(r"^== drvs (\d+)", log, re.M)
        routed = re.findall(r"^== routed (\d)", log, re.M)
        out.update(compact({"drc_violations": int(drvs[-1]) if drvs else None,
                            "all_nets_routed": routed[-1] == "1" if routed else None,
                            "elapsed_s": elapsed_s(log)}))
        return out

    label = "detailed_route" + (f" end_iter={end_iteration}" if end_iteration is not None else "")
    result = await run_step(SESSION, script, parse, label)
    flags["pin_access"] = True
    flags["drc_report"] = drc_path
    result["summary"]["drc_report"] = flags["drc_report"]
    return result


@mcp.tool(annotations=READ_ONLY)
async def drc_report(
    limit: Annotated[int, Field(ge=0, le=10000, description="How many individual violations to list.")] = 50,
    report_file: Annotated[str | None, Field(description="Report to read (default: the last detailed_route report).")] = None,
    recheck: Annotated[bool, Field(description="Run a fresh DRC check on the current routing instead of reading a report.")] = False,
    area: Annotated[list[float] | None, Field(description="With recheck: only check [x1, y1, x2, y2] microns.")] = None,
) -> dict[str, Any]:
    """Design-rule violations of the detailed routing, grouped by type, layer and net, with locations (microns)."""
    state = await route_state()
    if recheck:
        if not state["routed"]:
            raise ToolError("The design has no detailed routing to check. Call detailed_route first.")
        box = ""
        if area is not None:
            if len(area) != 4:
                raise ToolError("area must be [x1, y1, x2, y2] in microns.")
            box = (" -box [list " + " ".join(f"[expr {{round({v} * [[__stage_block] getDbUnitsPerMicron])}}]"
                                              for v in area) + "]")
        path = await report_path(None, "check_drc")
        records, _ = await query(SESSION, f"drt::check_drc -output_file {tcl_quote(path)}{box}\n"
                                          f"__route_read {tcl_quote(path)}")
    else:
        path = to_wsl_path(report_file) if report_file else route_flags()["drc_report"]
        if not path:
            raise ToolError("No DRC report in this session. Run detailed_route, pass report_file, or use recheck=true.")
        records, _ = await query(SESSION, f"__route_read {tcl_quote(path)}")
    text = "\n".join(r[1] if len(r) > 1 else "" for r in by_kind(records, "line"))
    return {"report_file": path, "source": "check_drc" if recheck else "detailed_route",
            **parse_drc_report(text, limit)}


@mcp.tool(annotations=READ_ONLY)
async def routing_report() -> dict[str, Any]:
    """Routing status on one page: global/detailed routing done, unrouted nets, DRC, antennas, wirelength per layer."""
    state = await route_state()
    out: dict[str, Any] = {
        "stage": "routed" if state["routed"] else ("globally routed" if state["global_routes"] else "not routed"),
        "routing_layers": state.get("layers"),
        "global_routes": bool(state["global_routes"]),
        "route_guides": state["guides"],
        "all_nets_routed": bool(state["routed"]),
        "clocks_propagated": f"{state['propagated_clocks']}/{state['clocks']}",
    }
    if state["global_routes"] or state["routed"]:
        output = await SESSION.run("check_antennas", max_chars=50_000_000)
        out["antennas"] = parse_antennas(output)
    if state["routed"]:
        output = await SESSION.run(
            "report_wire_length -detailed_route -summary\n"
            "if {[catch {puts \"== drvs [detailed_route_num_drvs]\"}]} { puts \"== drvs ?\" }",
            max_chars=50_000_000,
        )
        out["wirelength"] = parse_wire_length_table(output)
        drvs = output.rsplit("== drvs ", 1)[-1].split()[0] if "== drvs " in output else "?"
        out["drc_violations_last_route"] = int(drvs) if drvs.isdigit() else "unknown (not detail-routed in this session)"
    elif state["global_routes"]:
        output = await SESSION.run("report_wire_length -global_route -summary")
        out["global_wirelength"] = parse_wire_length_table(output)
    flags = route_flags()
    if flags["drc_report"]:
        out["drc_report"] = flags["drc_report"]
    return out


# ---------------------------------------------------------------------------
# Whole flow
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def route_design(
    congestion_iterations: Annotated[int, Field(ge=0, description="Global-route overflow iterations.")] = 100,
    antenna_iterations: Annotated[int, Field(ge=1, le=20, description="Iterations per repair_antennas call.")] = 5,
    fix_loops: Annotated[int, Field(ge=0, le=10, description="Antenna repair + re-route loops after detailed routing.")] = 5,
    end_iteration: Annotated[int | None, Field(ge=1, le=64, description="Detailed-route end iteration (default: until clean).")] = None,
    insert_fillers: Annotated[bool, Field(description="Fill row gaps with the platform's filler cells at the end.")] = True,
) -> dict[str, Any]:
    """The reference flow's routing stage in one call: pin access -> global route -> antenna repair ->
    detailed route -> (antenna repair + detailed route)* -> antenna check -> fillers. Returns each step."""
    plat = active_platform(SESSION)
    state = await require_placed()
    if state["routed"]:
        raise ToolError("The design is already routed. Load an unrouted checkpoint to route it again.")
    steps: dict[str, Any] = {}
    warnings: list[str] = []
    if state["clocks"] and state["propagated_clocks"] < state["clocks"]:
        warnings.append("Clocks are ideal (no clock tree?). Normally CTS runs in the optimization server first.")

    async def step(name: str, coro) -> dict[str, Any]:
        try:
            result = await coro
        except ToolError as exc:
            raise ToolError(f"route_design stopped at {name}: {exc}. Completed: {list(steps) or 'none'}.") from exc
        steps[name] = result.get("summary", result)
        return steps[name]

    await step("global_route", global_route(congestion_iterations=congestion_iterations))
    await step("repair_antennas", repair_antennas(iterations=antenna_iterations))
    await step("detailed_route", detailed_route(end_iteration=end_iteration))
    loops = 0
    antennas = await check_antennas()
    while antennas.get("net_violations") and loops < fix_loops and not antennas.get("no_diode"):
        loops += 1
        repaired = await step(f"repair_antennas_{loops}", repair_antennas(iterations=antenna_iterations))
        if not repaired.get("diodes_inserted") and not repaired.get("jumpers_inserted"):
            break
        await step(f"detailed_route_{loops}", detailed_route(end_iteration=end_iteration))
        antennas = await check_antennas()
    steps["final_antennas"] = antennas
    if insert_fillers and plat.get("filler_cells"):
        script = f"filler_placement {tcl_list(tcl_split(plat.get('filler_cells')))}\n" \
                 "if {[catch {check_placement -verbose} __err]} { puts \"CHECK_PLACEMENT_FAILED: $__err\" }"
        fill = await run_step(SESSION, script, lambda log: {**parse_filler(log), **parse_check_placement(log)},
                              "filler_placement", before=False)
        steps["filler_placement"] = fill["summary"]
    final = steps.get(f"detailed_route_{loops}") or steps["detailed_route"]
    return compact({
        "steps": steps,
        "antenna_fix_loops": loops,
        "drc_violations": final.get("drc_violations"),
        "antenna_violations": antennas.get("net_violations"),
        "wirelength_um": final.get("wirelength_um"),
        "vias": final.get("vias"),
        "status": await design_status(SESSION),
        "warnings": warnings,
        "next": "save_checkpoint name='routed', then continue in the signoff server.",
    })


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def route_and_verify() -> str:
    """Route a placed, clock-tree-synthesized design and verify it."""
    return """Route the design in the OpenROAD routing session and verify the result.

1. design_status. If nothing is loaded, list_checkpoints and load_checkpoint the latest 'cts' checkpoint.
2. route_design. Report the global route (wirelength, vias, overflow), the detailed-route iterations
   (violations per iteration), antenna fixes and fillers.
3. If DRC violations remain: drc_report and summarize them by type and layer (see the drc_triage prompt).
4. routing_report and check_antennas: confirm every net is routed and there are no antenna violations.
5. design_status: setup/hold slack with global-routing parasitics.
6. snapshot of the routed layout.
7. save_checkpoint name="routed" with a short note, so the signoff server can continue.

Finish with a table: wirelength, vias, DRC count, antenna count, setup/hold WNS, and a verdict."""


@mcp.prompt()
def fix_congestion() -> str:
    """Diagnose and fix global-routing congestion."""
    return """Fix global-routing congestion in the OpenROAD routing session.

1. global_route allow_congestion=true and read the per-layer congestion table (usage %, overflow).
2. snapshot with show={"routing": false} to see where cells are dense.
3. Try, one at a time, re-running global_route after each:
   - configure_routing with smaller layer_adjustments on the overflowing layers, or a wider signal layer range;
   - configure_routing region_adjustments only where it is congested;
   - more congestion_iterations.
4. If overflow stays, recommend re-placing at lower density or utilization in the placement server.

Report overflow before/after each attempt and the setting that worked."""


@mcp.prompt()
def drc_triage() -> str:
    """Explain and reduce detailed-routing DRC violations."""
    return """Triage the detailed-routing DRC violations in the OpenROAD routing session.

1. drc_report (limit 30): totals by type, by layer and the nets that appear most often.
2. For the main groups, explain the likely cause (shorts and spacing in pin-dense areas, cut spacing on
   via layers, min-area/EOL on narrow wires) using the coordinates.
3. snapshot area=<box around the worst cluster> to look at it.
4. Try detailed_route again with more iterations or another or_seed; for shorts at pins, more
   min_access_points. If violations cluster in dense regions, suggest placement padding or lower density.
5. drc_report recheck=true to confirm the final count.

Report the violation counts before/after and what changed."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
