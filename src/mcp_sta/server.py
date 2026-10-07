"""MCP server exposing OpenROAD static timing analysis (OpenSTA) tools.

The server runs on Windows and drives one persistent ``openroad`` process inside
WSL (see ``openroad_common.session``). The design is loaded once and every later
tool call is a fast query against the live timing graph.
"""

from __future__ import annotations

import re
import subprocess
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from openroad_common import (
    Config,
    OpenRoadError,
    OpenRoadSession,
    tcl_file,
    tcl_list,
    tcl_quote,
)
from openroad_common.parsing import parse_slack_report

CFG = Config.from_env("STA")

StaError = OpenRoadError

# STA-only helper procs added to the shared Tcl driver.
STA_DRIVER_TCL = r"""
# Resolve a name/pattern to pins, then ports, then instances, then clocks.
proc __mcp_objs {pattern} {
  foreach cmd {get_pins get_ports get_cells get_clocks} {
    set objs [$cmd -quiet $pattern]
    if {[llength $objs] > 0} {
      return $objs
    }
  }
  error "No pin, port, instance or clock matches '$pattern'"
}

proc __mcp_names {objs} {
  set names {}
  foreach obj $objs {
    lappend names [get_full_name $obj]
  }
  return $names
}
"""

SESSION = OpenRoadSession(CFG, "sta", STA_DRIVER_TCL)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def tcl_objs(patterns: list[str]) -> str:
    """Tcl expression resolving names/patterns to pins, ports, instances or clocks."""
    return "[concat " + " ".join(f"[__mcp_objs {tcl_quote(p)}]" for p in patterns) + "]"


def _status(slacks: dict[str, float | str]) -> str:
    worst = slacks.get("worst_slack")
    if isinstance(worst, float):
        return "VIOLATED" if worst < 0 else "MET"
    return "UNCONSTRAINED"


def require_design() -> None:
    SESSION.require_design()


DESIGN_STATS_TCL = """
puts "== design statistics"
puts "instances: [llength [get_cells -hierarchical *]]"
puts "nets:      [llength [get_nets -hierarchical *]]"
puts "ports:     [llength [get_ports *]]"
puts "clocks:    [__mcp_names [all_clocks]]"
"""


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
    "openroad-sta",
    title="OpenROAD STA",
    description="Static timing analysis with OpenROAD/OpenSTA running in WSL.",
    instructions=(
        "Static timing analysis on a persistent OpenROAD session. Start with load_design "
        "(absolute Windows paths are converted to WSL paths automatically). Then use "
        "timing_summary for WNS/TNS, list_violations and report_timing for paths, "
        "check_design_rules for slew/cap/fanout, and check_setup for constraint problems. "
        "Times and capacitances are in the units of the first Liberty library read."
    ),
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(read_only_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)

PathList = Annotated[list[str], Field(description="Absolute file paths (Windows or WSL).")]
FilePath = Annotated[str, Field(description="Absolute file path (Windows or WSL).")]


# ----------------------------- session & loading ---------------------------


