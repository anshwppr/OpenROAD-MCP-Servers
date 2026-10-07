"""Shared building blocks for the physical-design stage servers (opt, place, route, signoff).

``register_stage_tools`` adds the tools every stage server has: platform discovery and
validation, design loading, checkpoints (the hand-off between servers), design status,
parasitic estimation, layout snapshots and session control. Technology data always comes
from the active :class:`~openroad_common.platform.Platform`; nothing here names a technology.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from datetime import datetime
from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from openroad_common.parsing import messages, parse_design_area, parse_slack_report
from openroad_common.platform import (
    FILE_KEYS,
    FILE_LIST_KEYS,
    PLATFORM_DRIVER_TCL,
    Platform,
    list_platforms,
    resolve_platform,
)
from openroad_common.records import by_kind, first, query, tcl
from openroad_common.session import Config, OpenRoadSession
from openroad_common.tcl import parse_records, tcl_file, tcl_list, tcl_quote, to_wsl_path

READ_ONLY = ToolAnnotations(read_only_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

LOG_TAIL_CHARS = 6000

STAGE_DRIVER_TCL = PLATFORM_DRIVER_TCL + r"""
# From OpenROAD test/flow_helpers.tcl, so OpenROAD's reference SDC files load.
proc set_all_input_output_delays {{clk_period_factor .2}} {
  set clk [lindex [all_clocks] 0]
  set period [get_property $clk period]
  set delay [expr $period * $clk_period_factor]
  set_input_delay $delay -clock $clk [delete_from_list [all_inputs] [all_clocks]]
  set_output_delay $delay -clock $clk [delete_from_list [all_outputs] [all_clocks]]
}

proc __stage_null {obj} {
  expr {$obj eq "" || $obj eq "NULL"}
}

proc __stage_block {} {
  set block [ord::get_db_block]
  if {[__stage_null $block]} { error "No design block is loaded." }
  return $block
}

proc __stage_bbox {r} {
  list [$r xMin] [$r yMin] [$r xMax] [$r yMax]
}

# Design status records plus timing/area report lines (parsed on the Python side).
proc __stage_status {} {
  set block [__stage_block]
  array set st {}
  set n 0
  foreach inst [$block getInsts] {
    incr n
    incr st([$inst getPlacementStatus])
  }
  __mcp_rec insts $n
  foreach k [array names st] { __mcp_rec status $k $st($k) }
  __mcp_rec nets [llength [$block getNets]]
  __mcp_rec ports [llength [$block getBTerms]]
  __mcp_rec rows [llength [$block getRows]]
  __mcp_rec tracks [llength [$block getTrackGrids]]
  __mcp_rec dbu [$block getDbUnitsPerMicron]
  __mcp_rec die {*}[__stage_bbox [$block getDieArea]]
  __mcp_rec core {*}[__stage_bbox [$block getCoreArea]]
  __mcp_rec routed [expr {[catch {$block designIsRouted 0} r] ? 0 : $r}]
  set clocks 0
  catch {set clocks [llength [all_clocks]]}
  __mcp_rec clocks $clocks
  if {$n > 0} { catch {report_design_area} }
  if {$clocks > 0} {
    catch {
      report_worst_slack -max -digits 4
      report_tns -max -digits 4
      report_worst_slack -min -digits 4
      report_tns -min -digits 4
    }
    # DRV counts come from report_check_types (sta::max_fanout_violation_count crashes
    # OpenROAD 26Q2 on unplaced designs); Python counts the VIOLATED lines per section.
    foreach chk {max_slew max_capacitance max_fanout} {
      puts "== drv $chk"
      catch {report_check_types -$chk -violators}
    }
    puts "== drv end"
  }
}

proc __stage_snapshot {path width area options} {
  set cmd [list save_image -web -width $width]
  if {$area ne ""} { lappend cmd -area $area }
  foreach {k v} $options { lappend cmd -display_option [list $k $v] }
  lappend cmd $path
  {*}$cmd
  set f [open $path rb]
  set data [read $f]
  close $f
  file delete -force $path
  __mcp_rec image [binary encode base64 $data]
}

