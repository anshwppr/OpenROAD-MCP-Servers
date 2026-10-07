"""mcp-route offline checks: routing parsers against real OpenROAD 26Q2 logs (gcd on nangate45 and
sky130hd), the DRC report format, and the tool/prompt lists."""

import asyncio

from openroad_common.parsing import (
    parse_antennas,
    parse_drc_report,
    parse_drt,
    parse_grt,
    parse_wire_length_table,
)

GRT_LOG = """\
[INFO GRT-0020] Min routing layer: metal2
[INFO GRT-0021] Max routing layer: metal10
[INFO GRT-0019] Found 6 clock nets.
[INFO GRT-0053] Routing resources analysis:
          Routing      Original      Derated      Resource
Layer     Direction    Resources     Resources    Reduction (%)
---------------------------------------------------------------
metal1     Horizontal          0             0          0.00%
metal2     Vertical        17632          8278          53.05%
---------------------------------------------------------------
[INFO GRT-0111] Final number of vias: 2487
[INFO GRT-0112] Final usage 3D: 9385
[INFO GRT-0096] Final congestion report:
Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Overflow
---------------------------------------------------------------------------------------
metal1               0             0            0.00%             0 /  0 /  0
metal2            8278          1006           12.15%             0 /  0 /  0
metal3           11978           814            6.80%             0 /  0 /  0
metal4            5898             0            0.00%             0 /  0 /  0
---------------------------------------------------------------------------------------
Total            44228          1924            4.35%             0 /  0 /  0
[INFO GRT-0018] Total wirelength: 7897 um
[INFO GRT-0014] Routed nets: 410
[INFO GRT-0303] Global routing runtime = 00:00:00
"""

WL_LOG = """\
[INFO GRT-0279] Detailed route wire length by layer:
Layer    Wire length  Percentage
--------------------------------
metal2   2917.29um    54%
metal3   2324.10um    43%
metal4      1.26um     0%
metal6     67.48um     1%
metal7     82.04um     1%
--------------------------------
"""

ANT_REPAIR_LOG = """\
[INFO GRT-0006] Repairing antennas, iteration 1.
[INFO GRT-0012] Found 12 antenna violations.
[INFO DPL-0006] Core area: 77594.42 um^2, Instances area: 3952.54 um^2, Utilization: 5.1%
[INFO GRT-0015] Inserted 12 diodes.
[INFO GRT-0009] rerouting 12 nets.
[INFO GRT-0006] Repairing antennas, iteration 2.
[INFO GRT-0012] Found 0 antenna violations.
[INFO ANT-0002] Found 0 net violations.
[INFO ANT-0001] Found 0 pin violations.
"""

DRT_LOG = """\
[INFO DRT-0194] Start detail routing.
[INFO DRT-0195] Start 0th optimization iteration.
    Completing 100% with 4 violations.
[INFO DRT-0199]   Number of violations = 15.
Viol/Layer      metal2 metal3
Recheck              7      4
Short                4      0
[INFO DRT-0267] cpu time = 00:00:03, elapsed time = 00:00:01, memory = 910.62 (MB), peak = 909.23 (MB)
Total wire length = 5410 um.
Total wire length on LAYER metal1 = 0 um.
Total wire length on LAYER metal2 = 2922 um.
Total number of vias = 2184.
[INFO DRT-0195] Start 1st optimization iteration.
[INFO DRT-0199]   Number of violations = 5.
Viol/Layer      metal2
Metal Spacing        1
Short                4
Total wire length = 5392 um.
Total number of vias = 2188.
[INFO DRT-0195] Start 60th stubborn tiles iteration.
[INFO DRT-0199]   Number of violations = 0.
Total wire length = 5394 um.
Total wire length on LAYER metal2 = 2923 um.
Total wire length on LAYER metal3 = 2318 um.
Total wire length on LAYER metal5 = 0 um.
Total number of vias = 2191.
[INFO DRT-0198] Complete detail routing.
Total wire length = 5394 um.
Total number of vias = 2191.
"""

DRC_RPT = """\
violation type: Metal Spacing
\tsrcs: net:_126_ net:_178_
\tbbox = (58.6500, 31.3950) - (58.6850, 31.4650) on Layer metal1
violation type: Short
\tsrcs: net:_193_ net:_194_
\tbbox = (39.5800, 44.1000) - (39.6500, 44.1350) on Layer metal2
violation type: Short
\tsrcs: net:_188_ net:dpath.a_lt_b$in0\\[1\\]
\tbbox = (40.5300, 44.1000) - (40.6000, 44.1350) on Layer metal2
violation type: Short
\tsrcs: net:_188_ obs:blockage
\tbbox = (41.0000, 44.1000) - (41.0500, 44.1350) on Layer metal2
"""


