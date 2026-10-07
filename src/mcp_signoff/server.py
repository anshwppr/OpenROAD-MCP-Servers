"""MCP server for OpenROAD signoff: parasitic extraction (rcx), signoff timing and power (sta),
power-grid connectivity and IR drop (psm), metal density fill (fin), a signoff checklist and
the final output files.

The last of four stage servers (place -> opt -> route -> signoff). It starts from a routed
checkpoint. Technology values (RCX rules, supply nets and voltage, fill rules, filler cells)
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
    messages,
    parse_check_placement,
    parse_clock_min_period,
    parse_clock_skew,
    parse_drv_violators,
    parse_endpoint_report,
    parse_fill,
    parse_ir_report,
    parse_power,
    parse_rcx,
    parse_slack_report,
)
from openroad_common.records import by_kind, first, query
from openroad_common.session import Config, OpenRoadSession
from openroad_common.stage import (
    DESTRUCTIVE,
    MUTATING,
    READ_ONLY,
    STAGE_DRIVER_TCL,
    active_platform,
    design_status,
    platform_setup_tcl,
    record_step,
    register_stage_tools,
    run_step,
    tail,
)
from openroad_common.tcl import parse_records, tcl_file, tcl_list, tcl_quote, to_wsl_path

SIGNOFF_DRIVER_TCL = STAGE_DRIVER_TCL + r"""
# Output directory: $HOME/openroad_mcp/<kind>/<block name>_<tag> unless given.
proc __so_dir {dir kind tag} {
  if {$dir eq ""} {
    set dir [file join $::env(HOME) openroad_mcp $kind "[[__stage_block] getName]_$tag"]
  }
  file mkdir $dir
  __mcp_rec dir [file normalize $dir]
}

proc __so_files {paths} {
  foreach p $paths {
    if {[file exists $p]} { __mcp_rec file $p [file size $p] } else { __mcp_rec file $p -1 }
  }
}

# The worst instance voltages from an analyze_power_grid -voltage_file CSV
# (lowest first for a power net, highest first for a ground net).
proc __so_worst_voltage {path n ground} {
  set f [open $path]
  gets $f
  set rows {}
  while {[gets $f line] >= 0} {
    set cols [split $line ,]
    if {[llength $cols] >= 6} { lappend rows $cols }
  }
  close $f
  set order [expr {$ground ? "-decreasing" : "-increasing"}]
  foreach r [lrange [lsort -real $order -index 5 $rows] 0 [expr {$n - 1}]] {
    __mcp_rec v {*}$r
  }
}

proc __so_count {path pattern} {
  if {![file exists $path]} { return 0 }
  set f [open $path]
  set n [regexp -all -line $pattern [read $f]]
  close $f
  return $n
}

# web_save_report (OpenROAD 26Q2) bundles the web viewer's JavaScript but strips its `import` lines,
# so the 3D viewer's three.js import is lost and the page stops at "THREE is not defined" (blank
# page). It also leaves out the schematic libraries index.html loads. Put both back.
proc __so_fix_web_report {path} {
  set f [open $path r]
  fconfigure $f -encoding utf-8
  set html [read $f]
  close $f
  set fixes {}
  if {[string first "THREE." $html] >= 0 && ![regexp {import \* as THREE} $html]
      && [regexp -indices {import \{[^\}]*\} from 'https://esm\.sh/golden-layout@[^']*';} $html m]} {
    set end [lindex $m 1]
    set html "[string range $html 0 $end]\nimport * as THREE from 'https://esm.sh/three@0.160.0';[string range $html [expr {$end + 1}] end]"
    lappend fixes three.js
  }
  if {[string first "netlistsvg.bundle.js" $html] < 0 && [string first "</head>" $html] >= 0} {
    set libs "<script src=\"https://nturley.github.io/netlistsvg/elk.bundled.js\"></script>\n<script src=\"https://nturley.github.io/netlistsvg/built/netlistsvg.bundle.js\"></script>\n"
    set i [string first "</head>" $html]
    set html "[string range $html 0 [expr {$i - 1}]]$libs[string range $html $i end]"
    lappend fixes netlistsvg
  }
  if {[llength $fixes]} {
    set f [open $path w]
    fconfigure $f -encoding utf-8
    puts -nonewline $f $html
    close $f
  }
  __mcp_rec fixes {*}$fixes
}

