"""mcp-place end to end on real OpenROAD (WSL).

* gcd on the default test platform through place_design, compared with OpenROAD's reference run
  (test/flow.tcl + gcd_<platform>.metrics: I/O HPWL), then handed to mcp-opt through a checkpoint.
* The individual tools (utilization floorplan, auto skip_io, pin constraints, PDN replace, fillers, ...).
* Macro placement on the macro placer's own test case.
* A second and third technology (sky130hd, asap7) with no code changes: only platform=... differs.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import requires_openroad

pytestmark = [pytest.mark.openroad, requires_openroad]

TEST_DIR = "/home/ansh/OpenROAD/test"
MPL_DIR = "/home/ansh/OpenROAD/src/mpl/test"

# Die/core areas and reference I/O HPWL (design__io__hpwl in DBU / DBU per micron) from test/gcd_<platform>.*
GCD = {
    "nangate45": {"die": [0, 0, 100.13, 100.8], "core": [10.07, 11.2, 90.25, 91], "io_hpwl": 3934633 / 2000, "rel": 0.01},
    "sky130hd": {"die": [0, 0, 299.96, 300.128], "core": [9.996, 10.08, 289.964, 290.048], "io_hpwl": 6719711 / 1000,
                 "rel": 0.02},
    "asap7": {"die": [0, 0, 16.2, 16.2], "core": [1.08, 1.08, 15.12, 15.12], "io_hpwl": 243627 / 1000, "rel": 0.08},
}


def _sc(result):
    assert not getattr(result, "is_error", False), result.content[0].text
    return result.structured_content


async def _call(server, tool, **kwargs):
    return _sc(await server.mcp.call_tool(tool, kwargs))


async def _error(server, tool, **kwargs) -> str:
    with pytest.raises(ToolError) as exc:
        await server.mcp.call_tool(tool, kwargs)
    return str(exc.value)


async def _load_gcd(server, platform):
    return await _call(server, "load_design", platform=platform, verilog_files=[f"{TEST_DIR}/gcd_{platform}.v"],
                       top_module="gcd", sdc_file=f"{TEST_DIR}/gcd_{platform}.sdc")


async def _place_gcd(place, platform):
    await _load_gcd(place, platform)
    ref = GCD[platform]
    result = await _call(place, "place_design", die_area=ref["die"], core_area=ref["core"])
    steps = result["steps"]
    assert result["status"]["stage"] == "placed"
    assert steps["initialize_floorplan"]["die_um"] == pytest.approx(ref["die"], abs=0.01)
    assert steps["macro_placement"]["macros"] == 0
    assert steps["insert_tapcells"].get("tapcells", 0) + steps["insert_tapcells"].get("endcaps", 0) > 0
    assert steps["generate_pdn"]["grids"] and all(n["shapes"] > 0 for n in steps["generate_pdn"]["special_nets"])
    assert steps["global_placement_skip_io"]["skip_io"] is True
    placed, total = steps["place_io_pins"]["pins_placed"].split("/")
    assert steps["place_io_pins"]["io_pins"] == 54 and placed == total  # some PDNs add supply ports too
    assert steps["place_io_pins"]["io_hpwl_um"] == pytest.approx(ref["io_hpwl"], rel=ref["rel"])
    assert steps["global_placement"]["final_overflow"] <= 0.1
    assert steps["detailed_placement"]["passed"]
    return result


def test_place_design_gcd_and_handoff_to_opt():
    import mcp_opt.server as opt
    import mcp_place.server as place

    async def scenario():
        try:
            result = await _place_gcd(place, "nangate45")
            steps = result["steps"]
            assert steps["initialize_floorplan"]["rows"] == 57 and steps["initialize_floorplan"]["removed_buffers"] == 15
            assert steps["insert_tapcells"]["endcaps"] == 114
            assert steps["global_placement"]["routability_iterations"] > 0
            assert result["status"]["setup"]["worst_slack"] is not None  # parasitics estimated after placement

            report = await _call(place, "floorplan_report")
            assert report["pins_placed"] == report["pins"] == 54 and report["tap_cells"] == 114
            assert {n["net"] for n in report["power_grid"]} == {"VDD", "VSS"}
            assert (await _call(place, "check_placement"))["passed"]
            pins = await _call(place, "report_io_pins", limit=5)
            assert pins["pins_placed"] == 54 and len(pins["pins"]) == 5 and "center_um" in pins["pins"][0]
            image = await place.mcp.call_tool("snapshot", {"width": 400})
            assert base64.b64decode(image.content[0].data).startswith(b"\x89PNG")

            saved = await _call(place, "save_checkpoint", name="pytest_placed", note="pytest place", overwrite=True)
            assert saved["stage"] == "placed"
        finally:
            await place.SESSION.close()

        # Hand-off: the optimization server continues from the checkpoint (flow.tcl's next steps).
        try:
            loaded = await _call(opt, "load_checkpoint", name_or_path="pytest_placed")
            assert loaded["saved_by"] == "place" and loaded["status"]["stage"] == "placed"
            assert loaded["status"]["platform"] == "nangate45"
            rd = await _call(opt, "repair_design")
            assert rd["after"]["drv"] == {"max_slew": 0, "max_cap": 0, "max_fanout": 0}
            await _call(opt, "repair_tie_fanout")
            assert (await _call(opt, "legalize"))["summary"]["passed"]
            cts = await _call(opt, "clock_tree_synthesis")
            assert cts["summary"]["buffers_created"] > 0
            rt = await _call(opt, "repair_timing")
            wns = rt["after"]["setup"]["worst_slack"]
            assert -0.1 < wns < 0.05  # reference flow: about -0.025 ns
            assert rt["after"]["hold"]["worst_slack"] >= 0
            await _call(opt, "save_checkpoint", name="pytest_place_cts", note="pytest place->opt", overwrite=True)
        finally:
            await opt.SESSION.close()

    asyncio.run(scenario())


def test_place_tools_step_by_step():
    import mcp_place.server as place

    async def scenario():
        try:
            assert "design" in (await _error(place, "global_placement")).lower()  # nothing loaded yet
            await _load_gcd(place, "nangate45")
            assert "initialize_floorplan first" in await _error(place, "global_placement")
            assert "either utilization" in await _error(place, "initialize_floorplan")

            fp = await _call(place, "initialize_floorplan", utilization=30, aspect_ratio=1, core_space=[5])
            assert fp["summary"]["utilization"] == pytest.approx(0.3, abs=0.03)
            assert fp["summary"]["track_grids"] > 0 and fp["after"]["stage"] == "floorplanned"
            assert "already floorplanned" in await _error(place, "initialize_floorplan", utilization=30)

            assert (await _call(place, "macro_placement"))["summary"]["macros"] == 0
            tap = await _call(place, "insert_tapcells", distance=60)
            assert '"-distance" "60.0"' in tap["summary"]["command"]
            assert "already has" in await _error(place, "insert_tapcells")
            await _call(place, "generate_pdn")
            assert "replace=true" in await _error(place, "generate_pdn")
            again = await _call(place, "generate_pdn", replace=True)
            assert all(n["shapes"] > 0 for n in again["summary"]["special_nets"])

            gp = await _call(place, "global_placement", routability_driven=False)
            assert gp["summary"]["skip_io"] is True and "place_io_pins" in gp["summary"]["next"]

            await _call(place, "set_io_pin_constraint", direction="input", region="left:*")
            await _call(place, "set_io_pin_constraint", direction="output", region="right:*")
            pins = await _call(place, "place_io_pins", min_distance=2, min_distance_in_tracks=True, corner_avoidance=1)
            assert pins["summary"]["pins_placed"] == "54/54"
            listed = (await _call(place, "report_io_pins"))["pins"]
            die_x2 = fp["summary"]["die_um"][2]
            inputs = [p for p in listed if p["direction"] == "INPUT" and p["signal"] == "SIGNAL"]
            outputs = [p for p in listed if p["direction"] == "OUTPUT"]
            assert inputs and all(p["center_um"][0] < 1 for p in inputs)
            assert outputs and all(p["center_um"][0] > die_x2 - 1 for p in outputs)
            await _call(place, "set_io_pin_constraint", clear=True)

            p = await _call(place, "platform_info")
            moved = await _call(place, "place_pin", pin_name="req_val", layer=p["io_placer_ver_layer"],
                                location=[50, fp["summary"]["die_um"][3]], pin_size=[0.14, 0.28],
                                force_to_die_boundary=True)
            assert moved["pin"]["status"] == "FIRM" and moved["pin"]["layer"] == p["io_placer_ver_layer"]

            gp2 = await _call(place, "global_placement", timing_driven=False)
            assert "skip_io" not in gp2["summary"] and gp2["after"]["stage"] == "placed"
            dp = await _call(place, "detailed_placement")
            assert dp["summary"]["passed"] and dp["summary"]["max_displacement_um"] >= 0
            opt = await _call(place, "optimize_placement")
            assert opt["summary"]["passed"]
            assert opt["summary"]["mirroring"]["hpwl_after_um"] <= opt["summary"]["mirroring"]["hpwl_before_um"]
            assert opt["summary"]["improve"]["final_hpwl_um"] <= opt["summary"]["improve"]["original_hpwl_um"]

            fill = await _call(place, "filler_placement")
            assert fill["summary"]["fillers_placed"] == fill["summary"]["fillers_in_design"] > 0
            removed = await _call(place, "remove_fillers")
            assert removed["summary"]["fillers_removed"] == fill["summary"]["fillers_placed"]

            history = (await _call(place, "session_status"))["history"]
            assert any("generate_pdn" in h for h in history) and any("place_pin" in h for h in history)
        finally:
            await place.SESSION.close()

    asyncio.run(scenario())


def test_macro_placement():
    import mcp_place.server as place

    async def scenario():
        try:
            await _call(place, "load_design", def_file=f"{MPL_DIR}/testcases/boundary_push1.def",
                        lef_files=[f"{MPL_DIR}/Nangate45/Nangate45.lef", f"{MPL_DIR}/testcases/orientation_improve1.lef"])
            before = await _call(place, "floorplan_report")
            assert before["macros"] == 4
            result = await _call(place, "macro_placement", halo=[2, 2], boundary_weight=0)
            assert result["summary"]["macros"] == result["summary"]["placed"] == 4
            boxes = [m["bbox_um"] for m in result["summary"]["macro_list"]]
            assert len({tuple(b) for b in boxes}) == 4  # four distinct positions
            moved = await _call(place, "place_macro", name="MACRO_1", location=[60, 60], orientation="R0",
                                allow_overlap=True)
            assert moved["previous"]["status"] == "LOCKED"  # the macro placer locks what it places
            assert moved["macro"]["bbox_um"][:2] == pytest.approx([60, 60], abs=1) and moved["macro"]["orient"] == "R0"
            assert "No macro instance" in await _error(place, "place_macro", name="nope", location=[0, 0])
        finally:
            await place.SESSION.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["sky130hd", "asap7"])
def test_place_design_other_technologies(platform):
    """Same tool calls, another technology: nothing but platform=... changes."""
    import mcp_opt.server as opt
    import mcp_place.server as place

    async def scenario():
        try:
            await _place_gcd(place, platform)
            await _call(place, "save_checkpoint", name=f"pytest_placed_{platform}", overwrite=True)
        finally:
            await place.SESSION.close()
        if platform != "sky130hd":
            return
        try:
            await _call(opt, "load_checkpoint", name_or_path=f"pytest_placed_{platform}")
            rd = await _call(opt, "repair_design")
            assert rd["after"]["drv"]["max_slew"] == 0 and rd["after"]["drv"]["max_fanout"] == 0
            await _call(opt, "repair_tie_fanout")
            await _call(opt, "legalize")
            cts = await _call(opt, "clock_tree_synthesis")
            assert cts["summary"]["buffers_created"] > 0 and cts["summary"]["legalization"]["passed"]
            rt = await _call(opt, "repair_timing")
            # Reference flow (gcd_sky130hd.metrics): setup WNS about -0.567 ns after repair_timing, hold met.
            assert -0.8 < rt["after"]["setup"]["worst_slack"] < -0.35
            assert rt["after"]["setup"]["worst_slack"] >= rt["before"]["setup"]["worst_slack"]
            assert rt["after"]["hold"]["worst_slack"] >= 0
        finally:
            await opt.SESSION.close()

    asyncio.run(scenario())
