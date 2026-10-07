"""Map each natural-language prompt of openroad_tasks.xlsx to a check plan.

A plan is a check kind plus the names extracted from the prompt text. tests/tasks/checks.py
turns a plan into the MCP tool call(s), the oracle answer and the xlsx recipe.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASKS_JSON = Path(__file__).resolve().parent / "openroad_tasks.json"

N = r"([A-Za-z_][\w$]*(?:\[\d+\])?)"  # a signal / gate name
CELL = r"(AND|OR|NOT|NAND|NOR|XOR|XNOR|BUF|DFF)"


@dataclass
class Plan:
    case: str
    no: int
    prompt: str
    category: str
    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.case}#{self.no} [{self.category}] {self.kind}"


# (category, regex, kind, arg names or a function of the match)
RULES: list[tuple[str, str, str, Any]] = [
    ("Testcase Header", r"^This is the beginning of a new testcase\. The case name is (\w+)\.$", "header", ["case"]),
    ("Load Design", r"^Please load the design from the file ([\w.]+) located in the directory (\S+?)/?\.$", "load",
     lambda m: {"netlist": f"{m.group(2)}/{m.group(1)}"}),
    ("Write Output", r"^Please write the current design to the output file ([\w.]+)\.$", "write", ["path"]),
    ("Gate Count", r"^Please count all the gates in this design and report the total count broken down by gate type", "gate_counts", []),
    ("Gate Count", r"^Compute the total gate count of the design\.$", "gate_total", []),
    ("Gate Count", rf"^How many {CELL} gates are currently in the design\?$", "gate_count_type", ["cell_type"]),
    ("Fanin Cone Size", rf"^How many gates are in the fanin cone of primary output {N}\?$", "fanin_size", ["node"]),
    ("Logic Cone Size", rf"^How many gates are in the logic cone of output {N}\?$", "fanin_size", ["node"]),
    ("Logic Depth", rf"^Compute the maximum logic depth of the fanin cone of output {N}\.$", "depth_cone", ["node"]),
    ("Logic Depth", rf"^What is the depth of the cone of {N} now\?$", "depth_cone", ["node"]),
    ("Logic Depth", rf"^Compute the maximum logic depth from input {N} to output {N}\.$", "depth_between", ["src", "dst"]),
    ("Logic Depth", rf"^Determine the longest combinational path depth from {N} to {N}\.$", "depth_between", ["src", "dst"]),
    ("Logic Depth", rf"^Calculate the critical path depth between {N} and {N}\.$", "depth_between", ["src", "dst"]),
    ("Logic Depth", r"^How many outputs have a logic depth greater than (\d+)\?$", "outputs_depth_gt",
     lambda m: {"threshold": int(m.group(1))}),
    ("Logic Depth", r"^What is the maximum combinational depth from any primary input to any primary output", "depth_pi_po", []),
    ("Logic Depth", r"^Which output bit has the deepest fanin logic cone\?$", "deepest_output", []),
    ("Logic Depth", r"^What is the maximum combinational logic depth in the design now\?$", "depth_design", []),
    ("Logic Depth (Sequential)", r"from any primary input to any DFF D-pin", "depth_pi_reg", []),
    ("Logic Depth (Sequential)", r"on any register-to-register path", "depth_reg_reg", []),
    ("Fanout Analysis", rf"^What is the fanout of primary input {N}\? List (?:all|every) gates? that \1 drives directly\.$", "fanout_signal", ["node"]),
    ("Fanout Analysis", rf"^Determine the number of gates driven by {N}\.$", "fanout_count", ["node"]),
    ("Fanout Analysis", rf"^Enumerate the immediate successors of gate {N}\.$", "fanout_signal", ["node"]),
    ("Fanout Analysis", rf"^Report every gate connected to the output of {N}\.$", "fanout_signal", ["node"]),
    ("Fanout Analysis", rf"^List all gates that now connect to the renamed signal {N}\.$", "fanout_signal", ["node"]),
    ("Fanout Analysis", r"^Which primary input has the highest fanout in this design\?$", "highest_fanout_input", []),
    ("Fanout Analysis", rf"^What is the maximum fanout of {N} now\?$", "fanout_count", ["node"]),
    ("Path Existence", rf"^Determine whether a combinational path from {N} to {N} exists that does not traverse node {N}\.$", "path", ["src", "dst", "avoid"]),
    ("Path Existence", rf"^Verify whether a path connecting input {N} to output {N} exists while avoiding {N}\.$", "path", ["src", "dst", "avoid"]),
    ("Path Existence", rf"^Does a combinational path from {N} to {N} exist that avoids {N}\?$", "path", ["src", "dst", "avoid"]),
    ("Path Existence", rf"^Does a combinational path exist from primary input {N} to primary output {N}\? Report yes or no\.$", "path", ["src", "dst"]),
    ("Fanin Cone", rf"^Compute the transitive fanin cone of output {N}\.$", "fanin_cone", ["node"]),
    ("Fanin Cone", rf"^Compute the fanin logic cone of output {N} and list all gates that contribute to this output\.$", "fanin_cone", ["node"]),
    ("Fanin Cone", r"^Which output has the largest fanin cone\?$", "largest_fanin_cone", []),
    ("Fanout Cone", rf"^Compute the transitive fanout cone of input {N}\.$", "fanout_cone", ["node"]),
    ("Fanout Cone", rf"^What is the transitive fanout of primary input {N}\? List all gates reachable from \1\.$", "fanout_cone", ["node"]),
    ("Reachability", rf"^Determine all gates reachable from {N}\.$", "fanout_cone", ["node"]),
    ("Renaming", rf"^(?:Rename|Change the identifier of) (gate|wire) {N} to {N}\b", "rename", ["what", "old", "new"]),
    ("Renaming", rf"^(?:Try to rename internal signal|Update the name of signal) {N} to {N}\b", "rename",
     lambda m: {"what": "signal", "old": m.group(1), "new": m.group(2)}),
    ("Fanin Cone Intersection", rf"^Report all gates shared between the fanin cones of {N} and {N}\.$", "cone_intersection", ["a", "b"]),
    ("Gate Inspection", rf"^What type of gate is {N}\? Report its gate type and pin connections\.$", "gate_info", ["gate"]),
    ("Critical Path Membership", rf"^Determine whether gate {N} lies on any maximum-depth path", "critical_member", ["gate"]),
    ("I/O Summary", r"^(?:Determine the number of primary inputs and outputs|How many primary inputs and primary outputs does this design have)", "io_counts", []),
    ("I/O Summary", r"^Please list all the primary inputs of this design with their bit widths\.$", "io_widths",
     lambda m: {"direction": "input"}),
    ("I/O Summary", r"^List all primary outputs of this design with their bit widths\.$", "io_widths",
     lambda m: {"direction": "output"}),
    ("Path Dominance", rf"^Does every path from input {N} to output {N} pass through gate {N}\?", "dominance", ["src", "dst", "gate"]),
    ("Constant Input Detection", rf"^Report any {CELL} gates with constant inputs(?: \(0 or 1\))? in this design\.$", "const_inputs",
     lambda m: {"value": "any", "cell_type": m.group(1)}),
    ("Constant Input Detection", rf"^Report any {CELL} gates with a constant ([01]) input in this design\.$", "const_inputs",
     lambda m: {"value": m.group(2), "cell_type": m.group(1)}),
    ("Constant Input Detection", r"^List all gates with one or more inputs tied to 1'b([01])\.$", "const_inputs",
     lambda m: {"value": m.group(1), "cell_type": None}),
    ("Cut Analysis", rf"^Determine whether wire {N} is a cut between any primary input and any primary output", "cut", ["wire"]),
    ("Cut Analysis", rf"^Find all articulation points in the combinational graph between {N} and {N}\.$", "artic", ["src", "dst"]),
    ("Gate Listing", rf"^List all {CELL} gates in this design( with their input and output signals)?\.$", "list_gates",
     lambda m: {"cell_type": m.group(1), "pins": bool(m.group(2))}),
    ("Sequential Element Listing", rf"^List all flip-flops driven by clock {N}\.$", "dffs_on_clock", ["clock"]),
    ("Gate Count (Cone)", rf"^Report the number of each gate type in the cone of {N}\.$", "cone_type_counts", ["node"]),
    ("Floating Signal Detection", r"^Check if there are any floating inputs or unconnected output ports", "floating", []),
]


def load_tasks(path: Path = TASKS_JSON) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def plan_for(row: dict[str, Any]) -> Plan:
    """The single plan for one prompt row; raises if no rule (or several rules) match."""
    matches = []
    for category, pattern, kind, spec in RULES:
        if category != row["category"]:
            continue
        m = re.search(pattern, row["prompt"].strip())
        if not m:
            continue
        if callable(spec):
            args = spec(m)
        else:
            args = dict(zip(spec, m.groups()))
            if "avoid" in args:
                args["avoid"] = [args["avoid"]]
        matches.append(Plan(row["case"], row["no"], row["prompt"], row["category"], kind, args))
    if len(matches) != 1:
        raise ValueError(f"{len(matches)} plans match {row['case']}#{row['no']}: {row['prompt']!r}")
    return matches[0]


def all_plans() -> list[Plan]:
    return [plan_for(row) for row in load_tasks()["prompts"]]


def plans_by_case() -> dict[str, list[Plan]]:
    out: dict[str, list[Plan]] = {}
    for p in all_plans():
        out.setdefault(p.case, []).append(p)
    return out
