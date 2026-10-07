"""Gate-level graph of a netlist and reference answers for every task category.

Semantics (shared with the mcp-netlist server, defined independently here):
- A gate is a cell instance; depth = number of gates on a path.
- Flip-flops are cone/path boundaries: a combinational path starts at a primary input or a
  DFF output and ends at a primary output or a DFF D input.
- Constants (1'b0 / 1'b1) are not path starts, but gates fed by them are traversed normally
  (purely structural, no logic simplification).
"""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

from oracle.liberty import LibCell, parse_liberty
from oracle.verilog import CONSTANTS, Netlist, parse_file

BUNDLE = Path(__file__).resolve().parents[2] / "openroad_bundle"
DEFAULT_LIB = BUNDLE / "unit_delay.lib"

NEG = None  # "no path" level


def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def nsorted(names) -> list[str]:
    return sorted(names, key=natural_key)


def port_base(bit: str) -> str:
    return re.sub(r"\[\d+\]$", "", bit)


@dataclass
class Gate:
    name: str
    cell: str
    inputs: dict[str, str | None]  # pin -> net / constant / None
    outputs: dict[str, str | None]
    seq: bool
    clock_pins: list[str]


class Design:
    def __init__(self, nl: Netlist, lib: dict[str, LibCell]):
        self.nl = nl
        self.lib = lib
        self.gates: dict[str, Gate] = {}
        for inst in nl.instances.values():
            lc = lib[inst.cell]
            ins = {p: inst.pins.get(p) for p in lc.inputs if p in inst.pins}
            outs = {p: inst.pins.get(p) for p in lc.outputs if p in inst.pins}
            self.gates[inst.name] = Gate(inst.name, inst.cell, ins, outs, lc.sequential, lc.clock_pins)
        self._cache: dict[str, dict] = {}
        self.inputs = [b for b, d in nl.port_bits.items() if d == "input"]
        self.outputs = [b for b, d in nl.port_bits.items() if d == "output"]
        self._index()

    @classmethod
    def from_file(cls, path, lib_path=DEFAULT_LIB, undeclared_const_nets: bool = False) -> Design:
        return cls(parse_file(path, undeclared_const_nets), parse_liberty(lib_path))

    # ------------------------------------------------------------------ indexing

    def _index(self) -> None:
        self.driver: dict[str, tuple[str, str]] = {}  # net -> (gate, pin)
        self.loads: dict[str, list[tuple[str, str]]] = {}
        for g in self.gates.values():
            for pin, net in g.outputs.items():
                if net and net not in CONSTANTS:
                    self.driver[net] = (g.name, pin)
            for pin, net in g.inputs.items():
                if net:
                    self.loads.setdefault(net, []).append((g.name, pin))
        self.pi_set = set(self.inputs)
        self.po_set = set(self.outputs)
        self.d_nets = set()
        for g in self.gates.values():
            if g.seq:
                net = g.inputs.get("D")
                if net and net not in CONSTANTS:
                    self.d_nets.add(net)
        self.endpoints = self.po_set | self.d_nets
        self._topo = self._topological_order()

    def nets(self) -> set[str]:
        out = set(self.driver) | set(self.loads) | self.pi_set | self.po_set
        return {n for n in out if n not in CONSTANTS}

    def comb(self, name: str) -> bool:
        return name in self.gates and not self.gates[name].seq

    def out_nets(self, gate: str) -> list[str]:
        return [n for n in self.gates[gate].outputs.values() if n]

    def _topological_order(self) -> list[str]:
        comb = [g for g in self.gates.values() if not g.seq]
        indeg = {g.name: 0 for g in comb}
        succ: dict[str, list[str]] = {g.name: [] for g in comb}
        for g in comb:
            for net in g.inputs.values():
                d = self.driver.get(net) if net else None
                if d and self.comb(d[0]):
                    indeg[g.name] += 1
                    succ[d[0]].append(g.name)
        queue = deque(n for n, k in indeg.items() if k == 0)
        order = []
        while queue:
            n = queue.popleft()
            order.append(n)
            for s in succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    queue.append(s)
        if len(order) != len(comb):
            raise ValueError("combinational loop")
        return order

    # ------------------------------------------------------------------ names

    def net_of(self, name: str) -> str:
        """Net for a signal name; a gate name stands for its output net."""
        if name in self.gates:
            outs = self.out_nets(name)
            if not outs:
                raise KeyError(f"gate {name} has no connected output")
            return outs[0]
        if name in self.nets():
            return name
        raise KeyError(f"unknown signal {name}")

    # ------------------------------------------------------------------ inventory

    def gate_counts(self) -> Counter:
        return Counter(g.cell for g in self.gates.values())

    def port_widths(self, direction: str) -> dict[str, int]:
        return {base: len(bits) for base, (d, bits) in self.nl.ports.items() if d == direction}

    def gates_of_type(self, cell: str) -> list[str]:
        return nsorted(g.name for g in self.gates.values() if g.cell == cell)

    def gate_pins(self, name: str) -> dict[str, str | None]:
        g = self.gates[name]
        return {**g.inputs, **g.outputs}

    def dffs_on_clock(self, net: str) -> list[str]:
        return nsorted(
            g.name for g in self.gates.values() if g.seq and any(g.inputs.get(p) == net for p in g.clock_pins)
        )

    # ------------------------------------------------------------------ fanout

    def fanout(self, name: str) -> dict:
        nets = self.out_nets(name) if name in self.gates else [name]
        loads = [l for n in nets for l in self.loads.get(n, [])]
        return {
            "nets": nets,
            "gates": nsorted({g for g, _ in loads}),
            "load_pins": len(loads),
            "output_ports": nsorted(n for n in nets if n in self.po_set),
        }

    def fanout_ranking(self, scope: str = "inputs") -> list[tuple[str, int]]:
        nets = self.inputs if scope == "inputs" else self.nets()
        rows = [(n, len(self.loads.get(n, []))) for n in nets]
        return sorted(rows, key=lambda r: (-r[1], natural_key(r[0])))

    # ------------------------------------------------------------------ cones

    def fanin_cone(self, name: str) -> tuple[set[str], set[str]]:
        """(combinational gates, boundary DFFs) in the transitive fanin of a signal."""
        start = self.net_of(name)
        comb, dffs, seen = set(), set(), {start}
        queue = deque([start])
        while queue:
            d = self.driver.get(queue.popleft())
            if not d:
                continue
            g = self.gates[d[0]]
            if g.seq:
                dffs.add(g.name)
                continue
            if g.name in comb:
                continue
            comb.add(g.name)
            for net in g.inputs.values():
                if net and net not in CONSTANTS and net not in seen:
                    seen.add(net)
                    queue.append(net)
        return comb, dffs

    def fanout_cone(self, name: str, removed: frozenset[str] = frozenset()) -> tuple[set[str], set[str], set[str]]:
        """(combinational gates, boundary DFFs, primary outputs) reachable from a signal."""
        start = self.net_of(name)
        comb, dffs, pos, seen = set(), set(), set(), {start}
        queue = deque([start])
        while queue:
            net = queue.popleft()
            if net in self.po_set:
                pos.add(net)
            for gname, _pin in self.loads.get(net, []):
                if gname in removed:
                    continue
                g = self.gates[gname]
                if g.seq:
                    dffs.add(gname)
                    continue
                if gname in comb:
                    continue
                comb.add(gname)
                for out in g.outputs.values():
                    if out and out not in seen:
                        seen.add(out)
                        queue.append(out)
        if name in self.gates:
            comb.discard(name)
        return comb, dffs, pos

    def reaches(self, src: str, dst: str, removed: frozenset[str] = frozenset()) -> bool:
        s, t = self.net_of(src), self.net_of(dst)
        if s == t:
            return True
        seen, queue = {s}, deque([s])
        while queue:
            net = queue.popleft()
            for gname, _pin in self.loads.get(net, []):
                if gname in removed or not self.comb(gname):
                    continue
                for out in self.gates[gname].outputs.values():
                    if out == t:
                        return True
                    if out and out not in seen:
                        seen.add(out)
                        queue.append(out)
        return False

    # ------------------------------------------------------------------ depth

    def levels(self, seeds: dict[str, int]) -> dict[str, int]:
        """Longest gate count from any seed net to every net (absent = unreachable)."""
        lv = dict(seeds)
        for gname in self._topo:
            g = self.gates[gname]
            best = None
            for net in g.inputs.values():
                v = lv.get(net) if net else None
                if v is not None and (best is None or v > best):
                    best = v
            if best is not None:
                for out in g.outputs.values():
                    if out and (lv.get(out) is None or lv[out] < best + 1):
                        lv[out] = best + 1
        return lv

    def pi_levels(self) -> dict[str, int]:
        if "pi" not in self._cache:
            self._cache["pi"] = self.levels({n: 0 for n in self.inputs})
        return self._cache["pi"]

    def reg_levels(self) -> dict[str, int]:
        if "reg" not in self._cache:
            seeds = {n: 0 for g in self.gates.values() if g.seq for n in g.outputs.values() if n}
            self._cache["reg"] = self.levels(seeds)
        return self._cache["reg"]

    def depth_to(self, name: str) -> int:
        """Max depth of a signal's cone from any PI or DFF output; -1 if none."""
        net = self.net_of(name)
        vals = [v for v in (self.pi_levels().get(net), self.reg_levels().get(net)) if v is not None]
        return max(vals) if vals else -1

    def depth_between(self, src: str, dst: str) -> int:
        lv = self.levels({self.net_of(src): 0})
        v = lv.get(self.net_of(dst))
        return -1 if v is None else v

    @staticmethod
    def _max(lv: dict[str, int], nets) -> int | None:
        vals = [lv[n] for n in nets if lv.get(n) is not None]
        return max(vals) if vals else None

    def depth_summary(self) -> dict[str, int | None]:
        pi, reg = self.pi_levels(), self.reg_levels()
        groups = {
            "pi_to_po": self._max(pi, self.po_set),
            "pi_to_reg": self._max(pi, self.d_nets),
            "reg_to_reg": self._max(reg, self.d_nets),
            "reg_to_po": self._max(reg, self.po_set),
        }
        vals = [v for v in groups.values() if v is not None]
        groups["overall"] = max(vals) if vals else None
        return groups

    def output_depths(self) -> dict[str, int]:
        return {o: self.depth_to(o) for o in self.outputs}

    def down_levels(self) -> dict[str, int]:
        """Longest gate count from each net to any endpoint (absent = none reachable)."""
        if "down" in self._cache:
            return self._cache["down"]
        down: dict[str, int] = {}

        def value(net: str) -> int | None:
            best = 0 if net in self.endpoints else None
            for gname, _pin in self.loads.get(net, []):
                if not self.comb(gname):
                    continue
                for out in self.gates[gname].outputs.values():
                    v = down.get(out) if out else None
                    if v is not None and (best is None or v + 1 > best):
                        best = v + 1
            return best

        for gname in reversed(self._topo):
            for out in self.gates[gname].outputs.values():
                if out:
                    v = value(out)
                    if v is not None:
                        down[out] = v
        for net in self.nets():
            if net not in down:
                v = value(net)
                if v is not None:
                    down[net] = v
        self._cache["down"] = down
        return down

    def on_max_depth_path(self, gate: str) -> bool:
        overall = self.depth_summary()["overall"]
        down = self.down_levels().get(self.net_of(gate))
        if overall is None or down is None:
            return False
        return self.depth_to(gate) >= 0 and self.depth_to(gate) + down == overall

    # ------------------------------------------------------------------ paths & cuts

    def avoid_gates(self, names) -> frozenset[str]:
        out = set()
        for n in names:
            if n in self.gates:
                out.add(n)
            elif n in self.driver:
                out.add(self.driver[n][0])
        return frozenset(out)

    def path_exists(self, src: str, dst: str, avoid=()) -> bool:
        if any(a not in self.gates and a == self.net_of(src) for a in avoid):
            return False  # avoiding the start signal itself
        return self.reaches(src, dst, self.avoid_gates(avoid))

    def articulation_points(self, src: str, dst: str) -> list[str] | None:
        """Gates whose removal disconnects src from dst; None if there is no path at all."""
        if not self.reaches(src, dst):
            return None
        a = self.fanout_cone(src)[0]
        b = self.fanin_cone(dst)[0]
        return nsorted(g for g in a & b if not self.reaches(src, dst, frozenset([g])))

    def pi_fanin(self, name: str) -> set[str]:
        """Primary inputs with a combinational path to a signal."""
        start = self.net_of(name)
        pis, seen, queue = set(), {start}, deque([start])
        while queue:
            net = queue.popleft()
            if net in self.pi_set:
                pis.add(net)
            d = self.driver.get(net)
            if not d or not self.comb(d[0]):
                continue
            for n in self.gates[d[0]].inputs.values():
                if n and n not in CONSTANTS and n not in seen:
                    seen.add(n)
                    queue.append(n)
        return pis

    def cut_pairs(self, wire: str) -> list[str]:
        """'pi->po' pairs that are connected but lose every path when the wire is blocked."""
        net = self.net_of(wire)
        pairs = []
        if net in self.pi_set:
            pos = self.fanout_cone(net)[2]
            return nsorted(f"{net}->{q}" for q in pos if q != net)
        drv = self.driver.get(net)
        removed = frozenset([drv[0]]) if drv else frozenset()
        for p in nsorted(self.pi_fanin(net)):
            before = self.fanout_cone(p)[2]
            after = self.fanout_cone(p, removed)[2]
            pairs += [f"{p}->{q}" for q in nsorted(before - after) if q != net]
        return pairs

    # ------------------------------------------------------------------ checks

    def constant_inputs(self, value: str = "any", cell: str | None = None) -> list[str]:
        wanted = {"0": {"1'b0"}, "1": {"1'b1"}, "any": set(CONSTANTS)}[value]
        return nsorted(
            g.name
            for g in self.gates.values()
            if (cell is None or g.cell == cell) and any(n in wanted for n in g.inputs.values())
        )

    def floating(self) -> dict[str, list[str]]:
        return {
            "unused_inputs": [i for i in self.inputs if not self.loads.get(i) and i not in self.po_set],
            "undriven_outputs": [o for o in self.outputs if o not in self.driver and o not in self.pi_set],
            "dangling_gate_outputs": nsorted(
                n for n in self.driver if not self.loads.get(n) and n not in self.po_set
            ),
            "undriven_nets": nsorted(
                n for n in self.loads if n not in CONSTANTS and n not in self.driver and n not in self.pi_set
            ),
        }

    # ------------------------------------------------------------------ edits & equivalence

    def rename(self, kind: str, old: str, new: str) -> None:
        if kind == "instance":
            inst = self.nl.instances.pop(old)
            inst.name = new
            self.nl.instances[new] = inst
        else:
            if old in self.nl.port_bits:
                raise ValueError("refusing to rename a port net")
            for inst in self.nl.instances.values():
                for pin, net in inst.pins.items():
                    if net == old:
                        inst.pins[pin] = new
            self.nl.wires.discard(old)
            self.nl.wires.add(new)
        self.__init__(self.nl, self.lib)

    def canonical(self) -> tuple[dict, dict]:
        ports = dict(self.nl.port_bits)
        cells = {
            name: (inst.cell, tuple(sorted((p, n) for p, n in inst.pins.items() if n is not None)))
            for name, inst in self.nl.instances.items()
        }
        return ports, cells

    def equivalent(self, other: Design) -> bool:
        return self.canonical() == other.canonical()

    def diff(self, other: Design, limit: int = 5) -> list[str]:
        (pa, ca), (pb, cb) = self.canonical(), other.canonical()
        out = []
        if pa != pb:
            out.append(f"ports differ: {sorted(set(pa.items()) ^ set(pb.items()))[:limit]}")
        for name in nsorted(set(ca) | set(cb)):
            if ca.get(name) != cb.get(name):
                out.append(f"{name}: {ca.get(name)} != {cb.get(name)}")
                if len(out) >= limit:
                    break
        return out
