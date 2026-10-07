"""Exhaustive tool-vs-oracle checks on small netlists, with OpenROAD in WSL.

Unlike the prompt replay, these sweep every signal / pair of the fixtures, so they cover the
cases the contest prompts never hit: constants on gates (1'b0 and 1'b1), register-to-register
paths, cuts, floating signals and bus-bit names next to look-alike scalars.
"""

import asyncio
import itertools
from pathlib import Path

import pytest

from conftest import requires_openroad
from oracle import BUNDLE, Design, nsorted

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "netlists"
CASES = [FIXTURES / f"{n}.v" for n in ("seq_small", "const_mix", "cut_diamond", "floating", "bus_names")]
CASES.append(BUNDLE / "netlists" / "test04.v")

pytestmark = [requires_openroad, pytest.mark.openroad]


def run(coro):
    return asyncio.run(coro)


async def sweep(srv, path: Path, tmp: Path) -> list[str]:
    d = Design.from_file(path)
    bad: list[str] = []

    def check(label, got, want):
        if got != want:
            bad.append(f"{path.stem} {label}: tool={got!r:.300} oracle={want!r:.300}")

    try:
        r = await srv.load_design(str(path))
        check("cells", r["cells"], len(d.gates))
        check("warnings", r["warnings"], [])

        r = await srv.gate_counts()
        check("gate_counts", {k: v for k, v in r["by_library_cell"].items() if v}, dict(d.gate_counts()))
        r = await srv.io_summary()
        check("inputs", {p["name"]: p["width"] for p in r["inputs"]["ports"]}, d.port_widths("input"))
        check("outputs", {p["name"]: p["width"] for p in r["outputs"]["ports"]}, d.port_widths("output"))

        signals = nsorted(d.nets())
        for n in signals:
            r = await srv.fanout(n)
            check(f"fanout {n}", r["gates"], d.fanout(n)["gates"])
            r = await srv.fanin_cone(n, include_boundary_dffs=True)
            comb, dffs = d.fanin_cone(n)
            check(f"fanin {n}", (r["combinational_gates"], r["boundary_dffs"]["gates"]), (len(comb), nsorted(dffs)))
            if n in d.driver or n in d.pi_set:
                r = await srv.fanout_cone(n)
                comb, dffs, pos = d.fanout_cone(n)
                check(f"fanout_cone {n}", (r["gates"], r["output_ports"]), (nsorted(comb), nsorted(pos)))
            r = await srv.logic_depth(n)
            check(f"depth {n}", r["depth"] if r["exists"] else -1, d.depth_to(n))

        for g in nsorted(d.gates):
            r = await srv.gate_info(g)
            check(f"gate_info {g}", {p["pin"]: p["net"] for p in r["pins"]}, d.gate_pins(g))
            if g in d.driver.values() or d.out_nets(g):
                r = await srv.fanout(g)
                check(f"fanout gate {g}", r["gates"], d.fanout(g)["gates"])
            if not d.gates[g].seq:
                r = await srv.depth_summary(through_gate=g)
                check(f"on max path {g}", r["through"]["on_max_depth_path"], d.on_max_depth_path(g))

        starts = nsorted(d.inputs + [n for g in d.gates.values() if g.seq for n in g.outputs.values() if n])
        ends = nsorted(d.outputs + list(d.d_nets))
        for s, t in itertools.product(starts, ends):
            if t in d.outputs and t not in d.driver and t not in d.pi_set:
                continue  # undriven output
            r = await srv.path_exists(s, t)
            check(f"path {s}->{t}", r["exists"], d.reaches(s, t))
            r = await srv.logic_depth(t, s)
            check(f"depth {s}->{t}", r["depth"] if r["exists"] else -1, d.depth_between(s, t))
            r = await srv.articulation_points(s, t)
            want = d.articulation_points(s, t)
            check(f"artic {s}->{t}", r["articulation_points"] if r["path_exists"] else None, want)
            for g in nsorted(d.gates)[:12]:
                r = await srv.path_exists(s, t, [g])
                check(f"path {s}->{t} avoid {g}", r["exists"], d.path_exists(s, t, [g]))

        for n in signals:
            if n in d.driver or n in d.pi_set:
                r = await srv.cut_analysis(n)
                check(f"cut {n}", sorted(r["pairs"]), sorted(d.cut_pairs(n)))

        s = d.depth_summary()
        r = await srv.depth_summary()
        check("depth_summary", {k: (r[k]["depth"] if r[k] else None) for k in s}, s)
        r = await srv.output_cone_stats(["depth", "cone_size"], threshold=1)
        depths = d.output_depths()
        check("over 1", r["depth"]["count_over_threshold"], sum(1 for v in depths.values() if v > 1))

        for value in ("0", "1", "any"):
            r = await srv.constant_inputs(value)
            check(f"const {value}", [g["gate"] for g in r["gates"]], d.constant_inputs(value))
        r = await srv.floating_signals()
        f = d.floating()
        check("floating", (r["unused_inputs"]["names"], r["undriven_outputs"]["names"], r["dangling_nets"]["names"]),
              (f["unused_inputs"], f["undriven_outputs"], f["dangling_gate_outputs"]))
        r = await srv.fanout_ranking("nets", top_n=1000)
        check("ranking", {x["net"]: x["fanout"] for x in r["ranking"]}, {n: k for n, k in d.fanout_ranking("nets")})

        # Rename a gate and an internal net, then the written netlist must equal the edited oracle.
        gate = nsorted(d.gates)[0]
        internal = next((n for n in nsorted(d.driver) if n not in d.nl.port_bits), None)
        await srv.rename_object(gate, "renamed_gate")
        d.rename("instance", gate, "renamed_gate")
        if internal:
            r = await srv.rename_object(internal, "renamed_wire")
            check("rename visible", r["visible_to_timing"], True)
            d.rename("net", internal, "renamed_wire")
            r = await srv.fanout("renamed_wire")
            check("fanout renamed", r["gates"], d.fanout("renamed_wire")["gates"])
        out = tmp / f"{path.stem}_out.v"
        await srv.write_verilog(str(out))
        written = Design.from_file(out)
        check("written netlist", written.equivalent(d) or written.diff(d), True)
    finally:
        await srv.SESSION.close()
    return bad


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_tools_match_oracle(path, tmp_path, netlist_server):
    bad = run(sweep(netlist_server, path, tmp_path))
    assert not bad, "\n".join(bad[:40])


