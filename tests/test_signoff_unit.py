"""mcp-signoff offline checks: signoff parsers against real OpenROAD 26Q2 output (gcd), the
Windows path helper, and the tool/prompt lists."""

import asyncio

from openroad_common.parsing import (
    parse_clock_min_period,
    parse_drv_violators,
    parse_endpoint_report,
    parse_fill,
    parse_ir_report,
    parse_power,
    parse_rcx,
)

RCX_LOG = """\
[INFO RCX-0431] Defined process_corner X with ext_model_index 0
[INFO RCX-0435] Reading extraction model file Nangate45/Nangate45.rcx_rules ...
[INFO RCX-0040] Final 1343 rc segments
[INFO RCX-0440] Coupling threshhold is 0.1000 fF, coupling capacitance less than 0.1000 fF will be grounded.
[INFO RCX-0442] 100% of 1994 wires extracted
[INFO RCX-0045] Extract 444 nets, 1753 rsegs, 1753 caps, 2269 ccs
[INFO RCX-0443] 444 nets finished
"""

END_LOG = """\
max_delay/setup group core_clock
                                     Required  Actual
Endpoint                               Delay   Delay   Slack
------------------------------------------------------------
resp_msg[15] (output)                   0.39    0.42   -0.03 (VIOLATED)
resp_msg[13] (output)                   0.39    0.42   -0.03 (VIOLATED)
min_delay/hold group core_clock
                                     Required  Actual
Endpoint                               Delay   Delay   Slack
------------------------------------------------------------
_674_/D (DFF_X1)                        0.06    0.11    0.05 (MET)
"""

DRV_LOG = """\
max slew
Pin                                     Limit     Slew    Slack
---------------------------------------------------------------
_284_/Y                                 1.494    1.501   -0.007 (VIOLATED)
max capacitance
Pin                                     Limit      Cap    Slack
---------------------------------------------------------------
_585_/ZN                               26.703   28.658   -1.955 (VIOLATED)
"""

POWER_LOG = """\
Group                  Internal  Switching    Leakage      Total
                          Power      Power      Power      Power (Watts)
----------------------------------------------------------------
Sequential             4.53e-04   9.97e-05   2.80e-06   5.56e-04  23.4%
Combinational          6.94e-04   7.17e-04   1.11e-05   1.42e-03  59.8%
Clock                  1.90e-04   2.10e-04   4.59e-07   4.00e-04  16.8%
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
Pad                    0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
----------------------------------------------------------------
Total                  1.34e-03   1.03e-03   1.44e-05   2.38e-03 100.0%
                          56.2%      43.2%       0.6%
"""

IR_LOG = """\
[INFO PSM-0040] All shapes on net VDD are connected.
[INFO PSM-0073] Using bump pattern with x-pitch 140.0000um, y-pitch 140.0000um, and size 70.0000um with an reduction factor of 3x.
########## IR report #################
Net              : VDD
Corner           : default
Total power      : 1.85e-03 W
Supply voltage   : 1.10e+00 V
Worstcase voltage: 1.09e+00 V
Average voltage  : 1.10e+00 V
Average IR drop  : 1.90e-03 V
Worstcase IR drop: 6.37e-03 V
Percentage drop  : 0.58 %
######################################
########## EM analysis ###############
Net                : VDD
Corner             : default
Maximum current    : 3.53e-04 A
Average current    : 1.08e-05 A
Number of resistors: 2168
######################################
"""

FILL_LOG = """\
[WARNING FIN-0010] Skipping layer li1.
[INFO FIN-0003] Filling layer met1.
[INFO FIN-0009] Filling 0 areas with non-OPC fill.
[INFO FIN-0004] Total fills: 0.
[WARNING FIN-0010] Skipping layer via.
[INFO FIN-0003] Filling layer met2.
[INFO FIN-0009] Filling 32 areas with non-OPC fill.
[INFO FIN-0004] Total fills: 8418.
[INFO FIN-0003] Filling layer met3.
[INFO FIN-0004] Total fills: 16910.
"""


def test_parse_rcx():
    assert parse_rcx(RCX_LOG) == {"nets": 444, "resistor_segments": 1753, "ground_caps": 1753, "coupling_caps": 2269,
                                  "rc_segments": 1343, "coupling_threshold_ff": 0.1}


def test_parse_endpoints_and_drv():
    ends = parse_endpoint_report(END_LOG)
    assert [e["endpoint"] for e in ends["setup"]] == ["resp_msg[15] (output)", "resp_msg[13] (output)"]
    assert ends["setup"][0] == {"endpoint": "resp_msg[15] (output)", "required": 0.39, "arrival": 0.42, "slack": -0.03}
    assert ends["hold"] == [{"endpoint": "_674_/D (DFF_X1)", "required": 0.06, "arrival": 0.11, "slack": 0.05}]
    drv = parse_drv_violators(DRV_LOG)
    assert drv == {"max_slew": [{"pin": "_284_/Y", "limit": 1.494, "value": 1.501, "slack": -0.007}],
                   "max_cap": [{"pin": "_585_/ZN", "limit": 26.703, "value": 28.658, "slack": -1.955}]}
    assert parse_drv_violators("") == {}


def test_parse_power_and_period():
    power = parse_power(POWER_LOG)
    assert power["total_w"] == 2.38e-03 and power["total"]["leakage_w"] == 1.44e-05
    assert set(power["groups"]) == {"sequential", "combinational", "clock"}  # zero groups dropped
    assert power["groups"]["clock"]["pct"] == 16.8
    assert parse_clock_min_period("core_clock period_min = 0.49 fmax = 2033.83\n") == {
        "core_clock": {"period_min": 0.49, "fmax_mhz": 2033.83}}


def test_parse_ir_report():
    (report,) = parse_ir_report(IR_LOG)
    assert report["net"] == "VDD" and report["supply_voltage_v"] == 1.1 and report["drop_pct"] == 0.58
    assert report["worst_ir_drop_v"] == 6.37e-03 and report["average_ir_drop_v"] == 1.9e-03
    assert report["em_max_current_a"] == 3.53e-04 and report["em_resistors"] == 2168
    assert parse_ir_report("") == []


def test_parse_fill():
    out = parse_fill(FILL_LOG)
    assert out["fills_by_layer"] == {"met1": 0, "met2": 8418, "met3": 8492}  # FIN-0004 totals are cumulative
    assert out["total_fills"] == 16910 and out["skipped_layers"] == ["li1", "via"]


def test_windows_path():
    import mcp_signoff.server as so

    assert so.windows_path("/mnt/c/Users/x/out.def") == "C:\\Users\\x\\out.def"
    assert so.windows_path("/home/ansh/a.spef") == f"\\\\wsl.localhost\\{so.CFG.wsl_distro}\\home\\ansh\\a.spef"


def test_signoff_server_tools_and_prompts():
    import mcp_signoff.server as so

    tools = {t.name for t in asyncio.run(so.mcp.list_tools())}
    own = {"extract_parasitics", "signoff_timing", "power_analysis", "check_power_grid", "analyze_ir_drop",
           "density_fill", "signoff_checklist", "write_outputs", "timing_report_html"}
    shared = {"list_platforms", "platform_info", "validate_platform", "load_design", "save_checkpoint",
              "load_checkpoint", "list_checkpoints", "design_status", "estimate_parasitics", "snapshot",
              "session_status", "reset_session", "run_tcl"}
    assert tools == own | shared
    prompts = {p.name for p in asyncio.run(so.mcp.list_prompts())}
    assert prompts == {"signoff_review", "ir_drop_review", "tapeout_readiness"}
    assert so.CFG.timeout == 1800
