"""MCP server for OpenROAD design optimization: parasitic estimation (est), the Resizer (rsz),
clock tree synthesis (cts) and legalization (dpl).

One of four stage servers (place -> opt -> route -> signoff) that hand designs to each other
through checkpoints. Technology values (CTS buffer, tie cells, wire-RC layers, padding, ...)
come from the active platform (see ``openroad_common.platform``) and can be overridden per call.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from openroad_common.parsing import (
    compact,
    grab,
    messages,
    parse_check_placement,
    parse_clock_skew,
    parse_cts,
    parse_dpl,
    parse_repair_design,
    parse_repair_timing,
    parse_rsz_counts,
)
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
    run_step,
    tail,
)
from openroad_common.tcl import parse_records, tcl_file, tcl_list, tcl_quote

CFG = Config.from_env("OPT", default_timeout=1800)
SESSION = OpenRoadSession(CFG, "opt", STAGE_DRIVER_TCL)


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        await SESSION.close()


mcp = MCPServer(
    "openroad-opt",
    title="OpenROAD Optimization",
    description="Parasitic estimation, gate sizing/buffering (Resizer), clock tree synthesis and legalization.",
    instructions=(
        "Design optimization on a persistent OpenROAD session. Load a placed design with load_checkpoint "
        "(e.g. 'placed' from the place server) or load_design. Typical order: setup_parasitics -> repair_design "
        "-> repair_tie_fanout -> legalize -> clock_tree_synthesis -> repair_timing -> save_checkpoint('cts') for "
        "the route server. Technology values default from the platform (list_platforms / platform_info). "
        "Distances are in microns, times in the Liberty time unit."
    ),
    lifespan=lifespan,
)

register_stage_tools(mcp, SESSION, CFG, "opt")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def ensure_parasitics() -> None:
    """Estimate placement parasitics first if the design is placed and none are active."""
    if SESSION.design.parasitics:
        return
    status = await design_status(SESSION)
    if status.get("stage") in ("placed", "partially placed", "routed"):
        await SESSION.run("estimate_parasitics -placement")
        SESSION.design.parasitics = "placement estimate"


def pad_sites(value: float | None) -> int:
    return int(round(value or 0))


def legalize_tcl(padding: int, max_displacement: float | None = None) -> str:
    md = f" -max_displacement {max_displacement}" if max_displacement else ""
    return (
        f"set_placement_padding -global -left {padding} -right {padding}\n"
        f"detailed_placement{md}\n"
        "if {[catch {check_placement -verbose} __err]} { puts \"CHECK_PLACEMENT_FAILED: $__err\" }"
    )


_LAYER_RC_RE = re.compile(r"^\s*(\S+)\s*\|\s*([-\d.eE+]+)\s*\|\s*([-\d.eE+]+)\s*$", re.MULTILINE)
_VIA_RC_RE = re.compile(r"^\s*(\S+)\s*\|\s*([-\d.eE+]+)\s*$", re.MULTILINE)


def parse_layer_rc(log: str) -> dict[str, Any]:
    layers = [{"layer": n, "r_kohm_per_um": float(r), "c_ff_per_um": float(c)} for n, r, c in _LAYER_RC_RE.findall(log)]
    vias = [{"via": n, "r_kohm": float(r)} for n, r in _VIA_RC_RE.findall(log)]
    return compact({"layers": layers, "vias": vias, "warnings": messages(log)})


# ---------------------------------------------------------------------------
# Parasitics and dont-use/dont-touch
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def setup_parasitics(
    signal_layer: Annotated[str | None, Field(description="Layer whose RC models signal wires (default: platform wire_rc_layer).")] = None,
    clock_layer: Annotated[str | None, Field(description="Layer whose RC models clock wires (default: platform wire_rc_layer_clk).")] = None,
    rc_file: Annotated[str | None, Field(description="Layer RC Tcl file (default: platform layer_rc_file).")] = None,
) -> dict[str, Any]:
    """Set per-layer and wire RC used for parasitic estimation, then re-estimate if the design is placed."""
    SESSION.require_design()
    plat = active_platform(SESSION)
    signal = signal_layer or plat.require("wire_rc_layer")
    clock = clock_layer or plat.get("wire_rc_layer_clk") or signal
    rc = rc_file or plat.get("layer_rc_file")
    lines = [f"source {tcl_file(rc)}"] if rc else []
    lines += [
        f"set_wire_rc -signal -layer {tcl_quote(signal)}",
        f"set_wire_rc -clock -layer {tcl_quote(clock)}",
        "report_layer_rc",
    ]
    _, log = parse_records(await SESSION.run("\n".join(lines), max_chars=50_000_000))
    SESSION.design.parasitics = None
    await ensure_parasitics()
    record_step(SESSION, f"setup_parasitics signal={signal} clock={clock}")
    return {"signal_layer": signal, "clock_layer": clock, "rc_file": rc, "parasitics": SESSION.design.parasitics,
            **parse_layer_rc(log)}


@mcp.tool(annotations=MUTATING)
async def set_dont_use(
    cells: Annotated[list[str], Field(description="Library cell names or patterns, e.g. ['*_X1'].")],
    unset: Annotated[bool, Field(description="Remove the dont-use mark instead.")] = False,
) -> dict[str, Any]:
    """Mark library cells the optimizer must not use (or allow them again)."""
    SESSION.require_design()
    command = "unset_dont_use" if unset else "set_dont_use"
    output = await SESSION.run(f"{command} {tcl_list(cells)}\nreport_dont_use")
    record_step(SESSION, f"{command} {' '.join(cells)}")
    return {"done": f"{command} {cells}", "report": tail(output)}


@mcp.tool(annotations=MUTATING)
async def set_dont_touch(
    objects: Annotated[list[str], Field(description="Instance or net names/patterns the optimizer must not change.")],
    unset: Annotated[bool, Field(description="Remove the dont-touch mark instead.")] = False,
) -> dict[str, Any]:
    """Protect instances or nets from sizing/buffering (or release them)."""
    SESSION.require_design()
    command = "unset_dont_touch" if unset else "set_dont_touch"
    finds = " ".join(f"[get_cells -quiet {tcl_quote(o)}] [get_nets -quiet {tcl_quote(o)}]" for o in objects)
    output = await SESSION.run(
        f"set __objs [concat {finds}]\n"
        "if {[llength $__objs] == 0} { error \"No instance or net matches the given names\" }\n"
        f"{command} $__objs\nputs \"{command}: [llength $__objs] object(s)\"\nreport_dont_touch"
    )
    record_step(SESSION, f"{command} {' '.join(objects)}")
    return {"done": command, "report": tail(output)}


# ---------------------------------------------------------------------------
# Resizer
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def buffer_ports(
    inputs: Annotated[bool, Field(description="Buffer input ports.")] = True,
    outputs: Annotated[bool, Field(description="Buffer output ports.")] = True,
    buffer_cell: Annotated[str | None, Field(description="Buffer cell to use (default: Resizer's choice).")] = None,
    max_utilization: Annotated[float | None, Field(ge=0, le=100, description="Stop above this utilization %.")] = None,
) -> dict[str, Any]:
    """Insert a buffer between each input/output port and its logic."""
    if not inputs and not outputs:
        raise ToolError("Set inputs and/or outputs.")
    args = ["buffer_ports"]
    if inputs and not outputs:
        args.append("-inputs")
    if outputs and not inputs:
        args.append("-outputs")
    if buffer_cell:
        args += ["-buffer_cell", tcl_quote(buffer_cell)]
    if max_utilization is not None:
        args += ["-max_utilization", str(max_utilization)]
    return await run_step(SESSION, " ".join(args), parse_rsz_counts, "buffer_ports")


@mcp.tool(annotations=MUTATING)
async def remove_buffers(
    instances: Annotated[list[str] | None, Field(description="Only these buffer instances (default: all removable buffers).")] = None,
) -> dict[str, Any]:
    """Remove buffers (e.g. synthesis buffers before placement-driven repair)."""
    script = "remove_buffers" + (f" [get_cells {tcl_list(instances)}]" if instances else "")
    return await run_step(SESSION, script, parse_rsz_counts, "remove_buffers")


@mcp.tool(annotations=MUTATING)
async def repair_design(
    max_wire_length: Annotated[float | None, Field(gt=0, description="Buffer wires longer than this (microns).")] = None,
    slew_margin: Annotated[float, Field(ge=0, lt=100, description="Extra slew margin %.")] = 0,
    cap_margin: Annotated[float, Field(ge=0, lt=100, description="Extra capacitance margin %.")] = 0,
    max_utilization: Annotated[float | None, Field(ge=0, le=100, description="Stop above this utilization %.")] = None,
    match_cell_footprint: Annotated[bool, Field(description="Only resize to cells with the same footprint.")] = False,
) -> dict[str, Any]:
    """Fix max slew, max capacitance, max fanout and long-wire violations by buffering and resizing."""
    await ensure_parasitics()
    args = ["repair_design", f"-slew_margin {slew_margin}", f"-cap_margin {cap_margin}"]
    if max_wire_length:
        args.append(f"-max_wire_length {max_wire_length}")
    if max_utilization is not None:
        args.append(f"-max_utilization {max_utilization}")
    if match_cell_footprint:
        args.append("-match_cell_footprint")
    return await run_step(SESSION, " ".join(args), parse_repair_design, "repair_design")


@mcp.tool(annotations=MUTATING)
async def repair_tie_fanout(
    separation: Annotated[float | None, Field(ge=0, description="Tie cell to load distance in microns (default: platform tie_separation).")] = None,
    max_fanout: Annotated[int | None, Field(ge=1, description="Maximum loads per tie cell.")] = None,
) -> dict[str, Any]:
    """Give constant-driven loads their own nearby tie-high/tie-low cells (platform tie ports)."""
    plat = active_platform(SESSION)
    ports = [p for p in (plat.get("tielo_port"), plat.get("tiehi_port")) if p]
    if not ports:
        raise ToolError(f"Platform '{plat.name}' defines no tielo_port/tiehi_port.")
    sep = separation if separation is not None else plat.get("tie_separation", 0)
    extra = f" -max_fanout {max_fanout}" if max_fanout else ""
    script = "\n".join(f"repair_tie_fanout -separation {sep}{extra} {tcl_quote(p)}" for p in ports)
    return await run_step(SESSION, script, parse_rsz_counts, f"repair_tie_fanout ({', '.join(ports)})")


@mcp.tool(annotations=MUTATING)
async def repair_timing(
    mode: Annotated[Literal["both", "setup", "hold"], Field(description="Which checks to repair.")] = "both",
    setup_margin: Annotated[float, Field(description="Extra setup slack to aim for (time units).")] = 0,
    hold_margin: Annotated[float, Field(description="Extra hold slack to aim for (time units).")] = 0,
    max_utilization: Annotated[float | None, Field(ge=0, le=100, description="Stop above this utilization %.")] = None,
    max_buffer_percent: Annotated[float | None, Field(gt=0, le=100, description="Hold buffers allowed, % of instances (default 20).")] = None,
    skip_gate_cloning: Annotated[bool, Field(description="Do not clone gates.")] = True,
    skip_pin_swap: Annotated[bool, Field(description="Do not swap commutative pins.")] = False,
    phases: Annotated[str | None, Field(description="Custom phase list, e.g. 'LEGACY LAST_GASP' or 'TNS'.")] = None,
    max_passes: Annotated[int | None, Field(ge=1, description="Limit repair passes.")] = None,
    legalize: Annotated[bool, Field(description="Run detailed placement afterwards (recommended).")] = True,
) -> dict[str, Any]:
    """Fix setup and/or hold violations (sizing, buffering, pin swaps, Vt swaps, ...), then legalize."""
    await ensure_parasitics()
    args = ["repair_timing"]
    if mode != "both":
        args.append(f"-{mode}")
    args += [f"-setup_margin {setup_margin}", f"-hold_margin {hold_margin}"]
    if max_utilization is not None:
        args.append(f"-max_utilization {max_utilization}")
    if max_buffer_percent is not None:
        args.append(f"-max_buffer_percent {max_buffer_percent}")
    if skip_gate_cloning:
        args.append("-skip_gate_cloning")
    if skip_pin_swap:
        args.append("-skip_pin_swap")
    if phases:
        args.append(f"-phases {tcl_quote(phases)}")
    if max_passes:
        args.append(f"-max_passes {max_passes}")
    script = " ".join(args)
    if legalize:
        script += "\n" + legalize_tcl(pad_sites(active_platform(SESSION).get("detail_place_pad")))

    def parse(log: str) -> dict[str, Any]:
        out = parse_repair_timing(log)
        if legalize:
            out["legalization"] = {**parse_dpl(log), **parse_check_placement(log)}
        return out

    return await run_step(SESSION, script, parse, f"repair_timing {mode}")


@mcp.tool(annotations=MUTATING)
async def recover_power(
    percent: Annotated[float, Field(gt=0, le=100, description="Share of paths with positive slack to downsize, %.")] = 100,
) -> dict[str, Any]:
    """Downsize gates on paths with positive slack to save power/area without creating violations."""
    await ensure_parasitics()
    return await run_step(
        SESSION,
        f"repair_timing -recover_power {percent}\nreport_power",
        lambda log: {**parse_repair_timing(log), "power_report": tail(log, 2500)},
        f"recover_power {percent}%",
    )


# ---------------------------------------------------------------------------
# Clock tree synthesis
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def clock_tree_synthesis(
    root_buf: Annotated[str | None, Field(description="Root buffer cell (default: platform cts_buffer).")] = None,
    buf_list: Annotated[list[str] | None, Field(description="Buffers CTS may use (default: [root_buf]).")] = None,
    sink_clustering: Annotated[bool, Field(description="Cluster nearby sinks under shared buffers.")] = True,
    max_diameter: Annotated[float | None, Field(gt=0, description="Max sink cluster diameter, microns (default: platform cts_cluster_diameter).")] = None,
    repair_clock_nets: Annotated[bool, Field(description="Buffer long clock nets afterwards.")] = True,
    legalize: Annotated[bool, Field(description="Run detailed placement afterwards (recommended).")] = True,
) -> dict[str, Any]:
    """Build the clock tree, switch to propagated clocks, repair clock nets and legalize."""
    plat = active_platform(SESSION)
    root = root_buf or plat.require("cts_buffer", "Pass root_buf, or add 'cts_buffer' to the platform.")
    bufs = buf_list or [root]
    diameter = max_diameter or plat.get("cts_cluster_diameter")
    args = ["clock_tree_synthesis", f"-root_buf {tcl_quote(root)}", f"-buf_list {tcl_list(bufs)}"]
    if sink_clustering:
        args.append("-sink_clustering_enable")
        if diameter:
            args.append(f"-sink_clustering_max_diameter {diameter}")
    lines = [
        "if {[llength [all_clocks]] == 0} { error \"No clocks are defined: read an SDC with create_clock first.\" }",
        "repair_clock_inverters",
        " ".join(args),
        "set_propagated_clock [all_clocks]",
    ]
    if repair_clock_nets:
        lines.append("repair_clock_nets")
    if legalize:
        lines.append(legalize_tcl(pad_sites(plat.get("detail_place_pad"))))
    if plat.get("wire_rc_layer"):
        lines.append("estimate_parasitics -placement")
    lines += ["report_cts", "report_clock_skew -digits 4"]

    def parse(log: str) -> dict[str, Any]:
        out = parse_cts(log)
        out["clock_skew"] = parse_clock_skew(log)
        out["report_cts"] = compact({
            "clock_roots": grab(log, r"Total number of Clock Roots: (\d+)", int),
            "buffers_inserted": grab(log, r"Total number of Buffers Inserted: (\d+)", int),
            "clock_subnets": grab(log, r"Total number of Clock Subnets: (\d+)", int),
            "sinks": grab(log, r"Total number of Sinks: (\d+)", int),
        })
        if legalize:
            out["legalization"] = {**parse_dpl(log), **parse_check_placement(log)}
        return out

    result = await run_step(SESSION, "\n".join(lines), parse, f"clock_tree_synthesis root={root}")
    if plat.get("wire_rc_layer"):
        SESSION.design.parasitics = "placement estimate"
    return result


@mcp.tool(annotations=READ_ONLY)
async def report_clock_tree() -> dict[str, Any]:
    """CTS statistics and setup/hold clock skew of the current design."""
    SESSION.require_design()
    output = await SESSION.run(
        "catch {report_cts}\nputs \"== setup skew\"\nreport_clock_skew -setup -digits 4\n"
        "puts \"== hold skew\"\nreport_clock_skew -hold -digits 4",
        max_chars=50_000_000,
    )
    return {
        "report_cts": compact({
            "clock_roots": grab(output, r"Total number of Clock Roots: (\d+)", int),
            "buffers_inserted": grab(output, r"Total number of Buffers Inserted: (\d+)", int),
            "clock_subnets": grab(output, r"Total number of Clock Subnets: (\d+)", int),
            "sinks": grab(output, r"Total number of Sinks: (\d+)", int),
        }),
        "clock_skew": parse_clock_skew(output),
        "report": tail(output, 8000),
    }


# ---------------------------------------------------------------------------
# Legalization and problem reports
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def legalize(
    padding: Annotated[int | None, Field(ge=0, description="Padding in sites on each side (default: platform detail_place_pad).")] = None,
    max_displacement: Annotated[float | None, Field(gt=0, description="Max cell move in microns.")] = None,
) -> dict[str, Any]:
    """Detailed placement (legalization) after the optimizer inserted or resized cells, then check_placement."""
    pad = padding if padding is not None else pad_sites(active_platform(SESSION).get("detail_place_pad"))
    return await run_step(
        SESSION,
        legalize_tcl(pad, max_displacement),
        lambda log: {**parse_dpl(log), **parse_check_placement(log)},
        f"legalize pad={pad}",
    )


@mcp.tool(annotations=READ_ONLY)
async def report_problems(
    long_wires: Annotated[int, Field(ge=1, le=1000, description="How many of the longest wires to list.")] = 10,
) -> dict[str, Any]:
    """Floating nets/pins, overdriven nets, the longest wires and design-rule (slew/cap/fanout) violators."""
    SESSION.require_design()
    output = await SESSION.run(
        "puts \"== floating\"\nreport_floating_nets -verbose\n"
        "puts \"== overdriven\"\nreport_overdriven_nets -verbose\n"
        f"puts \"== long wires\"\nreport_long_wires {long_wires}\n"
        "puts \"== drv\"\nreport_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3",
        max_chars=50_000_000,
    )
    wires = [
        {"driver": d, "manhattan_um": float(m), "steiner_um": float(s), "delay": float(t)}
        for d, m, s, t in re.findall(r"^(\S+)\s+manhtn\s+([\d.]+)\s+steiner\s+([\d.]+)\s+([-\d.]+)", output, re.M)
    ][:long_wires]  # report_long_wires N prints N+1 rows
    drv_text = output.split("== drv", 1)[1].strip() if "== drv" in output else ""
    return compact({
        "floating_nets": grab(output, r"RSZ-0020\] found (\d+) floating nets", int) or 0,
        "floating_pins": grab(output, r"RSZ-0095\] found (\d+) floating pins", int) or 0,
        "overdriven_nets": grab(output, r"RSZ-0024\] found (\d+) overdriven nets", int) or 0,
        "longest_wires": wires,
        "drv_violators": drv_text or "none",
    })


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def fix_timing() -> str:
    """Full optimization pass on a placed design."""
    return """Optimize the placed design in the OpenROAD optimization session.