def test_parse_grt():
    out = parse_grt(GRT_LOG)
    assert out["routed_nets"] == 410 and out["clock_nets"] == 6
    assert out["wirelength_um"] == 7897 and out["vias"] == 2487
    assert out["min_layer"] == "metal2" and out["max_layer"] == "metal10"
    assert out["overflow"] == 0 and out["usage_pct"] == 4.35
    # Only layers with resources or demand; the resource-analysis table (with directions) is not mistaken for it.
    assert [r["layer"] for r in out["congestion"]] == ["metal2", "metal3", "metal4"]
    assert out["congestion"][0] == {"layer": "metal2", "resource": 8278, "demand": 1006, "usage_pct": 12.15,
                                    "max_h_overflow": 0, "max_v_overflow": 0, "overflow": 0}


def test_parse_wire_length_table():
    out = parse_wire_length_table(WL_LOG)
    assert [r["layer"] for r in out["layers"]] == ["metal2", "metal3", "metal4", "metal6", "metal7"]
    assert out["total_um"] == 5392.17


def test_parse_antennas():
    out = parse_antennas(ANT_REPAIR_LOG)
    assert out == {"net_violations": 0, "pin_violations": 0, "repair_iterations": 2, "violations_found": 12,
                   "violations_left": 0, "diodes_inserted": 12, "nets_rerouted": 12}
    no_diode = parse_antennas("[WARNING GRT-0246] No diode with LEF class CORE ANTENNACELL found.\n"
                              "[INFO ANT-0002] Found 0 net violations.\n[INFO ANT-0001] Found 0 pin violations.\n")
    assert no_diode["no_diode"] is True and no_diode["net_violations"] == 0


def test_parse_drt():
    out = parse_drt(DRT_LOG)
    assert [i["iteration"] for i in out["iterations"]] == [0, 1, 60]
    assert out["iterations"][2]["kind"] == "stubborn tiles" and "kind" not in out["iterations"][0]
    assert [i["violations"] for i in out["iterations"]] == [15, 5, 0]
    assert out["iterations"][0]["by_type"] == {"Recheck": {"metal2": 7, "metal3": 4}, "Short": {"metal2": 4}}
    assert out["iterations"][1]["by_type"] == {"Metal Spacing": {"metal2": 1}, "Short": {"metal2": 4}}
    assert out["final_violations"] == 0 and "violations_by_type" not in out
    assert out["wirelength_um"] == 5394 and out["vias"] == 2191 and out["completed"] is True
    assert out["wirelength_by_layer_um"] == {"metal2": 2923, "metal3": 2318}


def test_parse_drc_report():
    out = parse_drc_report(DRC_RPT, limit=2)
    assert out["total"] == 4 and out["truncated"] is True and len(out["violations"]) == 2
    assert out["by_type"] == {"Metal Spacing": 1, "Short": 3}
    assert out["by_layer"] == {"metal1": 1, "metal2": 3}
    assert out["by_type_layer"] == {"Metal Spacing @ metal1": 1, "Short @ metal2": 3}
    assert out["top_nets"][0] == {"net": "_188_", "violations": 2}
    assert out["violations"][0] == {"type": "Metal Spacing", "sources": ["net:_126_", "net:_178_"],
                                    "bbox_um": [58.65, 31.395, 58.685, 31.465], "layer": "metal1"}
    assert parse_drc_report("")["total"] == 0


def test_route_server_tools_and_prompts():
    import mcp_route.server as route

    tools = {t.name for t in asyncio.run(route.mcp.list_tools())}
    own = {"configure_routing", "global_route", "report_wire_length", "check_antennas", "repair_antennas",
           "detailed_route", "drc_report", "routing_report", "route_design"}
    shared = {"list_platforms", "platform_info", "validate_platform", "load_design", "save_checkpoint",
              "load_checkpoint", "list_checkpoints", "design_status", "estimate_parasitics", "snapshot",
              "session_status", "reset_session", "run_tcl"}
    assert tools == own | shared
    prompts = {p.name for p in asyncio.run(route.mcp.list_prompts())}
    assert prompts == {"route_and_verify", "fix_congestion", "drc_triage"}
    assert route.CFG.timeout == 7200
