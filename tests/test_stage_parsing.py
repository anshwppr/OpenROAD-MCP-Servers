"""Log parsers against real OpenROAD 26Q2 output (snippets copied from actual runs and golden logs)."""

from openroad_common.parsing import (
    final_rows,
    parse_check_placement,
    parse_clock_skew,
    parse_cts,
    parse_design_area,
    parse_dpl,
    parse_repair_design,
    parse_repair_timing,
    parse_rsz_counts,
    parse_slack_report,
)
from openroad_common.stage import drv_counts

REPAIR_TIMING_26Q2 = """\
[INFO RSZ-0094] Found 24 endpoints with setup violations.
   Iter   | Removed | Resized | Inserted | Cloned |  Pin  |   Area   |    WNS   |   StTNS    |   EnTNS    |  Viol  |  Worst
          | Buffers |  Gates  | Buffers  |  Gates | Swaps |          |          |            |            | Endpts | St/EnPt
------------------------------------------------------------------------------------------------------------------------------
       0* |       0 |       0 |        0 |      0 |     0 |    +0.0% |   -0.059 |       -1.2 |       -0.4 |     24 | resp_msg[15]
    final |       0 |      54 |       20 |      0 |    12 |   +11.8% |   -0.025 |       -0.5 |       -0.2 |     12 | resp_msg[15]
------------------------------------------------------------------------------------------------------------------------------
[INFO RSZ-0040] Inserted 20 buffers.
[INFO RSZ-0051] Resized 54 instances: 54 up, 0 up match, 0 down, 0 VT
[INFO RSZ-0043] Swapped pins on 12 instances.
[WARNING RSZ-0062] Unable to repair all setup violations.
[INFO RSZ-0046] Found 35 endpoints with hold violations.
Iteration | Resized | Buffers | Cloned Gates |   Area   |   WNS   |   TNS   | Endpoint
        0 |       0 |       0 |            0 |    +0.0% |   0.145 |   0.000 | _863_/D
    final |       0 |      84 |            0 |    +9.8% |   0.200 |   0.000 | _870_/D
[INFO RSZ-0032] Inserted 84 hold buffers.
"""

# The older 11-column layout from OpenROAD's golden repair_setup1.ok.
REPAIR_TIMING_OLD = """\
   Iter   | Removed | Resized | Inserted | Cloned |  Pin  |   Area   |    WNS   |    TNS     |  Viol  |  Worst
          | Buffers |  Gates  | Buffers  |  Gates | Swaps |          |          |            | Endpts | Endpoint
---------------------------------------------------------------------------------------------------------
      final |       4 |       1 |        0 |      0 |     0 |    -4.6% |    0.013 |        0.0 |      0 | r2/D
[INFO RSZ-0059] Removed 4 buffers.
"""


def test_repair_timing_reads_columns_from_headers():
    out = parse_repair_timing(REPAIR_TIMING_26Q2)
    setup, hold = out["setup_final"], out["hold_final"]
    assert setup["wns"] == -0.025 and setup["inserted_buffers"] == 20 and setup["resized_gates"] == 54
    assert setup["sttns"] == -0.5 and setup["viol_endpts"] == 12 and setup["worst_st_enpt"] == "resp_msg[15]"
    assert hold["buffers"] == 84 and hold["wns"] == 0.2 and hold["endpoint"] == "_870_/D"
    assert out["setup_violating_endpoints_found"] == 24 and out["hold_violating_endpoints_found"] == 35
    assert out["inserted_buffers"] == 20 and out["hold_buffers"] == 84 and out["pin_swaps"] == 12
    assert out["warnings"] == ["[WARNING RSZ-0062] Unable to repair all setup violations."]

    old = parse_repair_timing(REPAIR_TIMING_OLD)
    assert old["setup_final"]["removed_buffers"] == 4 and old["setup_final"]["tns"] == 0.0
    assert old["removed_buffers"] == 4