1. design_status: note stage, utilization, setup/hold WNS/TNS and DRV counts. If nothing is loaded,
   list_checkpoints and load_checkpoint the latest 'placed' checkpoint.
2. setup_parasitics (platform defaults), then repair_design; report the DRV counts before/after.
3. repair_tie_fanout, then legalize.
4. clock_tree_synthesis (platform CTS buffer); report sinks, buffers and clock skew.
5. repair_timing mode=both; report WNS/TNS before/after and what the Resizer changed.
6. If setup WNS is still negative, try repair_timing mode=setup with phases="TNS" or a small setup_margin,
   and explain the remaining worst path (report_problems for long wires / DRV).
7. save_checkpoint name="cts" with a short note, so the route server can continue.

Finish with a table of the metrics after each step and a verdict."""


@mcp.prompt()
def cts_review() -> str:
    """Review the clock tree."""
    return """Review the clock tree of the design in the OpenROAD optimization session.

1. report_clock_tree: sinks, buffers, clock subnets, setup and hold skew.
2. design_status: hold WNS/TNS (clock skew often drives hold problems).
3. snapshot with show={"routing": false} to see the buffer distribution.

Say whether skew and buffer count are reasonable for the number of sinks, and what to change
(cluster diameter, buffer list, sink clustering) if not."""


@mcp.prompt()
def drv_cleanup() -> str:
    """Remove slew/capacitance/fanout violations."""
    return """Clean up design-rule violations in the OpenROAD optimization session.

1. report_problems: DRV violators, floating/overdriven nets, longest wires.
2. repair_design (try max_wire_length near the longest wires if long wires dominate).
3. legalize, then report_problems again.

Report violations before/after per type, and list anything left with a suggested fix."""


@mcp.prompt()
def power_recovery(percent: str = "100") -> str:
    """Trade positive slack for area/power."""
    return f"""Recover power on the design in the OpenROAD optimization session.

1. design_status: record area and setup/hold slack.
2. recover_power percent={percent}.
3. legalize, then design_status again.

Report area and power before/after and confirm no new setup/hold violations appeared."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
