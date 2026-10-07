import asyncio
import importlib
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import mcp_netlist.names as names
import mcp_netlist.server as srv

EXPECTED_TOOLS = {
    "load_design", "write_verilog", "rename_object", "session_status", "reset_session", "run_tcl",
    "gate_counts", "io_summary", "list_gates", "gate_info",
    "fanout", "fanout_ranking", "fanin_cone", "fanout_cone", "cone_intersection",
    "logic_depth", "depth_summary", "output_cone_stats",
    "path_exists", "articulation_points", "cut_analysis",
    "constant_inputs", "floating_signals",
}  # fmt: skip


def test_tool_set_and_annotations():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert set(tools) == EXPECTED_TOOLS
    for name in ("gate_counts", "fanin_cone", "logic_depth", "path_exists", "cut_analysis"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["write_verilog"].annotations.destructive_hint is True
    assert asyncio.run(srv.mcp.list_prompts())[0].name == "netlist_question_guide"


def test_run_tcl_can_be_disabled(monkeypatch):
    monkeypatch.setenv("NETLIST_ALLOW_RAW_TCL", "0")
    try:
        module = importlib.reload(srv)
        assert "run_tcl" not in {t.name for t in asyncio.run(module.mcp.list_tools())}
    finally:
        monkeypatch.delenv("NETLIST_ALLOW_RAW_TCL")
        importlib.reload(srv)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("n42[0]", "n42[0]"), ("{n42[0]}", "n42[0]"), ('"g7"', "g7"), (r"n42\[3\]", "n42[3]"), (r"\n5[1] ", "n5[1]")],
)
def test_normalize_name(raw, expected):
    assert names.normalize_name(raw) == expected


def test_normalize_name_rejects_empty():
    with pytest.raises(ToolError):
        names.normalize_name(" {} ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("NOT", "INV"), ("not gates", "INV"), ("NANDs", "NAND2"), ("xnor", "XNOR2"), ("flip-flop", "DFF"), ("AND2", "AND2")],
)
def test_normalize_cell_type(raw, expected):
    known = ["AND2", "BUF", "DFF", "INV", "NAND2", "NOR2", "OR2", "XNOR2", "XOR2"]
    assert names.normalize_cell_type(raw, known) == expected


def test_normalize_cell_type_unknown():
    with pytest.raises(ToolError, match="Unknown gate type"):
        names.normalize_cell_type("MUX", ["INV", "BUF"])


def test_group_port_bits_and_natural_sort():
    bits = ["n5[10]", "n5[2]", "n5[0]", "n5[1]", "clk", "n10[0]"]
    assert names.group_port_bits(bits) == [
        {"name": "clk", "width": 1, "msb": None, "lsb": None},
        {"name": "n5", "width": 4, "msb": 10, "lsb": 0},
        {"name": "n10", "width": 1, "msb": 0, "lsb": 0},
    ]
    assert sorted(["g10", "g2", "g1"], key=names.natural_key) == ["g1", "g2", "g10"]


def test_resolve_netlist(tmp_path, monkeypatch):
    (tmp_path / "test02.v").write_text("module top; endmodule\n")
    monkeypatch.setenv("NETLIST_DIR", str(tmp_path))
    for spec in ("test02", "test02.v", "testcase/test02/test02.v", str(tmp_path / "test02.v")):
        path, _how = names.resolve_netlist(spec)
        assert path == (tmp_path / "test02.v").resolve()
    with pytest.raises(ToolError, match="Netlist not found"):
        names.resolve_netlist("test99")


def test_resolve_output(tmp_path, monkeypatch):
    monkeypatch.setenv("NETLIST_OUTPUT_DIR", str(tmp_path))
    assert names.resolve_output(None, "test05") == tmp_path / "test05_out.v"
    assert names.resolve_output("x/y.v", None) == tmp_path / "x" / "y.v"
    assert names.resolve_output(r"C:\abs\o.v", None) == Path(r"C:\abs\o.v")
    with pytest.raises(ToolError):
        names.resolve_output(None, None)


def test_bundle_defaults_exist():
    assert names.default_liberty().is_file()
    assert names.default_lef().is_file()
    assert names.default_orhelp().is_file()
    assert (names.netlist_dir() / "test04.v").is_file()


# Records as printed by __nl_depth for a register-start path q1 -> g2 -> g3 -> w4 (through w4).
PATH_RECORDS = [
    ["meta", "net", "g3/Y", "", ""],
    ["path", "reg", "0", "r1/Q", "z", "4.0"],
    ["pt", "reg", "0", "r1/Q", "1.000000", "r1"],
    ["pt", "reg", "0", "g2/A", "1.0", ""],
    ["pt", "reg", "0", "g2/Y", "2.0", "g2"],
    ["pt", "reg", "0", "g3/A", "2.0", ""],
    ["pt", "reg", "0", "g3/Y", "3.000001", "g3"],
    ["pt", "reg", "0", "g9/Y", "4.0", "g9"],
    ["pt", "reg", "0", "z", "4.0", ""],
]


def test_path_measurement_register_start_through_pin():
    meta, best = srv.best_measure(PATH_RECORDS)
    assert meta["sink"] == "g3/Y"
    # Measured at the through pin, minus the CLK->Q stage; the DFF itself is not a gate on the path.
    assert best == {"depth": 2, "startpoint": "r1/Q", "gates": ["g2", "g3"]}


def test_path_measurement_from_internal_net():
    records = [["meta", "output", "z", "net", "g2/Y"]] + PATH_RECORDS[1:]
    _meta, best = srv.best_measure(records)
    assert best["depth"] == 2 and best["gates"] == ["g3", "g9"]


def test_answer_helpers():
    assert srv.a_an("AND") == "an AND" and srv.a_an("NOT") == "a NOT" and srv.a_an("XNOR") == "an XNOR"
    assert srv.name_list([f"g{i}" for i in range(60)], 3) == "g0, g1, g2 ..."
    assert srv.page(list(range(10)), 8, 5) == ([8, 9], False)
    assert srv.page(list(range(10)), 0, 5) == ([0, 1, 2, 3, 4], True)