proc __stage_ckpt_dir {dir} {
  if {$dir eq ""} {
    set dir [file join $::env(HOME) openroad_mcp checkpoints]
  }
  file mkdir $dir
  return [file normalize $dir]
}

proc __stage_write_text {path text} {
  set f [open $path w]
  fconfigure $f -encoding utf-8
  puts -nonewline $f $text
  close $f
}

proc __stage_list_ckpts {dir} {
  foreach path [lsort [glob -nocomplain -directory $dir -- *.json]] {
    set f [open $path]
    set text [read $f]
    close $f
    __mcp_rec ckpt $path $text
  }
}

proc __stage_check {key kind name} {
  set ok 0
  switch $kind {
    file { set ok [file exists $name] }
    layer { set ok [expr {![__stage_null [[ord::get_db_tech] findLayer $name]]}] }
    master { set ok [expr {![__stage_null [[ord::get_db] findMaster $name]]}] }
    pattern {
      foreach lib [[ord::get_db] getLibs] {
        foreach m [$lib getMasters] {
          if {[string match $name [$m getName]]} { set ok 1; break }
        }
        if {$ok} { break }
      }
    }
    site {
      foreach lib [[ord::get_db] getLibs] {
        if {![__stage_null [$lib findSite $name]]} { set ok 1; break }
      }
    }
  }
  __mcp_rec chk $key $kind $name $ok
}
"""


# ---------------------------------------------------------------------------
# Helpers shared by the stage servers
# ---------------------------------------------------------------------------


def tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


def active_platform(session: OpenRoadSession) -> Platform:
    platform = session.design.platform
    if platform is None:
        raise ToolError("No platform is active. Call load_design or load_checkpoint first.")
    return platform


def record_step(session: OpenRoadSession, text: str) -> None:
    session.design.edits.append(f"{datetime.now():%H:%M:%S} {text}")


def platform_setup_tcl(platform: Platform) -> str:
    """Per-design technology setup taken from the platform (layer RC, wire RC, dont_use, routing layers)."""
    lines: list[str] = []
    if platform.get("layer_rc_file"):
        lines.append(f"source {tcl_file(platform.get('layer_rc_file'))}")
    if platform.get("wire_rc_layer"):
        lines.append(f"set_wire_rc -signal -layer {tcl_quote(platform.get('wire_rc_layer'))}")
    if platform.get("wire_rc_layer_clk"):
        lines.append(f"set_wire_rc -clock -layer {tcl_quote(platform.get('wire_rc_layer_clk'))}")
    if platform.get("dont_use"):
        lines.append(f"catch {{set_dont_use {tcl_list(platform.get('dont_use'))}}}")
    routing = routing_setup_tcl(platform)
    if routing:
        # Routing-layer settings need track grids, which exist only after floorplanning.
        lines.append(f"if {{[llength [[ord::get_db_block] getTrackGrids]] > 0}} {{\n{routing}\n}}")
    return "\n".join(lines)


def routing_setup_tcl(platform: Platform) -> str:
    """Global-routing layers, layer adjustments and macro extension from the platform (flow.tcl order).

    Global placement's routability mode and the router both use these; the adjustments live in
    the router, not the database, so every session re-applies them.
    """
    lines: list[str] = []
    for layers, adjustment in platform.get("global_routing_layer_adjustments", []):
        lines.append(f"set_global_routing_layer_adjustment {tcl_quote(layers)} {adjustment}")
    signal, clock = platform.get("global_routing_layers"), platform.get("global_routing_clock_layers")
    if signal:
        lines.append(f"set_routing_layers -signal {tcl_quote(signal)}" + (f" -clock {tcl_quote(clock)}" if clock else ""))
    if platform.get("macro_extension") is not None:
        lines.append(f"set_macro_extension {int(platform.get('macro_extension'))}")
    return "\n".join(lines)


def _bbox_um(fields: list[str], dbu: int) -> list[float]:
    return [round(int(v) / dbu, 4) for v in fields[:4]]


def drv_counts(log: str) -> dict[str, int] | None:
    """Count VIOLATED lines in each ``== drv <check>`` section printed by __stage_status."""
    sections = re.findall(r"^== drv (max_\w+)\n(.*?)(?=^== drv )", log, re.MULTILINE | re.DOTALL)
    if not sections:
        return None
    names = {"max_slew": "max_slew", "max_capacitance": "max_cap", "max_fanout": "max_fanout"}
    return {names[check]: body.count("VIOLATED") for check, body in sections}


def status_from(records: list[list[str]], log: str, session: OpenRoadSession) -> dict[str, Any]:
    dbu_rec = first(records, "dbu", required=False)
    dbu = int(dbu_rec[1]) if dbu_rec else 1000
    slacks = parse_slack_report(log)
    platform = session.design.platform
    counts = {r[0]: int(r[1]) for r in records if r and r[0] in ("insts", "nets", "ports", "rows", "tracks", "clocks")}
    placement = {r[1]: int(r[2]) for r in by_kind(records, "status")}
    placed = sum(v for k, v in placement.items() if k not in ("NONE", "UNPLACED"))
    routed = first(records, "routed", required=False)
    result: dict[str, Any] = {
        "platform": platform.name if platform else None,
        "counts": counts,
        "placement_status": placement,
        "stage": (
            "routed" if routed and routed[1] == "1"
            else "placed" if counts.get("insts") and placed == counts["insts"]
            else "partially placed" if placed
            else "floorplanned" if counts.get("rows")
            else "netlist"
        ),
        "die_um": _bbox_um(first(records, "die")[1:], dbu) if first(records, "die", required=False) else None,
        "core_um": _bbox_um(first(records, "core")[1:], dbu) if first(records, "core", required=False) else None,
        **parse_design_area(log),
        "setup": slacks["setup"] or None,
        "hold": slacks["hold"] or None,
        "drv": drv_counts(log),
        "parasitics": session.design.parasitics,
    }
    return {k: v for k, v in result.items() if v not in (None, {}, [])}


async def design_status(session: OpenRoadSession) -> dict[str, Any]:
    records, log = await query(session, "__stage_status")
    return status_from(records, log, session)


async def run_step(
    session: OpenRoadSession,
    script: str,
    parser: Callable[[str], dict[str, Any]] | None = None,
    description: str | None = None,
    timeout: float | None = None,
    before: bool = True,
) -> dict[str, Any]:
    """Run an action script; return its parsed summary, design status before/after and the log tail."""
    session.require_design()
    status_before = await design_status(session) if before else None
    output = await session.run(script, timeout=timeout, max_chars=50_000_000)
    _records, log = parse_records(output)
    result: dict[str, Any] = {"summary": parser(log) if parser else {"warnings": messages(log)}}
    if status_before is not None:
        result["before"] = _status_brief(status_before)
    result["after"] = _status_brief(await design_status(session))
    result["log"] = tail(log)
    if description:
        record_step(session, description)
    return result


def _status_brief(status: dict[str, Any]) -> dict[str, Any]:
    keys = ("stage", "area_um2", "utilization_pct", "setup", "hold", "drv")
    brief = {k: status[k] for k in keys if k in status}
    brief["instances"] = status.get("counts", {}).get("insts")
    return brief


def _name_ok(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ToolError("Checkpoint names may only contain letters, digits, '_', '-' and '.'.")
    return name


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_stage_tools(mcp: MCPServer, session: OpenRoadSession, cfg: Config, server_name: str) -> None:
    """Register the tools every stage server shares."""

    @mcp.tool(annotations=READ_ONLY, name="list_platforms")
    async def list_platforms_tool() -> dict[str, Any]:
        """Technology platforms available: built-in presets, user JSON files and discovered OpenROAD .vars files."""
        platforms = await list_platforms(session)
        return {"platforms": platforms, "active": session.design.platform.name if session.design.platform else None}

    @mcp.tool(annotations=READ_ONLY)
    async def platform_info(
        name: Annotated[str | None, Field(description="Platform name or .vars/.json path (default: active/default).")] = None,
    ) -> dict[str, Any]:
        """All resolved settings of a technology platform (files, site, cells, layers, flow defaults)."""
        if name is None and session.design.platform is not None:
            return session.design.platform.summary()
        return (await resolve_platform(session, name)).summary()

    @mcp.tool(annotations=MUTATING)
    async def validate_platform(
        name: Annotated[str | None, Field(description="Platform name or .vars/.json path (default: active/default).")] = None,
    ) -> dict[str, Any]:
        """Check a platform: every file exists, and its site, cells and layers exist in the LEF.

        Without a loaded design this restarts the session and reads only the platform's LEF files.
        """
        use_current = name is None and session.design.loaded and session.design.platform is not None
        if session.design.loaded and not use_current:
            raise ToolError(
                "A design is loaded; validating another platform would discard it. Omit 'name' to validate the "
                "active platform, or save a checkpoint and call reset_session first."
            )
        platform = session.design.platform if use_current else await resolve_platform(session, name)
        if not use_current:
            await session.restart()
            lefs = platform.lef_files()
            if lefs:
                await session.run("\n".join(f"catch {{read_lef {tcl_quote(f)}}}" for f in lefs), max_chars=50_000_000)

        checks: list[tuple[str, str, str]] = []
        for key in sorted(FILE_KEYS):
            if platform.get(key):
                checks.append((key, "file", platform.get(key)))
        for key in sorted(FILE_LIST_KEYS):
            checks += [(key, "file", f) for f in platform.get(key, [])]
        for corner, path in platform.get("liberty_files", {}).items():
            checks.append((f"liberty_files.{corner}", "file", path))
        if platform.get("site"):
            checks.append(("site", "site", platform.get("site")))
        tap = platform.tapcell()
        for key in ("tapcell_master", "endcap_master"):
            if tap.get(key):
                checks.append((key, "master", tap[key]))
        for key in ("cts_buffer", "diode_cell"):
            if platform.get(key):
                checks.append((key, "master", platform.get(key)))
        for key in ("tielo_port", "tiehi_port"):
            if platform.get(key):
                checks.append((key, "master", str(platform.get(key)).split("/")[0]))
        if platform.get("filler_cells"):
            checks.append(("filler_cells", "pattern", platform.get("filler_cells")))
        for pattern in platform.get("dont_use", []):
            checks.append(("dont_use", "pattern", pattern))
        layer_keys = ("io_placer_hor_layer", "io_placer_ver_layer", "wire_rc_layer", "wire_rc_layer_clk",
                      "min_routing_layer", "max_routing_layer")
        for key in layer_keys:
            if platform.get(key):
                checks.append((key, "layer", platform.get(key)))
        for key in ("global_routing_layers", "global_routing_clock_layers"):
            for layer in str(platform.get(key, "")).split("-") if platform.get(key) else []:
                checks.append((key, "layer", layer))
        for layers, _adj in platform.get("global_routing_layer_adjustments", []):
            for layer in str(layers).split("-"):
                checks.append(("global_routing_layer_adjustments", "layer", layer))

        script = "\n".join(f"__stage_check {tcl_quote(k)} {kind} {tcl_quote(v)}" for k, kind, v in checks)
        records, _log = await query(session, script, require_design=False)
        required = {"tech_lef", "std_cell_lef", "liberty_file", "site"}
        results = []
        for r in by_kind(records, "chk"):
            ok = r[4] == "1"
            level = "PASS" if ok else ("FAIL" if r[1] in required else "WARN")
            results.append({"key": r[1], "kind": r[2], "value": r[3], "result": level})
        missing = [k for k in ("tech_lef", "std_cell_lef", "liberty_file", "site", "tapcell_args", "pdn_cfg",
                               "io_placer_hor_layer", "io_placer_ver_layer", "wire_rc_layer", "cts_buffer",
                               "filler_cells", "global_routing_layers", "rcx_rules_file") if not platform.get(k)]
        verdict = "FAIL" if any(x["result"] == "FAIL" for x in results) else (
            "WARN" if missing or any(x["result"] == "WARN" for x in results) else "PASS")
        return {"platform": platform.name, "source": platform.source, "verdict": verdict,
                "not_defined": missing, "checks": results}

    @mcp.tool(annotations=MUTATING)
    async def load_design(
        platform: Annotated[str | None, Field(description="Platform name or .vars/.json path (default from OPENROAD_PLATFORM).")] = None,
        verilog_files: Annotated[list[str], Field(description="Gate-level Verilog netlist(s).")] = [],
        top_module: Annotated[str | None, Field(description="Top module (with verilog_files).")] = None,
        def_file: Annotated[str | None, Field(description="DEF file as the netlist.")] = None,
        db_file: Annotated[str | None, Field(description="OpenROAD .odb database as the netlist.")] = None,
        sdc_file: Annotated[str | None, Field(description="SDC constraints.")] = None,
        lef_files: Annotated[list[str] | None, Field(description="Override the platform's LEF files (tech LEF first).")] = None,
        liberty_files: Annotated[list[str] | None, Field(description="Override the platform's Liberty files.")] = None,
        corner: Annotated[str | None, Field(description="Use the platform's liberty corner with this name.")] = None,
    ) -> dict[str, Any]:
        """Start a fresh session: read the platform's LEF/Liberty, the netlist (Verilog, DEF or ODB) and the SDC,
        then apply the platform's parasitic/dont_use/routing-layer setup. Returns the design status."""
        sources = sum([bool(verilog_files), def_file is not None, db_file is not None])
        if sources != 1:
            raise ToolError("Give exactly one netlist source: verilog_files (+ top_module), def_file, or db_file.")
        if verilog_files and not top_module:
            raise ToolError("top_module is required with verilog_files.")
        await session.restart()
        plat = await resolve_platform(session, platform)
        lefs = [] if db_file else (lef_files or plat.lef_files())
        libs = liberty_files or plat.liberty_files(corner)
        if not db_file and not lefs:
            raise ToolError(f"No LEF files: platform '{plat.name}' defines none and lef_files was not given.")
        if not libs:
            raise ToolError(f"No Liberty files: platform '{plat.name}' defines none and liberty_files was not given.")

        def step(command: str, path: str) -> list[str]:
            return [f"puts {tcl_quote(f'== {command} {path}')}", f"{command} {tcl_file(path)}"]

        script: list[str] = []
        for lef in lefs:
            script += step("read_lef", lef)
        for lib in libs:
            script += step("read_liberty", lib)
        if db_file:
            script += step("read_db", db_file)
        elif def_file:
            script += step("read_def", def_file)
        else:
            for v in verilog_files:
                script += step("read_verilog", v)
            script.append(f"link_design {tcl_quote(top_module)}")
        if sdc_file:
            script += step("read_sdc", sdc_file)
        script.append(platform_setup_tcl(plat))
        output = await session.run("\n".join(script), max_chars=50_000_000)

        design = session.design
        design.loaded = True
        design.platform = plat
        design.top_module = top_module
        design.add("lef", *lefs)
        design.add("liberty", *libs)
        design.add("verilog", *verilog_files)
        for kind, path in (("def", def_file), ("db", db_file), ("sdc", sdc_file)):
            if path:
                design.add(kind, path)
        record_step(session, f"load_design ({plat.name})")
        status = await design_status(session)
        if status.get("stage") in ("placed", "routed", "partially placed") and plat.get("wire_rc_layer"):
            await session.run("estimate_parasitics -placement")
            design.parasitics = "placement estimate"
            status = await design_status(session)
        return {"status": status, "warnings": messages(output)}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def save_checkpoint(
        name: Annotated[str, Field(description="Checkpoint name, e.g. placed, cts, routed.")],
        note: Annotated[str, Field(description="Free-text note stored in the manifest.")] = "",
        overwrite: Annotated[bool, Field(description="Replace an existing checkpoint of this name.")] = False,
        directory: Annotated[str | None, Field(description="Checkpoint directory (default $HOME/openroad_mcp/checkpoints in WSL).")] = None,
    ) -> dict[str, Any]:
        """Save the design for another stage server: <name>.odb + <name>.sdc + <name>.json (manifest with the
        resolved platform, Liberty files and stage history)."""
        session.require_design()
        plat = active_platform(session)
        _name_ok(name)
        directory = directory or _env_checkpoint_dir()
        records, _ = await query(
            session,
            tcl(
                """
set __dir [__stage_ckpt_dir @DIR@]
set __base [file join $__dir @NAME@]
if {!@OVERWRITE@ && [file exists $__base.json]} {
  error "Checkpoint already exists: $__base.json (pass overwrite=true to replace it)"
}
write_db $__base.odb
write_sdc $__base.sdc
__mcp_rec paths $__base.odb $__base.sdc $__base.json
""",
                DIR=tcl_quote(to_wsl_path(directory) if directory else ""),
                NAME=tcl_quote(name),
                OVERWRITE=int(overwrite),
            ),
        )
        odb, sdc, manifest_path = first(records, "paths")[1:4]
        status = await design_status(session)
        manifest = {
            "name": name,
            "server": server_name,
            "stage": status.get("stage"),
            "created": datetime.now().isoformat(timespec="seconds"),
            "note": note,
            "odb": odb,
            "sdc": sdc,
            "liberty_files": session.design.files.get("liberty", []),
            "platform": plat.to_dict(),
            "history": session.design.edits,
            "status": status,
        }
        await session.run(f"__stage_write_text {tcl_quote(manifest_path)} {tcl_quote(json.dumps(manifest))}")
        record_step(session, f"save_checkpoint {name}")
        return {"checkpoint": name, "manifest": manifest_path, "odb": odb, "sdc": sdc, "stage": status.get("stage")}

    @mcp.tool(annotations=MUTATING)
    async def load_checkpoint(
        name_or_path: Annotated[str, Field(description="Checkpoint name, or path to its .json manifest.")],
        directory: Annotated[str | None, Field(description="Checkpoint directory (default $HOME/openroad_mcp/checkpoints).")] = None,
    ) -> dict[str, Any]:
        """Load a checkpoint saved by any stage server: Liberty -> ODB -> SDC, the stored platform's setup, and
        placement parasitics when the design is placed."""
        if name_or_path.endswith(".json") or "/" in name_or_path or "\\" in name_or_path:
            manifest_path = to_wsl_path(name_or_path)
            script = f"__plat_read_text {tcl_quote(manifest_path)}"
        else:
            _name_ok(name_or_path)
            directory = directory or _env_checkpoint_dir()
            script = (
                f"set __dir [__stage_ckpt_dir {tcl_quote(to_wsl_path(directory) if directory else '')}]\n"
                f"__plat_read_text [file join $__dir {tcl_quote(name_or_path + '.json')}]"
            )
        records, _ = await query(session, script, require_design=False)
        try:
            manifest = json.loads(first(records, "text")[1])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ToolError(f"Cannot read checkpoint manifest: {exc}") from exc

        plat = Platform.from_dict(manifest["platform"])
        await session.restart()
        lines = [f"read_liberty {tcl_file(lib)}" for lib in manifest.get("liberty_files", [])]
        lines.append(f"read_db {tcl_file(manifest['odb'])}")
        if manifest.get("sdc"):
            lines.append(f"if {{[file exists {tcl_quote(manifest['sdc'])}]}} {{ read_sdc {tcl_quote(manifest['sdc'])} }}")
        lines.append(platform_setup_tcl(plat))
        output = await session.run("\n".join(lines), max_chars=50_000_000)

        design = session.design
        design.loaded = True
        design.platform = plat
        design.add("liberty", *manifest.get("liberty_files", []))
        design.add("db", manifest["odb"])
        design.edits = list(manifest.get("history", []))
        record_step(session, f"load_checkpoint {manifest.get('name')} (from {manifest.get('server')})")
        status = await design_status(session)
        if status.get("stage") in ("placed", "routed", "partially placed") and plat.get("wire_rc_layer"):
            await session.run("estimate_parasitics -placement")
            design.parasitics = "placement estimate"
            status = await design_status(session)
        return {"checkpoint": manifest.get("name"), "saved_by": manifest.get("server"),
                "created": manifest.get("created"), "note": manifest.get("note"), "status": status,
                "warnings": messages(output)}

    @mcp.tool(annotations=READ_ONLY)
    async def list_checkpoints(
        directory: Annotated[str | None, Field(description="Checkpoint directory (default $HOME/openroad_mcp/checkpoints).")] = None,
    ) -> dict[str, Any]:
        """Checkpoints available for hand-off between the stage servers."""
        directory = directory or _env_checkpoint_dir()
        records, _ = await query(
            session,
            f"__stage_list_ckpts [__stage_ckpt_dir {tcl_quote(to_wsl_path(directory) if directory else '')}]",
            require_design=False,
        )
        items = []
        for r in by_kind(records, "ckpt"):
            try:
                m = json.loads(r[2])
            except json.JSONDecodeError:
                continue
            items.append({"name": m.get("name"), "stage": m.get("stage"), "server": m.get("server"),
                          "platform": m.get("platform", {}).get("name"), "created": m.get("created"),
                          "note": m.get("note"), "manifest": r[1]})
        return {"checkpoints": items}

    @mcp.tool(annotations=READ_ONLY, name="design_status")
    async def design_status_tool() -> dict[str, Any]:
        """Stage (netlist/floorplanned/placed/routed), counts, area/utilization, setup/hold WNS/TNS, DRV counts."""
        session.require_design()
        return await design_status(session)

    @mcp.tool(annotations=MUTATING)
    async def estimate_parasitics(
        source: Annotated[Literal["placement", "global_routing"], Field(description="Estimate from placement or global routes.")] = "placement",
    ) -> dict[str, Any]:
        """Re-estimate wire parasitics so timing reflects the current placement/routing."""
        result = await run_step(session, f"estimate_parasitics -{source}", description=f"estimate_parasitics {source}")
        session.design.parasitics = f"{source.replace('_', ' ')} estimate"
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def snapshot(
        width: Annotated[int, Field(ge=256, le=4096, description="Image width in pixels.")] = 1024,
        area: Annotated[list[float] | None, Field(description="Region [x1, y1, x2, y2] in microns (default: whole die).")] = None,
        show: Annotated[dict[str, bool] | None, Field(description="Display options, e.g. {\"routing\": false, \"rudy\": true}.")] = None,
    ) -> Image:
        """Render a PNG image of the layout (headless OpenROAD renderer)."""
        session.require_design()
        if area is not None and len(area) != 4:
            raise ToolError("area must be [x1, y1, x2, y2] in microns.")
        options = []
        for key, value in (show or {}).items():
            options += [key, "true" if value else "false"]
        records, _ = await query(
            session,
            tcl(
                "__stage_snapshot @PATH@ @WIDTH@ @AREA@ @OPTS@",
                PATH=tcl_quote(f"/tmp/mcp_{server_name}_snapshot.png"),
                WIDTH=width,
                AREA=tcl_list(area) if area else '""',
                OPTS=tcl_list(options) if options else '""',
            ),
        )
        return Image(data=base64.b64decode(first(records, "image")[1]), format="png")

    @mcp.tool(annotations=READ_ONLY)
    async def session_status(
        check_connection: Annotated[bool, Field(description="Start OpenROAD if needed and report its version.")] = False,
    ) -> dict[str, Any]:
        """Session state, active platform, loaded files and the stage history of this design."""
        version = None
        if check_connection:
            version = await session.run(
                'if {[catch {puts "openroad [ord::openroad_version]"}]} {puts "openroad (version unknown)"}'
            )
        design = session.design
        return {
            "server": server_name,
            "running": session.running,
            "pid": session.pid,
            "wsl_distro": cfg.wsl_distro,
            "command": subprocess.list2cmdline(session.command) if session.command else None,
            "version": version,
            "timeout_s": cfg.timeout,
            "design_loaded": design.loaded,
            "platform": design.platform.name if design.platform else None,
            "parasitics": design.parasitics,
            "files": design.files,
            "history": design.edits,
            "raw_tcl_enabled": cfg.allow_raw_tcl,
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    async def reset_session() -> str:
        """Stop OpenROAD and forget the design (save a checkpoint first to keep it)."""
        await session.close()
        return "OpenROAD session stopped. Use load_design or load_checkpoint to start again."

    if cfg.allow_raw_tcl:

        @mcp.tool(annotations=DESTRUCTIVE)
        async def run_tcl(
            script: Annotated[str, Field(description="Tcl script to evaluate in the OpenROAD session.")],
        ) -> str:
            """Run any OpenROAD Tcl in the live session (escape hatch). Do not call `exit`."""
            output = await session.run(script)
            record_step(session, "run_tcl")
            return output or "(no output)"


def _env_checkpoint_dir() -> str | None:
    return os.environ.get("OPENROAD_CHECKPOINT_DIR") or None
