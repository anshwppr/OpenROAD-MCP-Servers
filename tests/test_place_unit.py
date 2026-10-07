"""mcp-place offline checks: parsers against real OpenROAD 26Q2 placement logs, tool/prompt lists,
and the platform-driven routing setup."""

import asyncio

from openroad_common.parsing import (
    parse_filler,
    parse_gpl,
    parse_ifp,
    parse_improve_placement,
    parse_optimize_mirroring,
    parse_pdn,
    parse_ppl,
    parse_tapcell,
)
from openroad_common.platform import build_platform
from openroad_common.stage import platform_setup_tcl, routing_setup_tcl

IFP_LOG = """\
[INFO IFP-0107] Defining die area using utilization: 30.00% and aspect ratio: 1.
[WARNING IFP-0028] Core area lower left (10.000, 10.000) snapped to (10.120, 10.880).
[INFO IFP-0001] Added 57 rows of 422 site FreePDK45_38x28_10R_NP_162NW_34O.
[INFO IFP-0100] Die BBox:  (  0.000  0.000 ) ( 100.130 100.800 ) um
[INFO IFP-0101] Core BBox: ( 10.070 11.200 ) ( 90.250 91.000 ) um
[INFO IFP-0102] Core area:                         6398.364 um^2
[INFO IFP-0103] Total instances area:               519.764 um^2
[INFO IFP-0104] Effective utilization:                0.081
[INFO IFP-0105] Number of instances:                    362
[INFO RSZ-0026] Removed 15 buffers.
"""

PPL_LOG = """\
Found 0 macro blocks.
Using 2 tracks default min distance between IO pins.
[INFO PPL-0001] Number of available slots 1220
[INFO PPL-0002] Number of I/O             54
[INFO PPL-0003] Number of I/O w/sink      54
[INFO PPL-0004] Number of I/O w/o sink    0
[INFO PPL-0005] Slots per section         200
[INFO PPL-0008] Successfully assigned pins to sections.
[INFO PPL-0012] I/O nets HPWL: 1967.32 um.
"""

PDN_LOG = """\
[WARNING ORD-0046] -defer_connection has been deprecated.
[WARNING PDN-0183] Replacing existing core voltage domain.
[INFO PDN-0001] Inserting grid: grid
"""

GPL_LOG = """\
[INFO GPL-0006] Number of instances:               461
[INFO GPL-0007] Movable instances:                 347
[INFO GPL-0008] Fixed instances:                   114
[INFO GPL-0019] Utilization:                    15.070 %
[INFO GPL-0023] Placement target density:       0.3000
Iteration | Overflow |     HPWL (um) |  HPWL(%) |   Penalty | Group
---------------------------------------------------------------
        0 |   0.5073 |  3.985153e+03 |   +0.00% |  4.69e-13 |
[INFO GPL-0038] Routability snapshot saved at iter = 1
      250 |   0.2996 |  3.912635e+03 |   -0.33% |  1.05e-08 |
[INFO GPL-0047] Routability iteration weighted routing congestion: 0.6580
Iteration | Overflow |     HPWL (um) |  HPWL(%) |   Penalty | Group
---------------------------------------------------------------
      340 |   0.1227 |  3.902152e+03 |   +0.14% |  3.43e-07 |
      350 |   0.0985 |  3.910494e+03 |   +0.21% |  5.05e-07 |
      350 |   0.0985 |  3.910494e+03 |          |  5.25e-07 |
---------------------------------------------------------------
[INFO GPL-1001] Global placement finished at iteration 350
[INFO GPL-1017] Routability mode iteration count: 249
[INFO GPL-1005] Routability final weighted congestion: 0.6178
[INFO GPL-1002] Placed Cell Area              959.6542
[INFO GPL-1004] Minimum Feasible Density        0.1600 (cell_area / free_area)
[INFO GPL-1014] Final placement area: 959.65 (+0.00%)
"""

GPL_TIMING_LOG = """\
[INFO GPL-0100] Timing-driven iteration 1/2, virtual: false.
[INFO GPL-0101]    Iter: 245, overflow: 0.632, keep resizer changes at: 1, HPWL: 6039154
    final |     +0.0% |       0 |       0 |             0 |         0
[INFO GPL-0106] Timing-driven: worst slack -1.6406654e-11
[WARNING GPL-1010] GPL reached the maximum number of iterations for nesterov 300. Placement may have failed to converge.
[INFO GPL-1001] Global placement finished at iteration 404
[INFO GPL-1014] Final placement area: 2357.46 (-3.48%)
"""

DPL_OPT_LOG = """\
[INFO DPL-0020] Mirrored 162 instances
[INFO DPL-0021] HPWL before            4119.1 u
[INFO DPL-0022] HPWL after             4052.9 u
[INFO DPL-0023] HPWL delta               -1.6 %
[INFO DPL-0383] Performed 137 cell flips.
Detailed Improvement Results
------------------------------------------
Original HPWL             4052.9 u (    1927.4,     2125.5)
Final HPWL                3929.4 u (    1917.6,     2011.8)
Delta HPWL                  -3.0 % (      -0.5,       -5.3)
[INFO DPL-0001] Placed 1359 filler instances.
"""