@mcp.tool(annotations=MUTATING)
async def load_design(
    liberty_files: PathList,
    lef_files: Annotated[
        list[str],
        Field(description="LEF files, technology LEF first. Required unless db_file is given."),
    ] = [],
    verilog_files: Annotated[list[str], Field(description="Gate-level Verilog netlist files.")] = [],
    top_module: Annotated[str | None, Field(description="Top module name (with verilog_files).")] = None,
    def_file: Annotated[str | None, Field(description="DEF file to use as the netlist.")] = None,
    db_file: Annotated[str | None, Field(description="OpenROAD .odb database to use as the netlist.")] = None,
    sdc_file: Annotated[str | None, Field(description="SDC constraints file.")] = None,
    spef_file: Annotated[str | None, Field(description="SPEF parasitics file.")] = None,
) -> str:
    """Load a design into a fresh OpenROAD session.

    Give exactly one netlist source: verilog_files + top_module, def_file, or db_file.
    Reads LEF -> Liberty -> netlist -> SDC -> SPEF and returns design statistics.
    Any previously loaded design is discarded.
    """
    sources = sum([bool(verilog_files), def_file is not None, db_file is not None])
    if sources != 1:
        raise ToolError("Give exactly one netlist source: verilog_files (+ top_module), def_file, or db_file.")
    if verilog_files and not top_module:
        raise ToolError("top_module is required with verilog_files.")
    if not liberty_files:
        raise ToolError("At least one Liberty file is required for timing.")
    if not lef_files and db_file is None:
        raise ToolError("OpenROAD needs LEF files (technology LEF first) to link a Verilog or DEF netlist.")

    def step(command: str, path: str) -> list[str]:
        return [f"puts {tcl_quote(f'== {command} {path}')}", f"{command} {tcl_file(path)}"]

    netlist: list[str] = []
    for lef in lef_files:
        netlist += step("read_lef", lef)
    for lib in liberty_files:
        netlist += step("read_liberty", lib)
    if db_file:
        netlist += step("read_db", db_file)
    elif def_file:
        netlist += step("read_def", def_file)
    else:
        for v in verilog_files:
            netlist += step("read_verilog", v)
        netlist += [f"puts {tcl_quote('== link_design ' + str(top_module))}", f"link_design {tcl_quote(top_module)}"]

    await SESSION.restart()
    log = [await SESSION.run("\n".join(netlist))]

    # The netlist is usable from here on, even if the constraints below fail.
    design = SESSION.design
    design.loaded = True
    design.top_module = top_module
    design.add("lef", *lef_files)
    design.add("liberty", *liberty_files)
    design.add("verilog", *verilog_files)
    for kind, path in (("def", def_file), ("db", db_file)):
        if path:
            design.add(kind, path)

    for kind, path in (("sdc", sdc_file), ("spef", spef_file)):
        if not path:
            continue
        try:
            log.append(await SESSION.run("\n".join(step(f"read_{kind}", path))))
        except StaError as exc:
            raise StaError(
                f"The netlist is loaded, but read_{kind} failed. Fix the file and call read_{kind} "
                f"(no need to reload the design).\n\n{exc}\n\n--- load log ---\n" + "\n".join(log)
            ) from None
        design.add(kind, path)
        if kind == "spef":
            design.parasitics = "spef"

    log.append(await SESSION.run(DESIGN_STATS_TCL))
    return "\n".join(part for part in log if part)


@mcp.tool(annotations=MUTATING)
async def read_sdc(sdc_file: FilePath) -> str:
    """Read an additional SDC constraints file into the loaded design."""
    require_design()
    output = await SESSION.run(f"read_sdc {tcl_file(sdc_file)}\n" + 'puts "clocks: [__mcp_names [all_clocks]]"')
    SESSION.design.add("sdc", sdc_file)
    return output


@mcp.tool(annotations=MUTATING)
async def read_spef(spef_file: FilePath) -> str:
    """Read extracted parasitics (SPEF). Delays are recalculated on the next report."""
    require_design()
    output = await SESSION.run(f"read_spef {tcl_file(spef_file)}")
    SESSION.design.add("spef", spef_file)
    SESSION.design.parasitics = "spef"
    return output or f"Read SPEF {spef_file}."


@mcp.tool(annotations=MUTATING)
async def set_wire_rc(
    layer: Annotated[str | None, Field(description="Routing layer to take RC from, e.g. metal3.")] = None,
    resistance: Annotated[float | None, Field(description="Wire resistance per unit length.")] = None,
    capacitance: Annotated[float | None, Field(description="Wire capacitance per unit length.")] = None,
    target: Annotated[
        Literal["both", "signal", "clock"], Field(description="Apply to signal nets, clock nets, or both.")
    ] = "both",
) -> str:
    """Set the per-unit-length wire RC used by estimate_parasitics.

    Give either a layer, or both resistance and capacitance (per micron, in the
    design's Liberty resistance/capacitance units).
    """
    require_design()
    args = ["set_wire_rc"]
    if target != "both":
        args.append(f"-{target}")
    if layer:
        args += ["-layer", tcl_quote(layer)]
    elif resistance is not None and capacitance is not None:
        args += ["-resistance", tcl_quote(resistance), "-capacitance", tcl_quote(capacitance)]
    else:
        raise ToolError("Give either layer, or both resistance and capacitance.")
    output = await SESSION.run(" ".join(args))
    return output or f"Wire RC set ({target})."


