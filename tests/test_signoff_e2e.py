"""mcp-signoff end to end on real OpenROAD (WSL).

Routed gcd designs are built through the stage servers (place -> opt -> route, see test_route_e2e),
then signed off. Reference: this OpenROAD binary's own run of test/flow.tcl, whose final report
(after OpenRCX extraction) gives
  nangate45: setup WNS -0.029 ns, TNS -0.241 ns, hold WNS 0.046 ns, skew 0.003 ns, power 2.38 mW;
  sky130hd:  setup WNS -0.538 ns, hold WNS 0.483 ns.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import requires_openroad
from test_route_e2e import TEST_DIR, _cts

pytestmark = [pytest.mark.openroad, requires_openroad]

REFERENCE = {
    "nangate45": {"setup": -0.029, "tns": -0.241, "hold": 0.046, "power_w": 2.38e-3, "tol": 0.006},
    "sky130hd": {"setup": -0.538, "tns": None, "hold": 0.483, "power_w": None, "tol": 0.05},
}
LIBERTY = {"nangate45": f"{TEST_DIR}/Nangate45/Nangate45_typ.lib", "sky130hd": f"{TEST_DIR}/sky130hd/sky130hd_tt.lib"}
SKY130_FILL_RULES = "/home/ansh/OpenROAD/src/fin/test/fill.json"


def _sc(result):
    assert not getattr(result, "is_error", False), result.content[0].text
    return result.structured_content


async def _call(server, tool, **kwargs):
    return _sc(await server.mcp.call_tool(tool, kwargs))


async def _error(server, tool, **kwargs) -> str:
    with pytest.raises(ToolError) as exc:
        await server.mcp.call_tool(tool, kwargs)
    return str(exc.value)


@pytest.fixture(scope="module")
def routed():
    """Routed checkpoints, one per platform, made by the place, opt and route servers."""
    import mcp_route.server as route

    async def build(platform: str) -> str:
        await _cts(platform, f"pytest_signoff_{platform}_cts")
        try:
            await _call(route, "load_checkpoint", name_or_path=f"pytest_signoff_{platform}_cts")
            result = await _call(route, "route_design")
            assert result["drc_violations"] == 0
            await _call(route, "save_checkpoint", name=f"pytest_signoff_{platform}_routed", overwrite=True)
        finally:
            await route.SESSION.close()
        return f"pytest_signoff_{platform}_routed"

    return {p: asyncio.run(build(p)) for p in ("nangate45", "sky130hd")}


@pytest.mark.parametrize("platform", ["nangate45", "sky130hd"])
def test_signoff_flow(routed, platform):
    import mcp_route.server as route
    import mcp_signoff.server as so
    import mcp_sta.server as sta

    ref = REFERENCE[platform]

    async def scenario():
        try:
            await _call(so, "load_checkpoint", name_or_path=routed[platform])
            estimated = await _call(so, "signoff_timing")
            assert "extract_parasitics" in estimated["note"]

            ex = await _call(so, "extract_parasitics")
            assert ex["summary"]["nets"] > 0 and ex["summary"]["coupling_caps"] > 0
            assert ex["summary"]["spef"].endswith(f"gcd_{platform}/gcd.spef")
            assert ex["after"]["parasitics"].startswith("extracted")

            timing = await _call(so, "signoff_timing", paths=3)
            assert "note" not in timing
            assert timing["setup"]["worst_slack"] == pytest.approx(ref["setup"], abs=ref["tol"])
            assert timing["hold"]["worst_slack"] == pytest.approx(ref["hold"], abs=ref["tol"])
            assert timing["hold"]["status"] == "MET" and timing["setup"]["status"] == "VIOLATED"
            if ref["tns"] is not None:
                assert timing["setup"]["tns"] == pytest.approx(ref["tns"], abs=0.02)
            assert len(timing["worst_endpoints"]["setup"]) == 3
            assert timing["worst_endpoints"]["setup"][0]["slack"] == pytest.approx(timing["setup"]["worst_slack"], abs=0.002)
            assert abs(timing["clock_skew"]["core_clock"]["setup"]) < 0.05
            assert timing["clock_min_period"]["core_clock"]["fmax_mhz"] > 0

            power = await _call(so, "power_analysis")
            assert power["total_w"] > 0 and {"sequential", "combinational", "clock"} <= set(power["groups"])
            if ref["power_w"] is not None:
                assert power["total_w"] == pytest.approx(ref["power_w"], rel=0.03)

            grid = await _call(so, "check_power_grid")
            assert grid["all_connected"] and {n["net"] for n in grid["nets"]} == {"VDD", "VSS"}
            ir = await _call(so, "analyze_ir_drop", worst_instances=3, enable_em=True)
            assert len(ir["reports"]) == 2 and 0 <= ir["worst_drop_pct"] < 5
            vdd = next(r for r in ir["reports"] if r["net"] == "VDD")
            assert vdd["supply_voltage_v"] > 0 and len(vdd["worst_instances"]) == 3 and vdd["em_resistors"] > 0
            assert vdd["worst_instances"][0]["voltage_v"] <= vdd["worst_instances"][-1]["voltage_v"]

            checklist = await _call(so, "signoff_checklist")
            results = {i["item"]: i["result"] for i in checklist["items"]}
            for item in ("routed", "drc", "antennas", "placement", "floating_nets", "hold", "extracted_parasitics",
                         "power_grid_VDD", "power_grid_VSS", "ir_drop_VDD", "ir_drop_VSS"):
                assert results[item] == "PASS", (item, checklist)
            # The reference design misses setup by a few ps (it does in OpenROAD's own flow too).
            assert results["setup"] == "FAIL" and checklist["verdict"] == "FAIL"

            html = await _call(so, "timing_report_html", setup_paths=5, hold_paths=5)
            assert html["bytes"] > 100_000 and html["path"].endswith(".html")
            # OpenROAD's exporter drops the three.js import (blank page); the tool puts it back.
            check = (await so.mcp.call_tool("run_tcl", {"script": (
                f"set f [open {{{html['path']}}}]; set h [read $f]; close $f\n"
                "puts \"THREE_IMPORT [regexp {import \\* as THREE from} $h] NETLISTSVG [regexp {netlistsvg.bundle.js} $h]\""
            )})).content[0].text
            assert "THREE_IMPORT 1 NETLISTSVG 1" in check
            outputs = await _call(so, "write_outputs")
            assert set(outputs["files"]) == {"odb", "def", "verilog", "sdc", "spef"}
            assert all(f["bytes"] > 0 for f in outputs["files"].values())
            if platform == "sky130hd":
                fill = await _call(so, "density_fill", rules_file=SKY130_FILL_RULES)
                assert fill["summary"]["total_fills"] > 0 and fill["summary"]["fills_by_layer"]
        finally:
            await so.SESSION.close()

        files = outputs["files"]
        # The written database is unmodified by extraction: DRC clean in a brand-new session.
        try:
            await _call(route, "load_design", platform=platform, db_file=files["odb"]["path"])
            assert (await _call(route, "drc_report", recheck=True))["total"] == 0
        finally:
            await route.SESSION.close()
        # The written database + SPEF + SDC give the same timing in the STA server.
        try:
            await sta.mcp.call_tool("load_design", {"db_file": files["odb"]["path"], "sdc_file": files["sdc"]["path"],
                                                    "spef_file": files["spef"]["path"], "liberty_files": [LIBERTY[platform]]})
            sta_timing = await _call(sta, "timing_summary")
            assert sta_timing["setup"]["worst_slack"] == pytest.approx(timing["setup"]["worst_slack"], abs=0.002)
        finally:
            await sta.SESSION.close()

    asyncio.run(scenario())


def test_signoff_errors_and_options(routed):
    import mcp_signoff.server as so

    async def scenario():
        try:
            assert "No design" in await _error(so, "extract_parasitics")
            # Not routed yet: the clock-tree checkpoint.
            await _call(so, "load_checkpoint", name_or_path="pytest_signoff_nangate45_cts")
            assert "not fully routed" in await _error(so, "extract_parasitics")
            assert "not fully routed" in await _error(so, "analyze_ir_drop")

            await _call(so, "load_checkpoint", name_or_path=routed["nangate45"])
            assert "no fill_rules" in await _error(so, "density_fill")
            assert "extract_parasitics first" in await _error(so, "write_outputs")
            # A wrong rules path is caught before OpenRCX reads it, so extraction still works afterwards.
            assert "File not found" in await _error(so, "extract_parasitics", rules_file="/home/ansh/no_such.rules")
            ex = await _call(so, "extract_parasitics", coupling_threshold=0.5)
            assert ex["summary"]["coupling_threshold_ff"] == 0.5

            default_power = (await _call(so, "power_analysis"))["total_w"]
            active_power = (await _call(so, "power_analysis", input_activity=0.5))["total_w"]
            assert active_power != default_power

            strict = await _call(so, "check_power_grid", net="VDD", require_terminals=True)
            assert strict["all_connected"] is False and "PSM-0025" in str(strict["nets"][0])
            only_vdd = await _call(so, "analyze_ir_drop", net="VDD", voltage=1.0, worst_instances=0)
            assert only_vdd["reports"][0]["supply_voltage_v"] == pytest.approx(1.0)
            assert "worst_instances" not in only_vdd["reports"][0]

            quick = await _call(so, "signoff_checklist", run_ir_drop=False, max_ir_drop_pct=1)
            assert not any(i["item"].startswith("ir_drop") for i in quick["items"])
            only_v = await _call(so, "write_outputs", formats=["verilog"], include_power_pins=True)
            assert list(only_v["files"]) == ["verilog"]
        finally:
            await so.SESSION.close()

    asyncio.run(scenario())
