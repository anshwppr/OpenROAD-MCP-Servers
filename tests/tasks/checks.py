"""Run one prompt plan three ways and compare: MCP tool, pure-Python oracle, xlsx Tcl recipe.

Each check kind returns (tool value, oracle value, recipe value or NOT_RUN). The tool value
must equal the oracle value; the recipe value (the spreadsheet's verified OpenROAD command,
run through run_tcl) must equal it too, unless the deviation is listed in KNOWN_RECIPE_DEVIATIONS.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oracle import BUNDLE, Design, nsorted
from tasks.prompt_plans import Plan

NOT_RUN = "<not run>"

# Library cell names for the gate names used in the prompts.
CELL = {"AND": "AND2", "OR": "OR2", "NOT": "INV", "NAND": "NAND2", "NOR": "NOR2", "XOR": "XOR2",
        "XNOR": "XNOR2", "BUF": "BUF", "DFF": "DFF"}
USER = {v: k for k, v in CELL.items()}

# (case, prompt no) -> reason the xlsx recipe is expected to disagree with the oracle.
KNOWN_RECIPE_DEVIATIONS: dict[tuple[str, int], str] = {}


class ToolCallError(Exception):
    pass


@dataclass
class Outcome:
    plan: Plan
    tool: Any = None
    oracle: Any = None
    recipe: Any = NOT_RUN
    answer: str = ""
    seconds: float = 0.0
    recipe_seconds: float = 0.0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def tool_ok(self) -> bool:
        return not self.error and self.tool == self.oracle

    @property
    def recipe_ok(self) -> bool | None:
        if self.recipe == NOT_RUN or self.error:
            return None
        return self.recipe == self.oracle

    def as_dict(self) -> dict[str, Any]:
        def short(v):
            text = repr(v)
            return text if len(text) < 600 else text[:600] + f"... ({len(text)} chars)"

        return {
            "case": self.plan.case,
            "no": self.plan.no,
            "category": self.plan.category,
            "kind": self.plan.kind,
            "prompt": self.plan.prompt,
            "args": self.plan.args,
            "answer": self.answer,
            "tool": short(self.tool),
            "oracle": short(self.oracle),
            "recipe": short(self.recipe),
            "tool_ok": self.tool_ok,
            "recipe_ok": self.recipe_ok,
            "recipe_deviation": KNOWN_RECIPE_DEVIATIONS.get((self.plan.case, self.plan.no)),
            "error": self.error,
            "notes": self.notes,
            "seconds": round(self.seconds, 3),
            "recipe_seconds": round(self.recipe_seconds, 3),
        }


class Ctx:
    """Per-testcase state: the MCP client, the oracle model (mutated by renames), temp dir."""

    def __init__(self, client, case: str, tmp: Path, recipes: bool = True):
        self.client = client
        self.case = case
        self.tmp = tmp
        self.recipes = recipes
        self.oracle: Design | None = None
        self.recipe_seconds = 0.0

    async def call(self, tool: str, **args) -> dict[str, Any]:
        r = await self.client.call_tool(tool, args)
        if r.is_error:
            raise ToolCallError(r.content[0].text if r.content else f"{tool} failed")
        return r.structured_content

    async def tcl(self, script: str) -> list[str]:
        """Run a recipe; return its output lines without OpenROAD [INFO]/[WARNING] messages."""
        t0 = time.perf_counter()
        out = (await self.call("run_tcl", script=script))["result"]
        self.recipe_seconds += time.perf_counter() - t0
        if out == "(no output)":
            return []
        return [l.strip() for l in out.splitlines() if l.strip() and not re.match(r"^\[(INFO|WARNING|ERROR)", l.strip())]

    def out_pin(self, gate: str) -> str:
        return "Q" if self.oracle.gates[gate].seq else "Y"


def wsl(path: Path) -> str:
    p = str(path.resolve())
    return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")


def tcl_dict(text: str) -> dict[str, int]:
    parts = text.split()
    return {parts[i]: int(parts[i + 1]) for i in range(0, len(parts) - 1, 2)}


def arrival(lines: list[str]) -> int:
    """'data arrival time' of a report_checks report; -1 for 'No paths found.'"""
    for line in lines:
        m = re.match(r"^(-?[\d.]+)\s+data arrival time", line)
        if m:
            return round(float(m.group(1)))
    if any("No paths found" in l for l in lines):
        return -1
    raise AssertionError("no arrival time in report:\n" + "\n".join(lines[:40]))


def first_point_is_register(lines: list[str]) -> bool:
    return any(re.search(r"\(rising edge-triggered flip-flop\)|/Q \(DFF\)", l) for l in lines)


# --------------------------------------------------------------------------- kinds


async def k_header(ctx: Ctx, o: Outcome, case: str):
    o.tool, o.oracle = case, ctx.case
    o.answer = f"testcase {case}"


async def k_load(ctx: Ctx, o: Outcome, netlist: str):
    r = await ctx.call("load_design", netlist=netlist)
    ctx.oracle = Design.from_file(BUNDLE / "netlists" / f"{ctx.case}.v")
    o.answer = r["answer"]
    o.tool = (r["cells"], {k: v for k, v in r["by_type"].items() if v})
    o.oracle = (len(ctx.oracle.gates), dict(ctx.oracle.gate_counts()))
    if r["warnings"]:
        o.notes.append(f"load warnings: {r['warnings'][:3]}")
    if ctx.recipes:
        n = dict(tcl_dict((await ctx.tcl("set n [dict create]\nforeach c [get_cells *] { dict incr n [get_property $c ref_name] }\nputs $n"))[0]))
        o.recipe = (sum(n.values()), n)


async def k_write(ctx: Ctx, o: Outcome, path: str):
    target = ctx.tmp / path
    r = await ctx.call("write_verilog", path=str(target))
    o.answer = r["answer"]
    written = Design.from_file(target)
    o.tool = written.equivalent(ctx.oracle) or written.diff(ctx.oracle)
    o.oracle = True
    if ctx.recipes:
        raw = ctx.tmp / f"{ctx.case}_recipe_out.v"
        await ctx.tcl(f"write_verilog {{{wsl(raw)}}}")
        recipe = Design.from_file(raw, undeclared_const_nets=True)
        o.recipe = recipe.equivalent(ctx.oracle) or recipe.diff(ctx.oracle)


async def k_gate_counts(ctx: Ctx, o: Outcome):
    r = await ctx.call("gate_counts")
    o.answer = r["answer"]
    o.tool = {k: v for k, v in r["by_library_cell"].items() if v}
    o.oracle = dict(ctx.oracle.gate_counts())
    if ctx.recipes:
        o.recipe = tcl_dict((await ctx.tcl("set n [dict create]\nforeach c [get_cells *] { dict incr n [get_property $c ref_name] }\nputs $n"))[0])


async def k_gate_total(ctx: Ctx, o: Outcome):
    r = await ctx.call("gate_counts")
    o.answer, o.tool, o.oracle = r["answer"], r["total"], len(ctx.oracle.gates)
    if ctx.recipes:
        o.recipe = int((await ctx.tcl("puts [llength [get_cells *]]"))[0])


async def k_gate_count_type(ctx: Ctx, o: Outcome, cell_type: str):
    r = await ctx.call("gate_counts", cell_type=cell_type)
    o.answer, o.tool = r["answer"], r["query"]["count"]
    o.oracle = ctx.oracle.gate_counts().get(CELL[cell_type], 0)
    if ctx.recipes:
        o.recipe = int((await ctx.tcl(f'puts [llength [get_cells -filter "ref_name=={CELL[cell_type]}" *]]'))[0])


async def k_fanin_size(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("fanin_cone", node=node, list_gates=False)
    o.answer, o.tool = r["answer"], r["size"]
    o.oracle = len(ctx.oracle.fanin_cone(node)[0])
    if ctx.recipes:
        o.recipe = int((await ctx.tcl(f"puts [llength [inst_names [get_fanin -to [node {{{node}}}] -flat -only_cells] comb]]"))[0])


async def k_fanin_cone(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("fanin_cone", node=node, limit=1_000_000)
    o.answer, o.tool = r["answer"], r["gates"]
    o.oracle = nsorted(ctx.oracle.fanin_cone(node)[0])
    if ctx.recipes:
        o.recipe = nsorted(await ctx.tcl(f"putl [inst_names [get_fanin -to [node {{{node}}}] -flat -only_cells] comb]"))


async def k_cone_type_counts(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("fanin_cone", node=node, list_gates=False)
    o.answer, o.tool = r["answer"], r["by_type"]
    comb = ctx.oracle.fanin_cone(node)[0]
    o.oracle = dict(Counter(USER[ctx.oracle.gates[g].cell] for g in comb))
    o.tool = dict(o.tool)
    if ctx.recipes:
        lines = await ctx.tcl(
            "set n [dict create]\n"
            f"foreach c [get_fanin -to [node {{{node}}}] -flat -only_cells] {{\n"
            '  if {[get_full_name $c] eq ""} continue\n'
            "  set r [get_property $c ref_name]\n"
            '  if {$r ne "DFF"} { dict incr n $r }\n'
            "}\nputs $n"
        )
        o.recipe = {USER[k]: v for k, v in tcl_dict(lines[0] if lines else "").items()}


async def k_fanout_cone(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("fanout_cone", node=node, limit=1_000_000)
    o.answer, o.tool = r["answer"], r["gates"]
    o.oracle = nsorted(ctx.oracle.fanout_cone(node)[0])
    if ctx.recipes:
        o.recipe = nsorted(await ctx.tcl(f"putl [inst_names [get_fanout -from [node {{{node}}}] -flat -only_cells] comb]"))


async def k_cone_intersection(ctx: Ctx, o: Outcome, a: str, b: str):
    r = await ctx.call("cone_intersection", nodes=[a, b], limit=1_000_000)
    o.answer, o.tool = r["answer"], r["shared_gates"]
    o.oracle = nsorted(ctx.oracle.fanin_cone(a)[0] & ctx.oracle.fanin_cone(b)[0])
    if ctx.recipes:
        o.recipe = nsorted(
            await ctx.tcl(
                f"set A [inst_names [get_fanin -to [node {{{a}}}] -flat -only_cells] comb]\n"
                f"set B [inst_names [get_fanin -to [node {{{b}}}] -flat -only_cells] comb]\n"
                "putl [lmap x $A {expr {$x in $B ? $x : [continue]}}]"
            )
        )


async def k_largest_fanin_cone(ctx: Ctx, o: Outcome):
    r = await ctx.call("output_cone_stats", metrics=["cone_size"])
    o.answer = r["answer"]
    o.tool = (r["cone_size"]["max_size"], r["cone_size"]["largest_outputs"])
    sizes = {out: len(ctx.oracle.fanin_cone(out)[0]) for out in ctx.oracle.outputs}
    top = max(sizes.values())
    o.oracle = (top, nsorted(k for k, v in sizes.items() if v == top))
    if ctx.recipes:
        rows = [l.split() for l in await ctx.tcl(
            'foreach p [all_outputs] { puts "[get_full_name $p] [llength [inst_names [get_fanin -to $p -flat -only_cells] comb]]" }'
        )]
        best = max(int(r[1]) for r in rows)
        o.recipe = (best, nsorted(r[0] for r in rows if int(r[1]) == best))


async def _fanout_recipe(ctx: Ctx, node: str) -> list[str]:
    if node in ctx.oracle.gates:
        src = f"[get_nets -of_objects [get_pins {node}/{ctx.out_pin(node)}]]"
    else:
        src = f"[get_nets {{{node}}}]"
    return nsorted(await ctx.tcl(
        f'putl [inst_names [get_cells -of_objects [get_pins -of_objects {src} -filter "direction==input"]]]'
    ))


async def k_fanout_signal(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("fanout", name=node, limit=1_000_000)
    o.answer, o.tool = r["answer"], r["gates"]
    o.oracle = ctx.oracle.fanout(node)["gates"]
    if ctx.recipes:
        o.recipe = await _fanout_recipe(ctx, node)


async def k_fanout_count(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("fanout", name=node, limit=1_000_000)
    o.answer, o.tool = r["answer"], r["gate_count"]
    o.oracle = len(ctx.oracle.fanout(node)["gates"])
    if ctx.recipes:
        o.recipe = len(await _fanout_recipe(ctx, node))


async def k_highest_fanout_input(ctx: Ctx, o: Outcome):
    r = await ctx.call("fanout_ranking", scope="inputs")
    o.answer, o.tool = r["answer"], (r["max_fanout"], r["leaders"])
    ranking = ctx.oracle.fanout_ranking("inputs")
    top = ranking[0][1]
    o.oracle = (top, nsorted(n for n, k in ranking if k == top))
    if ctx.recipes:
        rows = [l.split() for l in await ctx.tcl(
            "set rows {}\n"
            "foreach n [get_nets *] { if {[get_full_name $n] in {one_ zero_}} continue\n"
            "  if {![llength [get_ports -quiet [get_full_name $n]]]} continue\n"
            '  lappend rows [list [get_full_name $n] [llength [get_pins -quiet -of_objects $n -filter "direction==input"]]] }\n'
            "putl [lrange [lsort -integer -decreasing -index 1 $rows] 0 4]"
        )]
        best = int(rows[0][1])
        o.recipe = (best, nsorted(r[0] for r in rows if int(r[1]) == best))


async def k_depth_cone(ctx: Ctx, o: Outcome, node: str):
    r = await ctx.call("logic_depth", to_node=node)
    o.answer = r["answer"]
    o.tool = r["depth"] if r["exists"] else -1
    o.oracle = ctx.oracle.depth_to(node)
    if ctx.recipes:
        o.recipe = int((await ctx.tcl(f"puts [gdepth -to [node {{{node}}}]]"))[-1])


async def k_depth_between(ctx: Ctx, o: Outcome, src: str, dst: str):
    r = await ctx.call("logic_depth", to_node=dst, from_node=src)
    o.answer = r["answer"]
    o.tool = r["depth"] if r["exists"] else -1
    o.oracle = ctx.oracle.depth_between(src, dst)
    if ctx.recipes:
        lines = await ctx.tcl(f"report_checks -unconstrained -from [node {{{src}}}] -to [node {{{dst}}}]")
        a = arrival(lines)
        o.recipe = a - 1 if a > 0 and first_point_is_register(lines) else a


async def k_outputs_depth_gt(ctx: Ctx, o: Outcome, threshold: int):
    r = await ctx.call("output_cone_stats", metrics=["depth"], threshold=threshold)
    o.answer, o.tool = r["answer"], r["depth"]["count_over_threshold"]
    o.oracle = sum(1 for d in ctx.oracle.output_depths().values() if d > threshold)
    if ctx.recipes:
        o.recipe = int((await ctx.tcl(
            f"set k 0\nforeach p [all_outputs] {{ if {{[gdepth -to $p] > {threshold}}} {{ incr k }} }}\nputs $k"
        ))[-1])


async def k_deepest_output(ctx: Ctx, o: Outcome):
    r = await ctx.call("output_cone_stats", metrics=["depth"])
    o.answer, o.tool = r["answer"], (r["depth"]["max_depth"], r["depth"]["deepest_outputs"])
    depths = ctx.oracle.output_depths()
    top = max(depths.values())
    o.oracle = (top, nsorted(k for k, v in depths.items() if v == top))
    if ctx.recipes:
        rows = [l.split() for l in await ctx.tcl('foreach p [all_outputs] { puts "[get_full_name $p] [gdepth -to $p]" }')]
        best = max(int(r[1]) for r in rows)
        o.recipe = (best, nsorted(r[0] for r in rows if int(r[1]) == best))


async def _summary(ctx: Ctx, o: Outcome, group: str):
    r = await ctx.call("depth_summary")
    o.answer = r["answer"]
    o.tool = r[group]["depth"] if r[group] else None
    o.oracle = ctx.oracle.depth_summary()[group]


async def k_depth_pi_po(ctx: Ctx, o: Outcome):
    await _summary(ctx, o, "pi_to_po")
    if ctx.recipes:
        a = arrival(await ctx.tcl("report_checks -unconstrained -from [all_inputs] -to [all_outputs]"))
        o.recipe = a if a >= 0 else None


async def k_depth_design(ctx: Ctx, o: Outcome):
    await _summary(ctx, o, "overall")
    if ctx.recipes:
        v = int((await ctx.tcl("puts [gdepth -to [concat [all_outputs] [all_registers -data_pins]]]"))[-1])
        o.recipe = v if v >= 0 else None


async def k_depth_pi_reg(ctx: Ctx, o: Outcome):
    await _summary(ctx, o, "pi_to_reg")
    if ctx.recipes:
        a = arrival(await ctx.tcl("report_checks -unconstrained -from [all_inputs] -to [all_registers -data_pins]"))
        o.recipe = a if a >= 0 else None


async def k_depth_reg_reg(ctx: Ctx, o: Outcome):
    await _summary(ctx, o, "reg_to_reg")
    if ctx.recipes:
        a = arrival(await ctx.tcl(
            "report_checks -unconstrained -from [all_registers -clock_pins] -to [all_registers -data_pins]"
        ))
        o.recipe = a - 1 if a >= 0 else None


async def k_critical_member(ctx: Ctx, o: Outcome, gate: str):
    r = await ctx.call("depth_summary", through_gate=gate)
    o.answer, o.tool = r["answer"], r["through"]["on_max_depth_path"]
    o.oracle = ctx.oracle.on_max_depth_path(gate)
    if ctx.recipes:
        lines = await ctx.tcl(
            "set ends [concat [all_outputs] [all_registers -data_pins]]\n"
            "set L [gdepth -to $ends]\n"
            f"set G [gdepth -through [get_pins {gate}/{ctx.out_pin(gate)}] -to $ends]\n"
            'puts [expr {$G == $L ? "yes" : "no"}]'
        )
        o.recipe = lines[-1] == "yes"


async def k_path(ctx: Ctx, o: Outcome, src: str, dst: str, avoid: list[str] | None = None):
    avoid = avoid or []
    r = await ctx.call("path_exists", from_node=src, to_node=dst, avoid=avoid)
    o.answer, o.tool = r["answer"], r["exists"]
    o.oracle = ctx.oracle.path_exists(src, dst, avoid)
    if ctx.recipes:
        disable = "".join(f"set_disable_timing [get_cells -of_objects [node {{{a}}}]]\n" for a in avoid)
        enable = "".join(f"unset_disable_timing [get_cells -of_objects [node {{{a}}}]]\n" for a in avoid)
        lines = await ctx.tcl(
            disable
            + f"set rc [catch {{report_checks -unconstrained -from [node {{{src}}}] -to [node {{{dst}}}]}} out]\n"
            + enable
            + "if {$rc} { error $out }"
        )
        o.recipe = arrival(lines) >= 0


async def k_dominance(ctx: Ctx, o: Outcome, src: str, dst: str, gate: str):
    r = await ctx.call("articulation_points", from_node=src, to_node=dst, gate=gate)
    o.answer = r["answer"]
    o.tool = r["gate_check"]["every_path_passes"] if r["path_exists"] else "no path"
    if not ctx.oracle.reaches(src, dst):
        o.oracle = "no path"
    else:
        o.oracle = not ctx.oracle.reaches(src, dst, frozenset([gate]))
    if ctx.recipes:
        before = arrival(await ctx.tcl(f"report_checks -unconstrained -from [node {{{src}}}] -to [node {{{dst}}}]"))
        if before < 0:
            o.recipe = "no path"
        else:
            after = arrival(await ctx.tcl(
                f"set_disable_timing [get_cells {gate}]\n"
                f"set rc [catch {{report_checks -unconstrained -from [node {{{src}}}] -to [node {{{dst}}}]}} out]\n"
                f"unset_disable_timing [get_cells {gate}]\nif {{$rc}} {{ error $out }}"
            ))
            o.recipe = after < 0


async def k_artic(ctx: Ctx, o: Outcome, src: str, dst: str):
    r = await ctx.call("articulation_points", from_node=src, to_node=dst)
    o.answer = r["answer"]
    o.tool = r["articulation_points"] if r["path_exists"] else "no path"
    res = ctx.oracle.articulation_points(src, dst)
    o.oracle = "no path" if res is None else res
    if ctx.recipes:
        lines = await ctx.tcl(f"putl [artic_points {{{src}}} {{{dst}}}]")
        o.recipe = "no path" if any("no combinational path" in l for l in lines) else nsorted(lines)


async def k_cut(ctx: Ctx, o: Outcome, wire: str):
    r = await ctx.call("cut_analysis", wire=wire, limit=1_000_000)
    o.answer = r["answer"]
    if not r["complete"]:
        o.notes.append("cut_analysis hit its time budget")
    o.tool = sorted(r["pairs"])
    o.oracle = sorted(ctx.oracle.cut_pairs(wire))
    if ctx.recipes:
        o.recipe = sorted(await ctx.tcl(f"putl [cut_pairs {{{wire}}}]"))


async def k_rename(ctx: Ctx, o: Outcome, what: str, old: str, new: str):
    kind = "instance" if old in ctx.oracle.gates else "net"
    r = await ctx.call("rename_object", old_name=old, new_name=new, kind="gate" if kind == "instance" else "net")
    o.answer = r["answer"]
    ctx.oracle.rename(kind, old, new)
    o.tool = (r["new_name"], r["visible_to_timing"], len(r["connections"]))
    pins = len(ctx.oracle.gate_pins(new)) if kind == "instance" else len(ctx.oracle.loads.get(new, [])) + (1 if new in ctx.oracle.driver else 0)
    o.oracle = (new, True, pins)
    if ctx.recipes:
        find = "findInst" if kind == "instance" else "findNet"
        lines = await ctx.tcl(
            f"set b [ord::get_db_block]\n"
            f'puts "[expr {{[$b {find} {new}] ne {{NULL}}}}] [expr {{[$b {find} {old}] eq {{NULL}}}}]"'
        )
        o.recipe = (new, lines[-1] == "1 1", pins)


async def k_gate_info(ctx: Ctx, o: Outcome, gate: str):
    r = await ctx.call("gate_info", name=gate)
    o.answer = r["answer"]
    o.tool = (r["cell_type"], {p["pin"]: p["net"] for p in r["pins"]})
    o.oracle = (ctx.oracle.gates[gate].cell, ctx.oracle.gate_pins(gate))
    if ctx.recipes:
        lines = await ctx.tcl(f"report_instance -connections {gate}")
        cell = next(l.split(":", 1)[1].strip() for l in lines if l.startswith("Cell:"))
        pins = {}
        for l in lines:
            m = re.match(r"^(\w+)\s+(input|output|inout)\s+(\S+)$", l)
            if m:
                pins[m.group(1)] = {"one_": "1'b1", "zero_": "1'b0"}.get(m.group(3), m.group(3))
        o.recipe = (cell, pins)


async def k_io_counts(ctx: Ctx, o: Outcome):
    r = await ctx.call("io_summary")
    o.answer, o.tool = r["answer"], (r["inputs"]["bits"], r["outputs"]["bits"])
    o.oracle = (len(ctx.oracle.inputs), len(ctx.oracle.outputs))
    if ctx.recipes:
        o.recipe = tuple(int(x) for x in (await ctx.tcl('puts "[llength [all_inputs]] [llength [all_outputs]]"'))[-1].split())


async def k_io_widths(ctx: Ctx, o: Outcome, direction: str):
    r = await ctx.call("io_summary", direction=direction)
    o.answer = r["answer"]
    o.tool = {p["name"]: p["width"] for p in r[direction + "s"]["ports"]}
    o.oracle = ctx.oracle.port_widths(direction)
    if ctx.recipes:
        cmd = "all_inputs" if direction == "input" else "all_outputs"
        o.recipe = tcl_dict((await ctx.tcl(
            "set w [dict create]\n"
            f'foreach p [{cmd}] {{ regsub {{\\[\\d+\\]$}} [get_full_name $p] "" b; dict incr w $b }}\nputs $w'
        ))[-1])


async def k_const_inputs(ctx: Ctx, o: Outcome, value: str, cell_type: str | None):
    args = {"value": value, "limit": 1_000_000}
    if cell_type:
        args["cell_type"] = cell_type
    r = await ctx.call("constant_inputs", **args)
    o.answer, o.tool = r["answer"], [g["gate"] for g in r["gates"]]
    o.oracle = ctx.oracle.constant_inputs(value, CELL[cell_type] if cell_type else None)
    if ctx.recipes:
        t = f" {CELL[cell_type]}" if cell_type else ""
        if value == "any":
            script = f"putl [lsort -unique [concat [const_loads 1{t}] [const_loads 0{t}]]]"
        else:
            script = f"putl [const_loads {value}{t}]"
        o.recipe = nsorted(await ctx.tcl(script))


async def k_list_gates(ctx: Ctx, o: Outcome, cell_type: str, pins: bool):
    r = await ctx.call("list_gates", cell_type=cell_type, include_pins=pins, limit=1_000_000)
    o.answer = r["answer"]
    cell = CELL[cell_type]
    if pins:
        o.tool = {g["name"]: g["pins"] for g in r["gates"]}
        o.oracle = {g: ctx.oracle.gate_pins(g) for g in ctx.oracle.gates_of_type(cell)}
    else:
        o.tool = [g["name"] for g in r["gates"]]
        o.oracle = ctx.oracle.gates_of_type(cell)
    if ctx.recipes:
        # The recipe lists names; with pins, matching names stand for the (netlist) pin data.
        names = nsorted(await ctx.tcl(f'putl [inst_names [get_cells -filter "ref_name=={cell}" *]]'))
        o.recipe = o.oracle if pins and names == list(o.oracle) else names


async def k_dffs_on_clock(ctx: Ctx, o: Outcome, clock: str):
    r = await ctx.call("list_gates", cell_type="DFF", clock_net=clock, include_pins=False, limit=1_000_000)
    o.answer, o.tool = r["answer"], [g["name"] for g in r["gates"]]
    o.oracle = ctx.oracle.dffs_on_clock(clock)
    if ctx.recipes:
        o.recipe = nsorted(await ctx.tcl(
            f'putl [inst_names [get_cells -of_objects [get_pins -of_objects [get_nets {{{clock}}}] -filter "direction==input && name==CLK"]]]'
        ))


async def k_floating(ctx: Ctx, o: Outcome):
    r = await ctx.call("floating_signals", limit=1_000_000)
    o.answer = r["answer"]
    o.tool = (r["unused_inputs"]["names"], r["undriven_outputs"]["names"])
    f = ctx.oracle.floating()
    o.oracle = (nsorted(f["unused_inputs"]), nsorted(f["undriven_outputs"]))
    if ctx.recipes:
        lines = await ctx.tcl(
            'set fi {}; foreach p [all_inputs]  { set n [get_full_name $p]; if {[llength [get_pins -quiet -of_objects [get_nets $n] -filter "direction==input"]] == 0} { lappend fi $n } }\n'
            'set fo {}; foreach p [all_outputs] { set n [get_full_name $p]; if {[llength [get_pins -quiet -of_objects [get_nets $n] -filter "direction==output"]] == 0} { lappend fo $n } }\n'
            'puts "unused inputs:"; putl $fi\nputs "undriven outputs:"; putl $fo'
        )
        split = lines.index("undriven outputs:")
        o.recipe = (nsorted(lines[1:split]), nsorted(lines[split + 1 :]))


KINDS = {name[2:]: fn for name, fn in globals().items() if name.startswith("k_")}


async def run_plan(ctx: Ctx, plan: Plan) -> Outcome:
    o = Outcome(plan)
    ctx.recipe_seconds = 0.0
    t0 = time.perf_counter()
    try:
        await KINDS[plan.kind](ctx, o, **plan.args)
    except Exception as exc:  # recorded per prompt; the test fails on it at the end
        o.error = f"{type(exc).__name__}: {exc}"
    o.recipe_seconds = ctx.recipe_seconds
    o.seconds = time.perf_counter() - t0 - o.recipe_seconds
    return o
