"""The pure-Python oracle against hand-checked answers (no OpenROAD needed)."""

from pathlib import Path

import pytest

from oracle import BUNDLE, Design, parse_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "netlists"


@pytest.fixture(scope="module")
def t04():
    return Design.from_file(BUNDLE / "netlists" / "test04.v")


def fixture(name: str) -> Design:
    return Design.from_file(FIXTURES / f"{name}.v")


def test_test04_anchors(t04):
    assert dict(t04.gate_counts()) == {"NOR2": 21, "OR2": 17, "AND2": 13, "INV": 10, "XNOR2": 4, "XOR2": 1}
    assert t04.port_widths("input") == {"n0": 3, "n1": 4, "n2": 9} and t04.outputs == ["n3"]
    assert t04.depth_to("n3") == 14
    assert [t04.depth_between(s, "n3") for s in ("n0[0]", "n1[0]", "n2[8]")] == [10, 9, 6]
    assert t04.fanout("n0[0]")["gates"] == ["g12", "g19", "g52", "g53"]
    assert [n for n, k in t04.fanout_ranking() if k == 4] == ["n0[0]", "n2[4]", "n2[6]"]
    assert len(t04.fanin_cone("n3")[0]) == 66


def test_test31_matches_the_spreadsheet():
    d = Design.from_file(BUNDLE / "netlists" / "test31.v")
    assert len(d.fanout_cone("n2")[0]) == 343  # xlsx: reachability of n2
    assert d.depth_summary()["pi_to_reg"] == 14  # xlsx: PI -> D
    assert d.gate_pins("g0") == {"A": "n4490", "Y": "n4404"}
    assert d.gate_pins("g1027") == {"D": "n1747", "CLK": "n0", "RN": "n1", "SN": "1'b1", "Q": "n16"}
    f = d.floating()
    assert len(f["unused_inputs"]) == 16 and f["undriven_outputs"] == ["n17", "n18"]


def test_seq_small():
    d = fixture("seq_small")
    assert d.depth_summary() == {"pi_to_po": 2, "pi_to_reg": 2, "reg_to_reg": 3, "reg_to_po": 1, "overall": 3}
    assert d.output_depths() == {"y": 1, "z": 2}
    assert d.fanin_cone("z") == ({"g6", "g7"}, {"r1"})
    assert d.dffs_on_clock("clk") == ["r1", "r2"]
    assert d.on_max_depth_path("g3") and not d.on_max_depth_path("g6")
    assert d.fanout_cone("clk") == (set(), {"r1", "r2"}, set())
    assert d.depth_between("q1", "w5") == 3


def test_cut_diamond():
    d = fixture("cut_diamond")
    assert d.articulation_points("a", "y") == ["g0", "g3", "g4"]
    assert d.articulation_points("b", "z") is None
    assert d.cut_pairs("w1") == ["a->y", "a->z"]
    assert d.cut_pairs("w2") == []
    assert d.cut_pairs("w4") == ["a->y"]
    assert d.path_exists("a", "y", ["w2"]) and not d.path_exists("a", "y", ["w4"])


def test_const_mix():
    d = fixture("const_mix")
    assert d.constant_inputs("0") == ["g0", "g5"]
    assert d.constant_inputs("1") == ["g1", "g2", "g5"]
    assert d.constant_inputs("any", "NAND2") == ["g2"]
    # Constants are not path starts, but gates fed by them are traversed.
    assert d.depth_to("y") == 3 and d.path_exists("a", "y")


def test_floating():
    assert fixture("floating").floating() == {
        "unused_inputs": ["a[1]", "c"],
        "undriven_outputs": ["u"],
        "dangling_gate_outputs": ["w2"],
        "undriven_nets": [],
    }


def test_rename_and_equivalence():
    a, b = fixture("seq_small"), fixture("seq_small")
    assert a.equivalent(b)
    b.rename("instance", "g3", "renamed_gate")
    b.rename("net", "w4", "renamed_wire")
    assert not a.equivalent(b)
    assert b.gate_pins("renamed_gate") == {"A": "w3", "Y": "renamed_wire"}
    assert b.fanout("renamed_wire")["gates"] == ["g4"]
    with pytest.raises(ValueError):
        b.rename("net", "y", "nope")


def test_parses_openroad_output_style():
    text = """module top (a,
    y);
 input a;
 output y;

 INV g0 (.A(a),
    .Y(w));
 DFF g1 (.CLK(a),
    .RN(zero_),
    .SN(one_),
    .D(w),
    .Q(y));
endmodule
"""
    nl = parse_text(text, undeclared_const_nets=True)
    assert nl.instances["g1"].pins == {"CLK": "a", "RN": "1'b0", "SN": "1'b1", "D": "w", "Q": "y"}
    assert nl.instances["g0"].pins == {"A": "a", "Y": "w"}
