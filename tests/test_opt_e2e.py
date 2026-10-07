"""mcp-opt end to end on real OpenROAD (WSL): gcd on the default test platform.

Placement is done through run_tcl from platform values only (the place server comes later);
the optimization steps are compared with OpenROAD's reference run of the same flow
(test/flow.tcl + gcd_nangate45.metrics: final setup WNS about -0.025 ns, DRV 0).
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from conftest import requires_openroad

pytestmark = [pytest.mark.openroad, requires_openroad]

TEST_DIR = "/home/ansh/OpenROAD/test"


def _sc(result):
    assert not getattr(result, "is_error", False), result.content[0].text
    return result.structured_content


async def _call(server, tool, **kwargs):
    return _sc(await server.mcp.call_tool(tool, kwargs))


def test_platform_discovery_and_validation():
    import mcp_opt.server as opt

    async def scenario():
        try:
            names = {p["name"] for p in (await _call(opt, "list_platforms"))["platforms"]}
            assert {"nangate45", "sky130hd", "sky130hs", "asap7"} <= names
            sky = await _call(opt, "platform_info", name="sky130hd")
            assert sky["site"] == "unithd" and set(sky["liberty_files"]) == {"fast", "slow"}
            for name in ("nangate45", "asap7"):
                result = await _call(opt, "validate_platform", name=name)
                assert result["verdict"] == "PASS", result
        finally:
            await opt.SESSION.close()

    asyncio.run(scenario())


def test_opt_flow_gcd():
    import mcp_opt.server as opt

    async def scenario():
        try:
            loaded = await _call(opt, "load_design", platform="nangate45", verilog_files=[f"{TEST_DIR}/gcd_nangate45.v"],
                                 top_module="gcd", sdc_file=f"{TEST_DIR}/gcd_nangate45.sdc")
            assert loaded["status"]["stage"] == "netlist" and loaded["status"]["counts"]["clocks"] == 1

            p = await _call(opt, "platform_info")
            tap, pad = p["tapcell"], int(p["global_place_pad"])
            await opt.mcp.call_tool("run_tcl", {"script": f"""
initialize_floorplan -site {p['site']} -die_area {{0 0 100.13 100.8}} -core_area {{10.07 11.2 90.25 91}}
source {p['tracks_file']}
remove_buffers
tapcell -distance {tap['distance']} -tapcell_master {tap['tapcell_master']} -endcap_master {tap['endcap_master']}
source {p['pdn_cfg']}
pdngen
global_placement -density {p['global_place_density']} -pad_left {pad} -pad_right {pad} -skip_io
place_pins -hor_layers {p['io_placer_hor_layer']} -ver_layers {p['io_placer_ver_layer']}
global_placement -routability_driven -density {p['global_place_density']} -pad_left {pad} -pad_right {pad}
"""})
            assert (await _call(opt, "design_status"))["stage"] == "placed"

            rc = await _call(opt, "setup_parasitics")
            assert rc["parasitics"] == "placement estimate" and len(rc["layers"]) >= 2

            rd = await _call(opt, "repair_design")
            assert rd["after"]["drv"] == {"max_slew": 0, "max_cap": 0, "max_fanout": 0}

            await _call(opt, "repair_tie_fanout")
            leg = await _call(opt, "legalize")
            assert leg["summary"]["passed"]

            cts = await _call(opt, "clock_tree_synthesis")
            assert cts["summary"]["sinks"] and cts["summary"]["buffers_created"] > 0
            assert cts["summary"]["legalization"]["passed"]

            rt = await _call(opt, "repair_timing")
            wns = rt["after"]["setup"]["worst_slack"]
            assert rt["summary"]["setup_final"]["wns"] == pytest.approx(wns, abs=0.002)
            assert -0.1 < wns < 0.05  # reference flow ends at about -0.025 ns
            assert wns >= rt["before"]["setup"]["worst_slack"]  # repair never makes setup worse here
            assert rt["after"]["hold"]["worst_slack"] >= 0

            skew = (await _call(opt, "report_clock_tree"))["clock_skew"]
            assert "core_clock" in skew

            image = await opt.mcp.call_tool("snapshot", {"width": 400})
            png = base64.b64decode(image.content[0].data)
            assert png.startswith(b"\x89PNG")

            saved = await _call(opt, "save_checkpoint", name="pytest_opt_cts", note="pytest", overwrite=True)
            assert saved["odb"].endswith(".odb")
            await _call(opt, "reset_session")
            reloaded = await _call(opt, "load_checkpoint", name_or_path="pytest_opt_cts")
            assert reloaded["status"]["counts"]["insts"] == rt["after"]["instances"]
            assert reloaded["status"]["setup"]["worst_slack"] == pytest.approx(wns, abs=0.005)
            listed = {c["name"] for c in (await _call(opt, "list_checkpoints"))["checkpoints"]}
            assert "pytest_opt_cts" in listed

            # Remaining optimization tools, on the reloaded checkpoint.
            assert "set_dont_use" in (await _call(opt, "set_dont_use", cells=["*_X32"]))["done"]
            await _call(opt, "set_dont_use", cells=["*_X32"], unset=True)
            assert "set_dont_touch" in (await _call(opt, "set_dont_touch", objects=["clk"]))["done"]
            await _call(opt, "set_dont_touch", objects=["clk"], unset=True)
            ports = await _call(opt, "buffer_ports", inputs=False, outputs=True)
            assert ports["summary"].get("output_buffers", 0) > 0
            await _call(opt, "remove_buffers")
            est = await _call(opt, "estimate_parasitics", source="placement")
            assert est["after"]["instances"] > 0
            power = await _call(opt, "recover_power", percent=50)
            assert power["after"]["area_um2"] <= power["before"]["area_um2"]
            await _call(opt, "legalize")
            problems = await _call(opt, "report_problems", long_wires=3)
            assert len(problems["longest_wires"]) == 3
            history = (await _call(opt, "session_status"))["history"]
            assert any("recover_power" in h for h in history) and any("load_checkpoint" in h for h in history)
        finally:
            await opt.SESSION.close()

    asyncio.run(scenario())
