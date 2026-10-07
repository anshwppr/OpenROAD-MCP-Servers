"""Technology platform layer: Tcl list parsing, .vars normalization, merging, accessors (offline)."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from openroad_common.platform import (
    GENERIC_DEFAULTS,
    Platform,
    build_platform,
    layer_adjustments,
    normalize,
    parse_tapcell_args,
    resolve_path,
    tcl_split,
    vars_base_dir,
    vars_platform_name,
)


def test_tcl_split():
    assert tcl_split('a {b c} "d e" f\\ g') == ["a", "b c", "d e", "f g"]
    assert tcl_split("{{M2-M7} 0.25}") == ["{M2-M7} 0.25"]
    assert tcl_split("  ") == []
    with pytest.raises(ValueError):
        tcl_split("{unbalanced")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Nangate45.vars, sky130hd.vars and asap7.vars spell the same thing three ways.
        ("{metal2-metal10} 0.5", [["metal2-metal10", 0.5]]),
        ("{{metal2-metal10} 0.5}", [["metal2-metal10", 0.5]]),
        ("{met1 0.4} {met2 0.4} {met3 0.4}", [["met1", 0.4], ["met2", 0.4], ["met3", 0.4]]),
        ("{{M2-M7} 0.25}", [["M2-M7", 0.25]]),
        ([["M2", 0.3], ["M3", "0.2"]], [["M2", 0.3], ["M3", 0.2]]),
        ("", []),
        ("M2-M7", []),
    ],
)
def test_layer_adjustments(value, expected):
    assert layer_adjustments(value) == expected


def test_tapcell_args():
    parsed = parse_tapcell_args("-distance 25  -tapcell_master TAPCELL_ASAP7_75t_R  -endcap_master TAPCELL_ASAP7_75t_R")
    assert parsed == {"distance": 25.0, "tapcell_master": "TAPCELL_ASAP7_75t_R", "endcap_master": "TAPCELL_ASAP7_75t_R"}
    assert parse_tapcell_args("-distance 14 -tapcell_master x_tap") == {"distance": 14.0, "tapcell_master": "x_tap"}
    assert parse_tapcell_args(None) == {}


def test_paths_and_names():
    assert resolve_path("asap7/a.lef", "/home/u/OpenROAD/test") == "/home/u/OpenROAD/test/asap7/a.lef"
    assert resolve_path("/abs/x.lib", "/ignored") == "/abs/x.lib"
    assert resolve_path(r"C:\pdk\t.lef", None) == "/mnt/c/pdk/t.lef"
    assert vars_base_dir("/home/u/OpenROAD/test/sky130hd/sky130hd.vars") == "/home/u/OpenROAD/test"
    assert vars_platform_name("/x/Nangate45/Nangate45.vars") == "nangate45"


def test_normalize_vars_values():
    raw = {
        "platform": "sky130hd",
        "tech_lef": "sky130hd/sky130hd.tlef",
        "extra_lef": '"sky130hd/a.lef" "sky130hd/b.lef"',
        "liberty_files": '"fast" "sky130hd/ff.lib" "slow" "sky130hd/ss.lib"',
        "dont_use": "*probe_p_* *probec_p_*",
        "global_place_density": "0.3",
        "detail_place_pad": "2",
        "global_routing_layer_adjustments": "{met1 0.4} {met2 0.4}",
        "site": "unithd",
        "rcx_rules_file": "",
    }
    out = normalize(raw, "/t")
    assert "platform" not in out
    assert out["tech_lef"] == "/t/sky130hd/sky130hd.tlef"
    assert out["extra_lef"] == ["/t/sky130hd/a.lef", "/t/sky130hd/b.lef"]
    assert out["liberty_files"] == {"fast": "/t/sky130hd/ff.lib", "slow": "/t/sky130hd/ss.lib"}
    assert out["dont_use"] == ["*probe_p_*", "*probec_p_*"]
    assert out["global_place_density"] == 0.3 and out["detail_place_pad"] == 2.0
    assert out["global_routing_layer_adjustments"] == [["met1", 0.4], ["met2", 0.4]]
    assert out["rcx_rules_file"] is None


def test_merge_order_and_accessors():
    plat = build_platform(
        "mytech",
        "test",
        [
            ({"tech_lef": "t.lef", "std_cell_lef": "c.lef", "liberty_file": "typ.lib", "power_net": "VPWR"}, "/a"),
            ({"liberty_file": "/b/override.lib", "extra_liberty": ["x.lib"]}, "/b"),
        ],
    )
    assert plat.get("ground_net") == GENERIC_DEFAULTS["ground_net"]  # generic default survives
    assert plat.get("power_net") == "VPWR"  # .vars-style layer beats the default
    assert plat.lef_files() == ["/a/t.lef", "/a/c.lef"]
    assert plat.liberty_files() == ["/b/override.lib", "/b/x.lib"]  # later layer wins
    with pytest.raises(ToolError, match="no liberty corner 'ff'"):
        plat.liberty_files("ff")
    with pytest.raises(ToolError, match="does not define 'cts_buffer'"):
        plat.require("cts_buffer")
    round_trip = Platform.from_dict(plat.to_dict())
    assert round_trip.values == plat.values and round_trip.name == "mytech"


def test_generic_defaults_have_no_technology():
    text = " ".join(str(v) for v in GENERIC_DEFAULTS.values()).lower()
    for word in ("metal", "met1", "nangate", "sky130", "asap7"):
        assert word not in text