@mcp.tool(annotations=MUTATING)
async def estimate_parasitics(
    source: Annotated[
        Literal["placement", "global_routing"],
        Field(description="Estimate from placement (Steiner trees) or from global routes."),
    ] = "placement",
) -> str:
    """Estimate wire parasitics for a placed (or globally routed) design.

    Needs a placed DEF/ODB and a prior set_wire_rc (or layer RC in the ODB).
    Timing reports afterwards include the estimated wire delays.
    """
    require_design()
    output = await SESSION.run(f"estimate_parasitics -{source}")
    SESSION.design.parasitics = f"estimated ({source})"
    return output or f"Parasitics estimated from {source}."


@mcp.tool(annotations=READ_ONLY)
async def session_status(
    check_connection: Annotated[
        bool, Field(description="Start OpenROAD if needed and report its version.")
    ] = False,
) -> dict[str, Any]:
    """Report the OpenROAD session state, loaded files and configuration."""
    version = None
    if check_connection:
        version = await SESSION.run(
            'if {[catch {puts "openroad [ord::openroad_version]"}]} {puts "openroad (version unknown)"}\n'
            'if {[catch {puts "opensta [sta::version]"}]} {}'
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
        "top_module": design.top_module,
        "parasitics": design.parasitics,
        "files": design.files,
        "raw_tcl_enabled": CFG.allow_raw_tcl,
    }


@mcp.tool(annotations=MUTATING)
async def reset_session() -> str:
    """Stop the OpenROAD process and forget the loaded design."""
    await SESSION.close()
    return "OpenROAD session stopped. Call load_design to start again."


# --------------------------------- analysis --------------------------------


SUMMARY_TCL = """
report_worst_slack -max -digits 4
report_tns -max -digits 4
report_wns -max -digits 4
report_worst_slack -min -digits 4
report_tns -min -digits 4
report_wns -min -digits 4
puts "clocks: [__mcp_names [all_clocks]]"
"""


async def _timing_summary() -> dict[str, Any]:
    report = await SESSION.run(SUMMARY_TCL)
    slacks = parse_slack_report(report)
    clocks = re.search(r"^clocks: (.*)$", report, re.MULTILINE)
    return {
        "setup": {**slacks["setup"], "status": _status(slacks["setup"])},
        "hold": {**slacks["hold"], "status": _status(slacks["hold"])},
        "clocks": clocks.group(1).split() if clocks else [],
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY)
async def timing_summary() -> dict[str, Any]:
    """Worst slack, TNS and WNS for setup (max) and hold (min), plus the clock list.

    Negative slack means a violation. Status is MET, VIOLATED or UNCONSTRAINED.
    """
    require_design()
    return await _timing_summary()


@mcp.tool(annotations=READ_ONLY)
async def report_timing(
    path_delay: Annotated[
        Literal["max", "min", "min_max"], Field(description="max = setup, min = hold, min_max = both.")
    ] = "max",
    from_points: Annotated[
        list[str] | None, Field(description="Startpoints: pins, ports, instances or clocks (patterns allowed).")
    ] = None,
    through_points: Annotated[
        list[str] | None, Field(description="Each entry becomes a separate -through, in order.")
    ] = None,
    to_points: Annotated[list[str] | None, Field(description="Endpoints: pins, ports, instances or clocks.")] = None,
    group_path_count: Annotated[int, Field(ge=1, le=1000, description="Paths to report per path group.")] = 1,
    endpoint_path_count: Annotated[int, Field(ge=1, le=100, description="Paths to report per endpoint.")] = 1,
    format: Annotated[
        Literal["full", "full_clock", "full_clock_expanded", "short", "end", "summary", "slack_only", "json"],
        Field(description="Report format; json gives machine-readable paths."),
    ] = "full_clock_expanded",
    fields: Annotated[
        list[Literal["capacitance", "slew", "fanout", "input_pin", "net", "src_attr"]],
        Field(description="Extra columns per path stage."),
    ] = [],
    digits: Annotated[int, Field(ge=1, le=10)] = 4,
    unconstrained: Annotated[bool, Field(description="Also report unconstrained paths.")] = False,
    slack_max: Annotated[float | None, Field(description="Only paths with slack <= this value.")] = None,
) -> str:
    """Detailed timing path report (OpenSTA report_checks)."""
    require_design()
    args = [
        "report_checks",
        f"-path_delay {path_delay}",
        f"-format {format}",
        f"-digits {digits}",
        f"-group_path_count {group_path_count}",
        f"-endpoint_path_count {endpoint_path_count}",
    ]
    if from_points:
        args.append(f"-from {tcl_objs(from_points)}")
    for point in through_points or []:
        args.append(f"-through {tcl_objs([point])}")
    if to_points:
        args.append(f"-to {tcl_objs(to_points)}")
    if fields:
        args.append(f"-fields {tcl_list(list(fields))}")
    if unconstrained:
        args.append("-unconstrained")
    if slack_max is not None:
        args.append(f"-slack_max {tcl_quote(slack_max)}")
    output = await SESSION.run(" ".join(args))
    return output or "No paths found."