proc __so_supply_nets {} {
  foreach net [[__stage_block] getNets] {
    if {[$net isSpecial] && [$net getSigType] in {POWER GROUND}} {
      set wires 0
      foreach sw [$net getSWires] { incr wires [llength [$sw getWires]] }
      __mcp_rec snet [$net getName] [$net getSigType] $wires [llength [$net getBTerms]]
    }
  }
}
"""

CFG = Config.from_env("SIGNOFF", default_timeout=1800)
SESSION = OpenRoadSession(CFG, "signoff", SIGNOFF_DRIVER_TCL)

# Per OpenROAD process: the extracted SPEF in use.
_STATE: dict[str, Any] = {"pid": None, "spef": None}


@asynccontextmanager
async def lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        await SESSION.close()


mcp = MCPServer(
    "openroad-signoff",
    title="OpenROAD Signoff",
    description="Parasitic extraction, signoff timing and power, power-grid and IR-drop analysis, density fill, "
                "a signoff checklist and the final output files.",
    instructions=(
        "Signoff on a persistent OpenROAD session. Load a routed design with load_checkpoint (e.g. 'routed' from "
        "the routing server). Typical order: extract_parasitics -> signoff_timing -> power_analysis -> "
        "check_power_grid -> analyze_ir_drop -> signoff_checklist -> write_outputs (and timing_report_html). "
        "RCX rules, supply nets/voltage and fill rules default from the platform. Times are in the Liberty "
        "time unit, distances in microns."
    ),
    lifespan=lifespan,
)

register_stage_tools(mcp, SESSION, CFG, "signoff")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def so_flags() -> dict[str, Any]:
    """Per-process extraction state; reset whenever OpenROAD was restarted."""
    if _STATE["pid"] != SESSION.pid:
        _STATE.update(pid=SESSION.pid, spef=None)
    return _STATE


def windows_path(wsl_path: str) -> str:
    r"""Where Windows sees a WSL file: /mnt/c/x -> C:\x, /home/x -> \\wsl.localhost\<distro>\home\x."""
    if wsl_path.startswith("/mnt/") and len(wsl_path) > 6 and wsl_path[6] == "/":
        return f"{wsl_path[5].upper()}:\\" + wsl_path[7:].replace("/", "\\")
    return f"\\\\wsl.localhost\\{CFG.wsl_distro}" + wsl_path.replace("/", "\\")


async def out_dir(directory: str | None, kind: str) -> str:
    tag = active_platform(SESSION).name
    records, _ = await query(
        SESSION, f"__so_dir {tcl_quote(to_wsl_path(directory) if directory else '')} {kind} {tcl_quote(tag)}")
    return first(records, "dir")[1]


async def block_name() -> str:
    records, _ = await query(SESSION, "__mcp_rec name [[__stage_block] getName]")
    return first(records, "name")[1]


async def require_routed() -> None:
    SESSION.require_design()
    records, _ = await query(SESSION, "__mcp_rec routed [[__stage_block] designIsRouted 0]")
    if first(records, "routed")[1] != "1":
        raise ToolError("The design is not fully routed. Load a 'routed' checkpoint from the routing server.")


async def supply_nets() -> list[dict[str, Any]]:
    records, _ = await query(SESSION, "__so_supply_nets")
    return [{"net": r[1], "type": r[2], "shapes": int(r[3]), "ports": int(r[4])} for r in by_kind(records, "snet")]


def report_section(log: str, name: str) -> str:
    """Text after a ``== name`` marker line, up to the next marker."""
    marker = f"== {name}"
    return log.split(marker, 1)[1].split("\n== ", 1)[0] if marker in log else ""


def parasitics_note() -> str | None:
    source = SESSION.design.parasitics
    if source and source.startswith("extracted"):
        return None
    return (f"Timing uses {source or 'no wire parasitics'}, not extracted parasitics. "
            "Run extract_parasitics for signoff-accurate numbers.")


async def nets_for(net: str | None) -> list[str]:
    plat = active_platform(SESSION)
    if net:
        return [net]
    return [n for n in (plat.get("power_net"), plat.get("ground_net")) if n]


# ---------------------------------------------------------------------------
# Extraction and timing
# ---------------------------------------------------------------------------


@mcp.tool(annotations=MUTATING)
async def extract_parasitics(
    rules_file: Annotated[str | None, Field(description="RCX rules file (default: platform rcx_rules_file).")] = None,
    spef_file: Annotated[str | None, Field(description="SPEF to write (default: $HOME/openroad_mcp/signoff/<design>_<platform>/<design>.spef).")] = None,
    coupling_threshold: Annotated[float | None, Field(gt=0, description="Coupling caps below this (fF) are grounded (default 0.1).")] = None,
    max_res: Annotated[float | None, Field(gt=0, description="Merge resistors up to this many ohms (default 50).")] = None,
) -> dict[str, Any]:
    """Extract RC parasitics from the routed wires (OpenRCX) into a SPEF, then continue on the unmodified
    design with the SPEF loaded, so timing, power and checks use the extracted parasitics.

    OpenRCX re-encodes the routed wires while it extracts (a design saved afterwards shows false DRC
    shorts), so the design is saved first and reloaded after extraction; the session restarts once."""
    await require_routed()
    plat = active_platform(SESSION)
    rules = rules_file or plat.get("rcx_rules_file")
    if not rules:
        raise ToolError(f"Platform '{plat.name}' has no rcx_rules_file. Pass rules_file, or use estimate_parasitics "
                        "source=global_routing for approximate parasitics.")
    folder = await out_dir(None, "signoff")
    name = await block_name()
    spef = to_wsl_path(spef_file) if spef_file else f"{folder}/{name}.spef"
    clean_odb, clean_sdc = f"{folder}/{name}_pre_extraction.odb", f"{folder}/{name}_pre_extraction.sdc"
    # tcl_file checks the rules file first: a failed read leaves OpenRCX unusable for the session.
    args = ["extract_parasitics", f"-ext_model_file {tcl_file(rules)}"]
    if coupling_threshold is not None:
        args.append(f"-coupling_threshold {coupling_threshold}")
    if max_res is not None:
        args.append(f"-max_res {max_res}")
    script = "\n".join([
        f"write_db {tcl_quote(clean_odb)}",
        f"write_sdc {tcl_quote(clean_sdc)}",
        "define_process_corner -ext_model_index 0 X",
        " ".join(args),
        f"write_spef {tcl_quote(spef)}",
    ])
    result = await run_step(SESSION, script, parse_rcx, "extract_parasitics")
    status_before = result["before"]

    # Back to the unmodified design, now with the extracted parasitics.
    design = SESSION.design
    await SESSION.restart()
    SESSION.design = design
    lines = [f"read_liberty {tcl_file(lib)}" for lib in design.files.get("liberty", [])]
    lines += [f"read_db {tcl_quote(clean_odb)}", f"read_sdc {tcl_quote(clean_sdc)}", platform_setup_tcl(plat),
              f"read_spef {tcl_quote(spef)}"]
    output = await SESSION.run("\n".join(lines), max_chars=50_000_000)
    so_flags()["spef"] = spef
    design.parasitics = "extracted (OpenRCX SPEF)"
    after = await design_status(SESSION)
    return {
        "summary": {**result["summary"], "rules_file": rules, "spef": spef, "spef_windows": windows_path(spef)},
        "before": status_before,
        "after": {k: after[k] for k in ("stage", "setup", "hold", "drv", "parasitics") if k in after},
        "log": result["log"] + ("\n" + tail(output, 2000) if messages(output) else ""),
    }


@mcp.tool(annotations=READ_ONLY)
async def signoff_timing(
    paths: Annotated[int, Field(ge=1, le=100, description="Worst endpoints listed per check.")] = 5,
    digits: Annotated[int, Field(ge=1, le=6, description="Digits in the reports.")] = 3,
) -> dict[str, Any]:
    """Final timing: setup/hold WNS and TNS, worst endpoints, slew/cap/fanout violators, clock skew,
    minimum clock period (fmax) and total power, with the parasitics source they are based on."""
    SESSION.require_design()
    output = await SESSION.run(
        f"report_worst_slack -max -digits {digits}\nreport_worst_slack -min -digits {digits}\n"
        f"report_tns -max -digits {digits}\nreport_tns -min -digits {digits}\n"
        f"report_wns -max -digits {digits}\nreport_wns -min -digits {digits}\n"
        "puts \"== endpoints\"\n"
        f"report_checks -path_delay max -group_path_count {paths} -format end -digits {digits}\n"
        f"report_checks -path_delay min -group_path_count {paths} -format end -digits {digits}\n"
        "puts \"== drv\"\n"
        f"report_check_types -max_slew -max_capacitance -max_fanout -violators -digits {digits}\n"
        "puts \"== skew\"\n"
        f"report_clock_skew -setup -digits {digits}\nreport_clock_skew -hold -digits {digits}\n"
        "puts \"== period\"\nreport_clock_min_period\n"
        "puts \"== power\"\nreport_power",
        max_chars=50_000_000,
    )
    _, log = parse_records(output)
    sections = {name: report_section(log, name) for name in ("endpoints", "drv", "skew", "period", "power")}
    slacks = parse_slack_report(log.split("== endpoints", 1)[0])
    drv = parse_drv_violators(sections["drv"])
    return compact({
        "setup": {**slacks["setup"], "status": "MET" if slacks["setup"].get("worst_slack", 0) >= 0 else "VIOLATED"},
        "hold": {**slacks["hold"], "status": "MET" if slacks["hold"].get("worst_slack", 0) >= 0 else "VIOLATED"},
        "worst_endpoints": parse_endpoint_report(sections["endpoints"]),
        "drv_violations": {k: len(v) for k, v in drv.items()} or {"max_slew": 0, "max_cap": 0, "max_fanout": 0},
        "drv_violators": drv,
        "clock_skew": parse_clock_skew(sections["skew"]),
        "clock_min_period": parse_clock_min_period(sections["period"]),
        "power": parse_power(sections["power"]),
        "parasitics": SESSION.design.parasitics,
        "note": parasitics_note(),
    })


@mcp.tool(annotations=MUTATING)
async def power_analysis(
    input_activity: Annotated[float | None, Field(ge=0, le=1, description="Switching activity of all inputs (toggles per clock).")] = None,
    vcd_file: Annotated[str | None, Field(description="Use switching activity from a VCD file (WSL path).")] = None,
    vcd_scope: Annotated[str | None, Field(description="Hierarchy scope of the design inside the VCD.")] = None,
    digits: Annotated[int, Field(ge=1, le=6, description="Digits in the report.")] = 3,
) -> dict[str, Any]:
    """Power by group (sequential, combinational, clock, macro, pad): internal, switching, leakage and total,
    optionally with a global input activity or VCD activity."""
    SESSION.require_design()
    lines = []
    if input_activity is not None:
        lines.append(f"set_power_activity -input -activity {input_activity}")
    if vcd_file:
        lines.append(f"read_vcd {('-scope ' + tcl_quote(vcd_scope) + ' ') if vcd_scope else ''}{tcl_file(vcd_file)}")
    lines.append(f"report_power -digits {digits}")
    output = await SESSION.run("\n".join(lines), max_chars=50_000_000)
    if input_activity is not None or vcd_file:
        record_step(SESSION, "power_analysis (activity set)")
    return compact({**parse_power(output), "activity": "vcd" if vcd_file else (input_activity if input_activity is not None else "default"),
                    "parasitics": SESSION.design.parasitics, "note": parasitics_note(),
                    "warnings": messages(output)})


# ---------------------------------------------------------------------------
# Power grid
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def check_power_grid(
    net: Annotated[str | None, Field(description="Supply net (default: platform power_net and ground_net).")] = None,
    require_terminals: Annotated[bool, Field(description="Also require top-level supply pins (ports) on the net.")] = False,
) -> dict[str, Any]:
    """Power-grid connectivity: every stripe, via and cell supply pin connected to the grid."""
    SESSION.require_design()
    results = []
    for name in await nets_for(net):
        flag = "" if require_terminals else " -dont_require_terminals"
        output = await SESSION.run(
            f"if {{[catch {{check_power_grid -net {tcl_quote(name)}{flag}}} __e]}} {{ puts \"== failed $__e\" }}"
        )
        _, log = parse_records(output)
        results.append(compact({
            "net": name,
            "connected": "PSM-0040" in log and "== failed" not in log,
            "warnings": messages(log),
            "errors": messages(log, level="ERROR"),
        }))
    return {"nets": results, "all_connected": all(r["connected"] for r in results), "supply_nets": await supply_nets()}


@mcp.tool(annotations=MUTATING)
async def analyze_ir_drop(
    net: Annotated[str | None, Field(description="Supply net (default: platform power_net and ground_net).")] = None,
    voltage: Annotated[float | None, Field(gt=0, description="Supply voltage of the power net (default: platform supply_voltage, else the Liberty voltage).")] = None,
    vsrc_file: Annotated[str | None, Field(description="Voltage source (bump/pad) locations file.")] = None,
    source_type: Annotated[Literal["FULL", "BUMPS", "STRAPS"] | None, Field(description="Where sources are assumed when there is no vsrc file.")] = None,
    enable_em: Annotated[bool, Field(description="Also run electromigration (current) analysis.")] = False,
    worst_instances: Annotated[int, Field(ge=0, le=1000, description="List this many instances with the worst supply voltage.")] = 10,
    directory: Annotated[str | None, Field(description="Where the voltage/EM files go (default: $HOME/openroad_mcp/signoff/<design>_<platform>).")] = None,
) -> dict[str, Any]:
    """Static IR-drop analysis of the power grid (PDNSim): worst and average IR drop, % of supply, the worst
    instances, and optionally EM currents."""
    await require_routed()
    plat = active_platform(SESSION)
    folder = await out_dir(directory, "signoff")
    supply = voltage or plat.get("supply_voltage")
    reports = []
    for name in await nets_for(net):
        ground = name == plat.get("ground_net")
        vfile = f"{folder}/{name}_voltage.csv"
        lines = []
        if supply and not ground:
            lines.append(f"set_pdnsim_net_voltage -net {tcl_quote(name)} -voltage {supply}")
        args = ["analyze_power_grid", f"-net {tcl_quote(name)}", f"-voltage_file {tcl_quote(vfile)}"]
        if vsrc_file:
            args.append(f"-vsrc {tcl_file(vsrc_file)}")
        if source_type:
            args.append(f"-source_type {source_type}")
        if enable_em:
            args += ["-enable_em", f"-em_outfile {tcl_quote(f'{folder}/{name}_em.csv')}"]
        lines.append(" ".join(args))
        if worst_instances:
            lines.append(f"__so_worst_voltage {tcl_quote(vfile)} {worst_instances} {int(ground)}")
        records, log = await query(SESSION, "\n".join(lines))
        report = (parse_ir_report(log) or [{}])[-1]
        report["worst_instances"] = [
            {"instance": r[1], "pin": r[2], "layer": r[3], "x_um": float(r[4]), "y_um": float(r[5]), "voltage_v": float(r[6])}
            for r in by_kind(records, "v")
        ]
        report["voltage_file"] = vfile
        if enable_em:
            report["em_file"] = f"{folder}/{name}_em.csv"
        report["warnings"] = messages(log)
        reports.append(compact(report))
    record_step(SESSION, "analyze_ir_drop")
    worst = max((r.get("drop_pct", 0) for r in reports), default=None)
    return {"reports": reports, "worst_drop_pct": worst}


# ---------------------------------------------------------------------------
# Fill, checklist, outputs
# ---------------------------------------------------------------------------


@mcp.tool(annotations=DESTRUCTIVE)
async def density_fill(
    rules_file: Annotated[str | None, Field(description="Fill rules JSON (default: platform fill_rules).")] = None,
    area: Annotated[list[float] | None, Field(description="Fill only [x1, y1, x2, y2] microns (default: the core).")] = None,
) -> dict[str, Any]:
    """Insert metal fill shapes to meet density rules (adds shapes to the design; save a checkpoint first
    to keep an unfilled copy)."""
    await require_routed()
    plat = active_platform(SESSION)
    rules = rules_file or plat.get("fill_rules")
    if not rules:
        raise ToolError(f"Platform '{plat.name}' has no fill_rules. Pass rules_file (a density fill JSON for this "
                        "technology).")
    script = f"density_fill -rules {tcl_file(rules)}"
    if area is not None:
        if len(area) != 4:
            raise ToolError("area must be [x1, y1, x2, y2] in microns.")
        script += f" -area {tcl_list(area)}"
    result = await run_step(SESSION, script, parse_fill, "density_fill", before=False)
    result["summary"]["rules_file"] = rules
    return result


@mcp.tool(annotations=MUTATING)
async def signoff_checklist(
    max_ir_drop_pct: Annotated[float, Field(gt=0, le=100, description="IR-drop limit, % of supply.")] = 5.0,
    run_ir_drop: Annotated[bool, Field(description="Run IR-drop analysis as part of the checklist.")] = True,
) -> dict[str, Any]:
    """PASS / WARN / FAIL for every signoff item: routed, DRC, antennas, placement, floating nets, setup,
    hold, slew/cap/fanout, extracted parasitics, power-grid connectivity and IR drop. Returns an overall verdict."""
    SESSION.require_design()
    folder = await out_dir(None, "signoff")
    drc_file = f"{folder}/checklist_drc.rpt"
    output = await SESSION.run(
        "__mcp_rec routed [[__stage_block] designIsRouted 0]\n"
        f"if {{[[__stage_block] designIsRouted 0]}} {{ drt::check_drc -output_file {tcl_quote(drc_file)} }}\n"
        f"__mcp_rec drc [__so_count {tcl_quote(drc_file)} {{^violation type:}}]\n"
        "puts \"== antennas\"\nif {[[__stage_block] designIsRouted 0]} { check_antennas }\n"
        "puts \"== placement\"\n"
        "if {[catch {check_placement -verbose} __err]} { puts \"CHECK_PLACEMENT_FAILED: $__err\" }\n"
        "puts \"== floating\"\nreport_floating_nets -verbose\n"
        "puts \"== timing\"\nreport_worst_slack -max -digits 4\nreport_worst_slack -min -digits 4\n"
        "report_tns -max -digits 4\nreport_tns -min -digits 4\n"
        "puts \"== drv\"\nreport_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3\n"
        "puts \"== end\"",
        max_chars=50_000_000,
    )
    records, log = parse_records(output)

    def section(name: str) -> str:
        return report_section(log, name)

    items: list[dict[str, Any]] = []

    def item(name: str, result: str, detail: Any) -> None:
        items.append({"item": name, "result": result, "detail": detail})

    routed = first(records, "routed")[1] == "1"
    item("routed", "PASS" if routed else "FAIL", "all nets routed" if routed else "the design is not fully routed")
    drc = int(first(records, "drc")[1])
    item("drc", "PASS" if routed and drc == 0 else "FAIL", f"{drc} violations (fresh check)" if routed else "not routed")
    ant = section("antennas")
    ant_nets = re.findall(r"ANT-0002\] Found (\d+) net violations", ant)
    item("antennas", "PASS" if ant_nets and int(ant_nets[-1]) == 0 else "FAIL",
         f"{ant_nets[-1]} net violations" if ant_nets else "not checked (not routed)")
    placement = parse_check_placement(section("placement"))
    item("placement", "PASS" if placement["passed"] else "FAIL", placement["failures"] or "legal")
    floating = re.findall(r"RSZ-0020\] found (\d+) floating nets", section("floating"))
    n_float = int(floating[-1]) if floating else 0
    item("floating_nets", "PASS" if n_float == 0 else "WARN", f"{n_float} floating nets")
    slacks = parse_slack_report(section("timing"))
    for check in ("setup", "hold"):
        ws = slacks[check].get("worst_slack")
        ok = isinstance(ws, float) and ws >= 0
        item(check, "PASS" if ok else ("FAIL" if isinstance(ws, float) else "WARN"),
             {"worst_slack": ws, "tns": slacks[check].get("tns")} if ws is not None else "no constrained paths")
    drv = parse_drv_violators(section("drv"))
    counts = {k: len(drv.get(k, [])) for k in ("max_slew", "max_cap", "max_fanout")}
    item("slew_cap_fanout", "PASS" if not any(counts.values()) else "FAIL", counts)
    note = parasitics_note()
    item("extracted_parasitics", "PASS" if note is None else "WARN", SESSION.design.parasitics or "none")

    grid = await check_power_grid()
    for r in grid["nets"]:
        item(f"power_grid_{r['net']}", "PASS" if r["connected"] else "FAIL",
             "connected" if r["connected"] else (r.get("errors") or r.get("warnings")))
    if run_ir_drop and routed and grid["nets"]:
        ir = await analyze_ir_drop(worst_instances=0)
        for r in ir["reports"]:
            pct = r.get("drop_pct")
            item(f"ir_drop_{r.get('net')}", "PASS" if pct is not None and pct <= max_ir_drop_pct else "FAIL",
                 f"{pct}% (limit {max_ir_drop_pct}%)")
    verdict = "FAIL" if any(i["result"] == "FAIL" for i in items) else (
        "WARN" if any(i["result"] == "WARN" for i in items) else "PASS")
    record_step(SESSION, f"signoff_checklist {verdict}")
    return {"verdict": verdict, "items": items,
            "failed": [i["item"] for i in items if i["result"] == "FAIL"],
            "warnings": [i["item"] for i in items if i["result"] == "WARN"]}


@mcp.tool(annotations=MUTATING)
async def write_outputs(
    directory: Annotated[str | None, Field(description="Output directory (default: $HOME/openroad_mcp/outputs/<design>_<platform> in WSL).")] = None,
    formats: Annotated[list[Literal["odb", "def", "verilog", "sdc", "spef"]], Field(description="Files to write.")] = ["odb", "def", "verilog", "sdc", "spef"],
    remove_fillers_from_verilog: Annotated[bool, Field(description="Leave the platform's filler cells out of the Verilog.")] = True,
    include_power_pins: Annotated[bool, Field(description="Write supply connections into the Verilog (for LVS).")] = False,
) -> dict[str, Any]:
    """Write the final design files: OpenROAD database, DEF, Verilog netlist, SDC and SPEF. GDS needs
    KLayout or Magic and is not produced here."""
    SESSION.require_design()
    plat = active_platform(SESSION)
    folder = await out_dir(directory, "outputs")
    if "spef" in formats and not so_flags()["spef"]:
        raise ToolError("No extracted parasitics in this session for the SPEF. Run extract_parasitics first, or "
                        "leave 'spef' out of formats.")
    records, _ = await query(SESSION, "__mcp_rec name [[__stage_block] getName]")
    base = f"{folder}/{first(records, 'name')[1]}"
    paths, lines = {}, []
    for fmt in formats:
        if fmt == "odb":
            paths[fmt] = f"{base}.odb"
            lines.append(f"write_db {tcl_quote(paths[fmt])}")
        elif fmt == "def":
            paths[fmt] = f"{base}.def"
            lines.append(f"write_def {tcl_quote(paths[fmt])}")
        elif fmt == "verilog":
            paths[fmt] = f"{base}.v"
            args = ["write_verilog"]
            if remove_fillers_from_verilog and plat.get("filler_cells"):
                args.append(f"-remove_cells {tcl_quote(plat.get('filler_cells'))}")
            if include_power_pins:
                args.append("-include_pwr_gnd")
            lines.append(" ".join(args + [tcl_quote(paths[fmt])]))
        elif fmt == "sdc":
            paths[fmt] = f"{base}.sdc"
            lines.append(f"write_sdc -no_timestamp {tcl_quote(paths[fmt])}")
        elif fmt == "spef":
            # The session was reloaded after extraction (see extract_parasitics), so copy its SPEF.
            paths[fmt] = f"{base}.spef"
            if so_flags()["spef"] != paths[fmt]:
                lines.append(f"file copy -force {tcl_quote(so_flags()['spef'])} {tcl_quote(paths[fmt])}")
    lines.append(f"__so_files {tcl_list(list(paths.values()))}")
    records, log = await query(SESSION, "\n".join(lines))
    sizes = {r[1]: int(r[2]) for r in by_kind(records, "file")}
    record_step(SESSION, f"write_outputs {', '.join(formats)}")
    return {
        "directory": folder,
        "directory_windows": windows_path(folder),
        "files": {fmt: {"path": p, "bytes": sizes.get(p, -1), "windows_path": windows_path(p)} for fmt, p in paths.items()},
        "warnings": messages(log),
    }


@mcp.tool(annotations=MUTATING)
async def timing_report_html(
    path: Annotated[str | None, Field(description="HTML file (default: $HOME/openroad_mcp/signoff/<design>_<platform>/timing_report.html).")] = None,
    setup_paths: Annotated[int, Field(ge=0, le=1000, description="Worst setup paths drawn on the layout.")] = 10,
    hold_paths: Annotated[int, Field(ge=0, le=1000, description="Worst hold paths drawn on the layout.")] = 10,
) -> dict[str, Any]:
    """An interactive HTML timing report (layout with the worst paths, timing, schematic and DRC panels) to
    open in a browser. The layout data is inside the file; the page loads its JavaScript libraries (Leaflet,
    Golden Layout, three.js) from public CDNs, so the browser needs internet access."""
    SESSION.require_design()
    target = to_wsl_path(path) if path else f"{await out_dir(None, 'signoff')}/timing_report.html"
    records, log = await query(
        SESSION,
        f"web_save_report -setup_paths {setup_paths} -hold_paths {hold_paths} {tcl_quote(target)}\n"
        f"if {{[file exists {tcl_quote(target)}]}} {{ __so_fix_web_report {tcl_quote(target)} }}\n"
        f"__so_files {tcl_list([target])}",
    )
    size = int(first(records, "file")[2])
    if size <= 0:
        raise ToolError(f"The report was not written: {tail(log, 2000)}")
    fixes = first(records, "fixes", required=False)
    return compact({"path": target, "windows_path": windows_path(target), "bytes": size,
                    "patched": [f for f in (fixes or [])[1:] if f] or None,
                    "needs_internet": "Open it in a browser with internet access (it loads Leaflet, Golden Layout "
                                      "and three.js from CDNs).",
                    "note": parasitics_note(), "warnings": messages(log)})


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def signoff_review() -> str:
    """Full signoff of a routed design."""
    return """Sign off the routed design in the OpenROAD signoff session.