def test_bus_base_name_is_explained(netlist_server):
    async def scenario():
        try:
            await netlist_server.load_design(str(FIXTURES / "bus_names.v"))
            with pytest.raises(Exception, match=r"4-bit port.*n42\[0\] \.\. n42\[3\]"):
                await netlist_server.fanin_cone("n42")
            with pytest.raises(Exception, match="No port, net or gate named 'n9'"):
                await netlist_server.logic_depth("n9")
            r = await netlist_server.fanout("n420")
            assert r["gates"] == ["g0"] and r["load_pin_count"] == 1
        finally:
            await netlist_server.SESSION.close()

    run(scenario())


def test_constants_are_neutralized_and_restored(tmp_path, netlist_server):
    async def scenario():
        try:
            r = await netlist_server.load_design(str(FIXTURES / "const_mix.v"))
            assert r["constant_pins"] == {"1'b1": 3, "1'b0": 2} and r["constants_neutralized"] == 5
            # With STA constant propagation, y would be constant and unreachable.
            assert (await netlist_server.path_exists("a", "y"))["exists"]
            out = tmp_path / "c.v"
            r = await netlist_server.write_verilog(str(out))
            assert r["constants_restored"] == {"1'b1": 3, "1'b0": 2}
            assert Design.from_file(out).equivalent(Design.from_file(FIXTURES / "const_mix.v"))
            r = await netlist_server.load_design(str(FIXTURES / "const_mix.v"), neutralize_constants=False)
            assert not (await netlist_server.path_exists("a", "y"))["exists"]
        finally:
            await netlist_server.SESSION.close()

    run(scenario())