@mcp.tool(annotations=READ_ONLY)
async def list_violations(
    path_delay: Annotated[
        Literal["max", "min", "both"], Field(description="max = setup, min = hold.")
    ] = "both",
    max_endpoints: Annotated[int, Field(ge=1, le=1000, description="Violating endpoints to list per check.")] = 20,
) -> str:
    """List endpoints with negative (or zero) slack, worst first."""
    require_design()
    checks = ["max", "min"] if path_delay == "both" else [path_delay]
    script = []
    for check in checks:
        label = "setup (max)" if check == "max" else "hold (min)"
        script += [
            f'puts "== {label} violations"',
            f"report_checks -path_delay {check} -format end -slack_max 0 "
            f"-group_path_count {max_endpoints} -endpoint_path_count 1 -digits 4",
        ]
    output = await SESSION.run("\n".join(script))
    return output


@mcp.tool(annotations=READ_ONLY)
async def check_design_rules() -> str:
    """Max slew, max capacitance and max fanout violations."""
    require_design()
    output = await SESSION.run("report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 4")
    return output or "No max slew, max capacitance or max fanout violations."


@mcp.tool(annotations=READ_ONLY)
async def check_setup() -> str:
    """Check constraints: missing clocks, unconstrained endpoints, missing I/O delays, loops."""
    require_design()
    output = await SESSION.run("check_setup -verbose")
    return output or "check_setup found no problems."


@mcp.tool(annotations=READ_ONLY)
async def report_clocks() -> str:
    """Clock definitions (period, waveform, sources) and setup/hold clock skew."""
    require_design()
    return await SESSION.run(
        'puts "== clock properties"\n'
        "report_clock_properties\n"
        'puts "== clock skew (setup)"\n'
        "report_clock_skew -setup -digits 4\n"
        'puts "== clock skew (hold)"\n'
        "report_clock_skew -hold -digits 4"
    )


@mcp.tool(annotations=READ_ONLY)
async def report_power(
    instances: Annotated[list[str] | None, Field(description="Report only these instances.")] = None,
    highest_n: Annotated[int | None, Field(ge=1, le=1000, description="Also list the N highest-power instances.")] = None,
) -> str:
    """Design power (internal, switching, leakage) by group, from default activities or read VCD/SAIF."""
    require_design()
    args = ["report_power", "-digits 4"]
    if instances:
        args.append("-instances [concat " + " ".join(f"[get_cells {tcl_quote(i)}]" for i in instances) + "]")
    elif highest_n:
        args.append(f"-highest_power_instances {highest_n}")
    return await SESSION.run(" ".join(args))


@mcp.tool(annotations=READ_ONLY)
async def design_statistics() -> str:
    """Design area/utilization, cell usage by type, and object counts."""
    require_design()
    return await SESSION.run(
        'puts "== design area"\n'
        "report_design_area\n"
        'puts "== cell usage"\n'
        "report_cell_usage\n" + DESIGN_STATS_TCL
    )


@mcp.tool(annotations=READ_ONLY)
async def timing_histogram(
    num_bins: Annotated[int, Field(ge=1, le=100)] = 10,
    mode: Annotated[Literal["setup", "hold"], Field(description="Histogram of setup or hold endpoint slack.")] = "setup",
) -> str:
    """Histogram of endpoint slacks, to see how many endpoints are near or past zero."""
    require_design()
    return await SESSION.run(f"report_timing_histogram -num_bins {num_bins} -{mode}")