1. design_status. If nothing is loaded, list_checkpoints and load_checkpoint the latest 'routed' checkpoint.
2. extract_parasitics (platform RCX rules); report nets and coupling caps extracted.
3. signoff_timing: setup/hold WNS/TNS, worst endpoints, slew/cap/fanout violators, clock skew, fmax, power.
4. power_analysis, check_power_grid and analyze_ir_drop.
5. signoff_checklist; explain every FAIL/WARN and what would fix it (and in which server: place, opt or route).
6. timing_report_html, then write_outputs.

Finish with a signoff table (item, value, PASS/WARN/FAIL), the output file paths and a clear verdict."""


@mcp.prompt()
def ir_drop_review(limit_pct: str = "5") -> str:
    """Review power-grid integrity and IR drop."""
    return f"""Review the power grid of the design in the OpenROAD signoff session (limit {limit_pct}% IR drop).

1. check_power_grid: is every supply net fully connected? List any unconnected shapes.
2. extract_parasitics if not done (switching power depends on the wire caps).
3. analyze_ir_drop enable_em=true: worst/average IR drop and % per net, the worst instances, EM currents.
4. snapshot of the area around the worst instances.

Say whether the grid passes the {limit_pct}% limit and, if not, what to change in the PDN script
(strap pitch/width, more straps, more vias) or in the placement (spread hot spots)."""


@mcp.prompt()
def tapeout_readiness() -> str:
    """Decide whether the design is ready to hand off."""
    return """Decide whether the design in the OpenROAD signoff session is ready for tapeout handoff.

1. signoff_checklist (it runs DRC, antennas, placement, timing, DRV, power grid and IR drop).
2. If parasitics are not extracted, extract_parasitics and run the checklist again.
3. density_fill if the platform has fill rules (it reports the fill shapes per layer).
4. write_outputs and timing_report_html.

Answer READY or NOT READY. For NOT READY, list each blocking item with its value, the likely cause and the
server/tool to fix it. Mention that GDS streaming (KLayout/Magic) is outside OpenROAD."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
