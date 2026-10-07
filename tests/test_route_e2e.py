"""mcp-route end to end on real OpenROAD (WSL).

* Full chain on gcd: mcp-place place_design -> mcp-opt (repair_design, tie cells, legalize, CTS,
  repair_timing) -> checkpoint -> mcp-route route_design -> checkpoint 'routed', then cross-checked
  with mcp-odb (routed wirelength) and mcp-sta (timing) on the routed database.
* The same calls on sky130hd (only platform=... differs), where antenna repair inserts diodes.
* The individual tools step by step, including a partial detailed route with DRC violations.
* asap7: global routing smoke test.

Reference numbers are this OpenROAD binary's own run of test/flow.tcl (the .metrics files in the
repository come from a newer OpenROAD and are compared loosely):
  nangate45: GR 7897 um / 2487 vias, DR 5392 um / 2191 vias, 0 DRC, 0 antenna violations.
  sky130hd:  GR 20244 um / 2236 vias, DR 14817 um, 0 DRC, antenna diodes inserted (metrics: 14662 um, 1977 vias).
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import requires_openroad

pytestmark = [pytest.mark.openroad, requires_openroad]

TEST_DIR = "/home/ansh/OpenROAD/test"
AREAS = {
    "nangate45": ([0, 0, 100.13, 100.8], [10.07, 11.2, 90.25, 91]),
    "sky130hd": ([0, 0, 299.96, 300.128], [9.996, 10.08, 289.964, 290.048]),
    "asap7": ([0, 0, 16.2, 16.2], [1.08, 1.08, 15.12, 15.12]),
}
BASELINE = {"nangate45": {"wl": 5392, "vias": 2191}, "sky130hd": {"wl": 14817, "vias": 1977}}


def _sc(result):
    assert not getattr(result, "is_error", False), result.content[0].text
    return result.structured_content


async def _call(server, tool, **kwargs):
    return _sc(await server.mcp.call_tool(tool, kwargs))


async def _error(server, tool, **kwargs) -> str:
    with pytest.raises(ToolError) as exc:
        await server.mcp.call_tool(tool, kwargs)
    return str(exc.value)


async def _placed(platform: str, name: str) -> None:
    import mcp_place.server as place

    try:
        await _call(place, "load_design", platform=platform, verilog_files=[f"{TEST_DIR}/gcd_{platform}.v"],
                    top_module="gcd", sdc_file=f"{TEST_DIR}/gcd_{platform}.sdc")
        die, core = AREAS[platform]
        await _call(place, "place_design", die_area=die, core_area=core)
        await _call(place, "save_checkpoint", name=name, overwrite=True)
    finally:
        await place.SESSION.close()


async def _cts(platform: str, name: str) -> None:
    import mcp_opt.server as opt

    await _placed(platform, f"{name}_placed")
    try:
        await _call(opt, "load_checkpoint", name_or_path=f"{name}_placed")
        await _call(opt, "repair_design")
        await _call(opt, "repair_tie_fanout")
        await _call(opt, "legalize")
        await _call(opt, "clock_tree_synthesis")
        await _call(opt, "repair_timing")
        await _call(opt, "save_checkpoint", name=name, overwrite=True)
    finally:
        await opt.SESSION.close()


@pytest.mark.parametrize("platform", ["nangate45", "sky130hd"])
def test_route_design_full_chain(platform):
    import mcp_route.server as route

    async def scenario():
        ckpt = f"pytest_route_{platform}_cts"
        await _cts(platform, ckpt)
        try:
            loaded = await _call(route, "load_checkpoint", name_or_path=ckpt)
            assert loaded["saved_by"] == "opt" and loaded["status"]["stage"] == "placed"

            result = await _call(route, "route_design")
            gr = result["steps"]["global_route"]
            assert gr["overflow"] == 0 and gr["routed_nets"] > 0 and gr["clock_nets"] > 0
            assert result["drc_violations"] == 0 and result["antenna_violations"] == 0
            assert result["status"]["stage"] == "routed"
            dr = result["steps"]["detailed_route"]
            assert dr["all_nets_routed"] and dr["iterations"][0]["violations"] >= dr["final_violations"]
            ref = BASELINE[platform]
            assert result["wirelength_um"] == pytest.approx(ref["wl"], rel=0.05)
            assert result["vias"] == pytest.approx(ref["vias"], rel=0.15 if platform == "sky130hd" else 0.05)
            assert result["steps"]["filler_placement"]["fillers_placed"] > 0
            assert result["steps"]["filler_placement"]["passed"]
            if platform == "nangate45":
                # The library has no antenna cell: reported, not an error.
                assert result["steps"]["repair_antennas"]["no_diode"] is True
            else:
                # sky130hd: antennas found and fixed with diodes (before and/or after detailed routing).
                diodes = sum(s.get("diodes_inserted", 0) for k, s in result["steps"].items() if k.startswith("repair_antennas"))
                assert diodes > 0

            report = await _call(route, "routing_report")
            assert report["stage"] == "routed" and report["all_nets_routed"]
            assert report["antennas"]["net_violations"] == 0 and report["drc_violations_last_route"] == 0
            assert report["wirelength"]["total_um"] == pytest.approx(ref["wl"], rel=0.05)
            assert (await _call(route, "drc_report", recheck=True))["total"] == 0

            saved = await _call(route, "save_checkpoint", name=f"pytest_routed_{platform}", overwrite=True)
            assert saved["stage"] == "routed"
            await _call(route, "reset_session")
            reloaded = await _call(route, "load_checkpoint", name_or_path=f"pytest_routed_{platform}")
            assert reloaded["status"]["stage"] == "routed"
            assert "already routed" in await _error(route, "route_design")
        finally:
            await route.SESSION.close()

        # Cross-check the routed database with the OpenDB and STA servers.
        import mcp_odb.server as odb
        import mcp_sta.server as sta

        odb_path = saved["odb"]
        try:
            await odb.mcp.call_tool("load_design", {"db_file": odb_path})
            summary = await _call(odb, "design_summary")
            assert summary["routed"] and summary["total_routed_wirelength_um"] == pytest.approx(ref["wl"], rel=0.05)
        finally:
            await odb.SESSION.close()
        try:
            await sta.mcp.call_tool("load_design", {"db_file": odb_path, "sdc_file": saved["sdc"],
                                                    "liberty_files": reloaded_libs(reloaded, platform)})
            timing = await _call(sta, "timing_summary")
            assert isinstance(timing["setup"]["worst_slack"], float) and timing["hold"]["status"] == "MET"
        finally:
            await sta.SESSION.close()

    asyncio.run(scenario())


def reloaded_libs(_reloaded, platform: str) -> list[str]:
    return {"nangate45": [f"{TEST_DIR}/Nangate45/Nangate45_typ.lib"],
            "sky130hd": [f"{TEST_DIR}/sky130hd/sky130hd_tt.lib"]}[platform]


def test_route_tools_step_by_step():
    import mcp_route.server as route

    async def scenario():
        await _cts("nangate45", "pytest_route_steps_cts")
        try:
            # Errors before anything is routed / on an unplaced design.
            await _call(route, "load_design", platform="nangate45", verilog_files=[f"{TEST_DIR}/gcd_nangate45.v"],
                        top_module="gcd", sdc_file=f"{TEST_DIR}/gcd_nangate45.sdc")
            assert "not fully placed" in await _error(route, "global_route")
            assert "not fully placed" in await _error(route, "detailed_route")

            await _call(route, "load_checkpoint", name_or_path="pytest_route_steps_cts")
            assert "global_route first" in await _error(route, "detailed_route")
            assert "global_route first" in await _error(route, "repair_antennas")
            assert "global_route first" in await _error(route, "check_antennas")
            assert "No DRC report" in await _error(route, "drc_report")

            p = await _call(route, "platform_info")
            cfg = await _call(route, "configure_routing", reset_to_platform=True, macro_extension=2)
            assert cfg["routing_layers"]["signal"] == p["global_routing_layers"]
            assert cfg["adjustments"][0]["adjustment"] == pytest.approx(0.5)

            gr = await _call(route, "global_route")
            assert gr["summary"]["vias"] == pytest.approx(2487, rel=0.05)
            assert gr["summary"]["wirelength_um"] == pytest.approx(7897, rel=0.05)
            assert gr["after"]["setup"]["worst_slack"] is not None
            assert (await _call(route, "session_status"))["parasitics"] == "global routing estimate"
            wl = await _call(route, "report_wire_length", source="global")
            assert wl["total_um"] > 0 and wl["layers"][0]["layer"] == p["io_placer_ver_layer"]
            assert "no detailed routing" in await _error(route, "report_wire_length")
            ant = await _call(route, "check_antennas")
            assert ant["net_violations"] == 0 and ant["checked"] == "global routes"
            rep = await _call(route, "repair_antennas")
            assert rep["summary"]["no_diode"] and "diode" in rep["summary"]["message"]

            # Partial detailed route (iterations 0 and 1 only) leaves violations for drc_report.
            partial = await _call(route, "detailed_route", end_iteration=1)
            assert partial["summary"]["drc_violations"] > 0
            assert partial["summary"]["final_violations"] == partial["summary"]["drc_violations"]
            drc = await _call(route, "drc_report", limit=3)
            assert drc["total"] == partial["summary"]["drc_violations"]
            assert sum(drc["by_type"].values()) == drc["total"] and drc["by_layer"]
            assert len(drc["violations"]) == min(3, drc["total"]) and "bbox_um" in drc["violations"][0]
            core = (await _call(route, "design_status"))["core_um"]
            local = await _call(route, "drc_report", recheck=True, area=core)
            assert local["source"] == "check_drc" and local["total"] >= 0

            # Running again continues from the existing wires (no -droute_end_iter carry-over from the partial run).
            more = await _call(route, "detailed_route")
            assert len(more["summary"]["iterations"]) > 2
            assert more["summary"]["drc_violations"] <= partial["summary"]["drc_violations"]

            # Clean route from scratch: reload the unrouted checkpoint (guides are rebuilt by global_route).
            await _call(route, "load_checkpoint", name_or_path="pytest_route_steps_cts")
            await _call(route, "global_route")
            full = await _call(route, "detailed_route")
            assert full["summary"]["drc_violations"] == 0 and full["summary"]["all_nets_routed"]
            assert full["summary"]["wirelength_um"] == pytest.approx(BASELINE["nangate45"]["wl"], rel=0.05)
            assert (await _call(route, "drc_report"))["total"] == 0
            nets = await _call(route, "report_wire_length", nets=["clk", "no_such_net"])
            assert nets["nets"]["clk"] > 0 and nets["missing"] == ["no_such_net"]
            report = await _call(route, "routing_report")
            assert report["stage"] == "routed" and report["clocks_propagated"] == "1/1"
            assert "already routed" in await _error(route, "configure_routing", macro_extension=1)
            history = (await _call(route, "session_status"))["history"]
            # load_checkpoint restores the checkpoint's history (place + opt steps), then the route steps follow.
            assert any("repair_timing" in h for h in history) and history[-1].endswith("detailed_route")
        finally:
            await route.SESSION.close()

    asyncio.run(scenario())


def test_global_route_after_checkpoint_reload_and_asap7():
    """Guides survive a checkpoint (detailed routing in a new session) and asap7 global-routes cleanly."""
    import mcp_route.server as route

    async def scenario():
        await _placed("asap7", "pytest_route_asap7_placed")
        try:
            await _call(route, "load_checkpoint", name_or_path="pytest_route_asap7_placed")
            gr = await _call(route, "global_route")
            assert gr["summary"]["overflow"] == 0 and gr["summary"]["routed_nets"] > 0
            await _call(route, "save_checkpoint", name="pytest_route_asap7_gr", overwrite=True)
            await _call(route, "reset_session")
            await _call(route, "load_checkpoint", name_or_path="pytest_route_asap7_gr")
            report = await _call(route, "routing_report")
            assert report["stage"] == "globally routed" and report["route_guides"] > 0
            # Detailed routing from the stored guides, in a fresh process (two iterations keep it short).
            dr = await _call(route, "detailed_route", end_iteration=1)
            assert dr["summary"]["iterations"] and dr["summary"]["all_nets_routed"] is not None
        finally:
            await route.SESSION.close()

    asyncio.run(scenario())