def test_parse_ifp():
    out = parse_ifp(IFP_LOG)
    assert out["rows"] == 57 and out["sites_per_row"] == 422 and out["site"].startswith("FreePDK45")
    assert out["die_um"] == [0.0, 0.0, 100.13, 100.8] and out["core_um"] == [10.07, 11.2, 90.25, 91.0]
    assert out["core_area_um2"] == 6398.364 and out["utilization"] == 0.081 and out["instances"] == 362
    assert out["removed_buffers"] == 15 and "IFP-0028" in out["warnings"][0]


def test_parse_ppl_tap_pdn():
    ppl = parse_ppl(PPL_LOG)
    assert ppl == {"slots": 1220, "io_pins": 54, "io_with_sink": 54, "io_without_sink": 0, "io_hpwl_um": 1967.32}
    tap = parse_tapcell("[INFO TAP-0004] Inserted 114 endcaps.\n[INFO TAP-0005] Inserted 0 tapcells.\n")
    assert tap == {"endcaps": 114, "tapcells": 0}
    pdn = parse_pdn(PDN_LOG)
    assert pdn["grids"] == ["grid"] and pdn["warnings"] == ["[WARNING PDN-0183] Replacing existing core voltage domain."]


def test_parse_gpl_routability():
    out = parse_gpl(GPL_LOG)
    assert out["iterations"] == 350 and out["final_overflow"] == 0.0985 and out["final_hpwl_um"] == 3910.494
    assert out["movable_instances"] == 347 and out["fixed_instances"] == 114
    assert out["routability_iterations"] == 249 and out["routability_final_congestion"] == 0.6178
    assert out["final_placement_area_um2"] == 959.65 and out["minimum_feasible_density"] == 0.16
    assert "did_not_converge" not in out


def test_parse_gpl_timing_driven_without_table_rows():
    out = parse_gpl(GPL_TIMING_LOG)
    assert out["final_overflow"] == 0.632 and out["iterations"] == 404
    assert out["timing_driven_worst_slack"] == -1.6406654e-11 and out["did_not_converge"] is True
    assert "final_hpwl_um" not in out


def test_parse_dpl_optimizations():
    assert parse_optimize_mirroring(DPL_OPT_LOG) == {
        "mirrored": 162, "hpwl_before_um": 4119.1, "hpwl_after_um": 4052.9, "delta_hpwl_pct": -1.6}
    assert parse_improve_placement(DPL_OPT_LOG) == {
        "original_hpwl_um": 4052.9, "final_hpwl_um": 3929.4, "delta_hpwl_pct": -3.0, "cell_flips": 137}
    assert parse_filler(DPL_OPT_LOG) == {"fillers_placed": 1359}


def test_routing_setup_follows_flow_order():
    plat = build_platform("p", "test", [({
        "global_routing_layers": "L2-L9", "global_routing_clock_layers": "L5-L9",
        "global_routing_layer_adjustments": "{{{L2-L9} 0.5}}"}, None)])
    lines = routing_setup_tcl(plat).splitlines()
    assert lines == [
        'set_global_routing_layer_adjustment "L2-L9" 0.5',
        'set_routing_layers -signal "L2-L9" -clock "L5-L9"',
        "set_macro_extension 2",
    ]
    # In load_design / load_checkpoint the routing setup only runs once track grids exist.
    assert "getTrackGrids]] > 0} {\nset_global_routing_layer_adjustment" in platform_setup_tcl(plat)


def test_place_server_tools_and_prompts():
    import mcp_place.server as place

    tools = {t.name for t in asyncio.run(place.mcp.list_tools())}
    own = {"initialize_floorplan", "place_io_pins", "place_pin", "set_io_pin_constraint", "report_io_pins",
           "macro_placement", "place_macro", "insert_tapcells", "generate_pdn", "global_placement",
           "detailed_placement", "optimize_placement", "check_placement", "filler_placement", "remove_fillers",
           "floorplan_report", "place_design"}
    shared = {"list_platforms", "platform_info", "validate_platform", "load_design", "save_checkpoint",
              "load_checkpoint", "list_checkpoints", "design_status", "estimate_parasitics", "snapshot",
              "session_status", "reset_session", "run_tcl"}
    assert tools == own | shared
    prompts = {p.name for p in asyncio.run(place.mcp.list_prompts())}
    assert prompts == {"floorplan_and_place", "placement_review", "io_pin_planning"}


def test_opt_server_tools_and_prompts():
    import mcp_opt.server as opt

    assert len(asyncio.run(opt.mcp.list_tools())) == 26
    assert {p.name for p in asyncio.run(opt.mcp.list_prompts())} == {
        "fix_timing", "cts_review", "drv_cleanup", "power_recovery"}