@mcp.tool(annotations=READ_ONLY)
async def inspect_object(
    name: Annotated[str, Field(description="Full hierarchical name of the net, instance or pin/port.")],
    kind: Annotated[Literal["net", "instance", "pin"], Field(description="Object type.")],
) -> str:
    """Details of one object: net (driver, loads, caps), instance (cell, pins), or pin (worst paths through it)."""
    require_design()
    if kind == "net":
        script = f"report_net -digits 4 {tcl_quote(name)}"
    elif kind == "instance":
        script = f"report_instance {tcl_quote(name)}"
    else:
        script = (
            f"report_checks -through {tcl_objs([name])} -path_delay min_max "
            "-fields {slew capacitance fanout} -digits 4 -format full"
        )
    return await SESSION.run(script)


FIND_COMMANDS = {
    "instance": "get_cells -hierarchical",
    "net": "get_nets -hierarchical",
    "pin": "get_pins -hierarchical",
    "port": "get_ports",
    "clock": "get_clocks",
    "lib_cell": "get_lib_cells",
}


@mcp.tool(annotations=READ_ONLY)
async def find_objects(
    pattern: Annotated[
        str, Field(description="Glob pattern, e.g. '*reg*', 'u1/*', or 'lib/NAND*' for lib cells.")
    ],
    kind: Annotated[Literal["instance", "net", "pin", "port", "clock", "lib_cell"], Field()] = "instance",
    limit: Annotated[int, Field(ge=1, le=5000)] = 100,
) -> str:
    """Find design objects by name pattern; returns the match count and the first `limit` names."""
    require_design()
    name_cmd = "get_name" if kind == "lib_cell" else "get_full_name"
    script = f"""
set __objs [{FIND_COMMANDS[kind]} -quiet {tcl_quote(pattern)}]
puts "matches: [llength $__objs]"
foreach __obj [lrange $__objs 0 {limit - 1}] {{
  puts [{name_cmd} $__obj]
}}
"""
    return await SESSION.run(script)


@mcp.tool(annotations=MUTATING)
async def set_clock_period(
    clock: Annotated[str, Field(description="Existing clock name.")],
    period: Annotated[float, Field(gt=0, description="New period in library time units (usually ns).")],
) -> dict[str, Any]:
    """What-if: redefine a clock with a new period on the same sources, then return the new timing summary.

    The clock is re-created with a 50% duty cycle, so latency/uncertainty set on
    it may need re-applying. load_design restores the original constraints.
    """
    require_design()
    name, new_period = tcl_quote(clock), tcl_quote(period)
    output = await SESSION.run(
        f"""
set __clk [get_clocks -quiet {name}]
if {{[llength $__clk] == 0}} {{ error "Clock not found: [set __name {name}]" }}
set __old [get_property $__clk period]
set __srcs [get_property $__clk sources]
if {{[llength $__srcs] > 0}} {{
  create_clock -name {name} -period {new_period} $__srcs
}} else {{
  create_clock -name {name} -period {new_period}
}}
puts "clock [set __name {name}]: period $__old -> [set __new {new_period}]"
"""
    )
    summary = await _timing_summary()
    return {"change": output, **summary}


if CFG.allow_raw_tcl:

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    async def run_tcl(
        script: Annotated[str, Field(description="Tcl script to evaluate in the OpenROAD session.")],
    ) -> str:
        """Run an arbitrary OpenROAD/OpenSTA Tcl script in the live session (escape hatch).

        Do not call `exit`; it ends the session.
        """
        output = await SESSION.run(script)
        return output or "(no output)"


# --------------------------------- prompts ---------------------------------