def test_repair_design():
    log = """\
Iteration |   Area    | Resized | Buffers | Nets repaired | Remaining
---------------------------------------------------------------------
        0 |     +0.0% |       0 |       0 |             0 |       418
    final |     +0.3% |       1 |       1 |             2 |         0
---------------------------------------------------------------------
[INFO RSZ-0034] Found 1 slew violations.
[INFO RSZ-0036] Found 1 capacitance violations.
[INFO RSZ-0039] Resized 1 instances.
[INFO RSZ-0038] Inserted 1 buffers in 2 nets."""
    out = parse_repair_design(log)
    assert out["slew_violations_found"] == 1 and out["cap_violations_found"] == 1
    assert out["inserted_buffers"] == 1 and out["nets_buffered"] == 2 and out["resized"] == 1
    assert out["final"] == {"area": 0.3, "resized": 1, "buffers": 1, "nets_repaired": 2, "remaining": 0}


def test_cts_and_skew():
    log = """\
[INFO CTS-0050] Root buffer is BUF_X4.
[INFO CTS-0010]  Clock net "clk" has 35 sinks.
[INFO CTS-0008] TritonCTS found 1 clock nets.
[INFO CTS-0018]     Created 5 clock buffers.
[INFO CTS-0012]     Minimum number of buffers in the clock path: 2.
[INFO CTS-0013]     Maximum number of buffers in the clock path: 2.
[INFO CTS-0100]  Leaf buffers 0
[INFO CTS-0101]  Average sink wire length 119.71 um
Clock core_clock
 0.0533 source latency _679_/CK ^
-0.0553 target latency _673_/CK ^
 0.0000 CRPR
--------------
-0.0020 setup skew
"""
    out = parse_cts(log)
    assert out["sinks"] == {"clk": 35} and out["buffers_created"] == 5 and out["clock_nets"] == 1
    assert out["avg_sink_wire_um"] == 119.71 and out["root_buffer"] == "BUF_X4"
    assert parse_clock_skew(log + "Clock core_clock\n-0.0010 hold skew\n") == {
        "core_clock": {"setup": -0.002, "hold": -0.001}
    }


def test_dpl_and_check_placement():
    log = """\
[INFO DPL-0006] Core area: 6398.36 um^2, Instances area: 533.33 um^2, Utilization: 8.3%
Placement Analysis
---------------------------------
total displacement        271.0 u
average displacement        0.6 u
max displacement            2.8 u
original HPWL            3983.4 u
legalized HPWL           4191.9 u
delta HPWL                    5 %
[WARNING RSZ-0062] not a placement warning"""
    out = parse_dpl(log)
    assert out["total_displacement_um"] == 271.0 and out["delta_hpwl_pct"] == 5 and out["utilization_pct"] == 8.3
    assert "warnings" not in out  # only DPL warnings are kept
    assert parse_check_placement(log) == {"passed": True, "failures": []}
    bad = "[WARNING DPL-0006] Site aligned check failed (1).\n[ERROR DPL-0033] detailed placement checks failed"
    assert parse_check_placement(bad) == {"passed": False, "failures": ["Site aligned check failed (1)"]}


def test_small_parsers():
    assert parse_design_area("Design area 608 um^2 9% utilization.") == {"area_um2": 608, "utilization_pct": 9}
    assert parse_rsz_counts("[INFO RSZ-0028] Inserted 18 BUF_X1 output buffers.") == {"output_buffers": 18}
    assert parse_slack_report("worst slack max -0.0250\ntns max -0.1757\nworst slack min INF\n") == {
        "setup": {"worst_slack": -0.025, "tns": -0.1757},
        "hold": {"worst_slack": "INF"},
    }
    assert final_rows("no table here") == []


def test_drv_counts_from_report_check_types():
    log = (
        "== drv max_slew\n"
        "max slew\n\nPin  Limit  Slew  Slack\n_1_/A 0.2 0.3 -0.1 (VIOLATED)\n_2_/A 0.2 0.25 -0.05 (VIOLATED)\n"
        "== drv max_capacitance\n\n"
        "== drv max_fanout\nmax fanout\n_3_/Z 10 12 -2 (VIOLATED)\n"
        "== drv end\n"
    )
    assert drv_counts(log) == {"max_slew": 2, "max_cap": 0, "max_fanout": 1}
    assert drv_counts("nothing") is None