@mcp.prompt()
def load_and_analyze(
    liberty_files: str,
    netlist_file: str,
    top_module: str,
    sdc_file: str,
    lef_files: str = "",
) -> str:
    """Load a design and give a first timing health check."""
    return f"""Load this design into the OpenROAD STA session and give me a first timing health check.

Files:
- Liberty: {liberty_files}
- LEF (technology LEF first): {lef_files or "(none given; ask me if OpenROAD needs them)"}
- Netlist: {netlist_file} (top module: {top_module})
- SDC: {sdc_file}

Steps:
1. Call load_design. If the netlist ends in .v use verilog_files + top_module; .def -> def_file; .odb -> db_file.
   Split comma-separated file lists into separate entries.
2. Call timing_summary and check_setup.
3. Summarize: design size, clocks, setup and hold status (WNS/TNS), and any constraint
   problems found by check_setup. End with the 3 most important next steps."""


@mcp.prompt()
def timing_signoff_review() -> str:
    """Full timing sign-off review of the loaded design."""
    return """Do a timing sign-off review of the design loaded in the OpenROAD STA session.

1. timing_summary: record setup and hold WNS/TNS.
2. list_violations (path_delay="both"): how many endpoints fail, and are they clustered
   (same clock domain, same module, same startpoint)?
3. report_timing for the worst setup path (path_delay="max") and the worst hold path
   (path_delay="min"), with fields ["slew", "capacitance", "fanout"].
4. check_design_rules: max slew / capacitance / fanout violations.
5. check_setup: unconstrained endpoints, missing clocks or I/O delays, combinational loops.

Finish with a verdict (PASS / FAIL / NOT SIGN-OFF READY) and a ranked issue list. For each
issue give its impact (slack or count) and a concrete fix."""


@mcp.prompt()
def debug_setup_violation(endpoint: str) -> str:
    """Find the root cause of a setup violation at an endpoint."""
    return f"""Debug the setup (max) timing violation at endpoint `{endpoint}`.

1. report_timing with to_points=["{endpoint}"], path_delay="max", format="full_clock_expanded",
   fields=["slew", "capacitance", "fanout", "net"].
2. From the path, find the stages with the largest incremental delay. Use inspect_object on
   their nets (kind="net") and instances (kind="instance").
3. Classify the main cause: long logic depth, weak/undersized driver, high fanout, large
   wire load/slew, clock skew (capture clock earlier than launch), or a constraint problem
   (for example, a path that should be multicycle or false).
4. Propose fixes in priority order (gate sizing, buffering, logic restructuring, useful skew,
   or a constraint fix only if it is functionally justified), with the expected slack gain."""


@mcp.prompt()
def debug_hold_violation(endpoint: str) -> str:
    """Find the root cause of a hold violation at an endpoint."""
    return f"""Debug the hold (min) timing violation at endpoint `{endpoint}`.

1. report_timing with to_points=["{endpoint}"], path_delay="min", format="full_clock_expanded",
   fields=["slew", "capacitance"].
2. report_clocks: check the skew between the launch and capture clocks.
3. Decide whether the violation comes from a very short data path or from clock skew
   (capture clock arriving late).
4. Propose fixes (delay cell / buffer insertion on the data path, clock tree skew
   adjustment), and make sure a fix does not break setup timing on the same path."""


@mcp.prompt()
def constraint_audit() -> str:
    """Audit the SDC constraints of the loaded design."""
    return """Audit the timing constraints of the design loaded in the OpenROAD STA session.

1. check_setup: unconstrained endpoints, missing clocks, missing input/output delays, loops.
2. report_clocks: are the periods, waveforms and sources sensible? Is any skew unexpected?
3. report_timing with unconstrained=true, format="end", group_path_count=20: which paths
   are not timed at all?
4. find_objects kind="port" pattern="*": are all inputs/outputs covered by I/O delays?

Report every missing or suspicious constraint and the SDC commands that would fix it."""


@mcp.prompt()
def what_if_clock_period(clock: str, period_ns: str) -> str:
    """Compare timing before and after changing a clock period."""
    return f"""Evaluate what happens if clock `{clock}` runs with period {period_ns}.

1. timing_summary: record the current setup/hold WNS and TNS.
2. set_clock_period with clock="{clock}" and period={period_ns}.
3. Compare before and after (setup and hold WNS/TNS, pass/fail status).
4. Estimate the achievable minimum period / Fmax: minimum period = current period - setup WNS
   (when WNS is negative this is larger than the current period).
Note that set_clock_period changes the live session; reloading the design restores the SDC."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
