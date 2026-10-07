"""The netlist driver procs, run in a plain tclsh with small fakes of the OpenROAD commands."""

import asyncio

import pytest

from conftest import requires_tclsh, tclsh_session
from mcp_netlist.tcl_procs import NETLIST_DRIVER_TCL
from openroad_common import parse_records

FAKES = r"""
set ::calls {}
proc set_disable_timing {obj} { lappend ::calls "disable $obj" }
proc unset_disable_timing {obj} { lappend ::calls "enable $obj" }

# A glob-style lookup (like a pattern match): "n42[0]" also matches "n420".
set ::ports {n42[0] n42[1] n420 clk}
proc get_ports {args} {
  set pattern [lindex $args end]
  set out {}
  foreach p $::ports { if {[string match $pattern $p] || $p eq $pattern} { lappend out $p } }
  return $out
}
proc get_full_name {obj} { return $obj }

# write_verilog as OpenROAD 26Q2 writes constants: an undeclared one_ / zero_ net.
proc write_verilog {path} {
  set f [open $path w]
  puts $f "module top (a, q);"
  puts $f " input a;"
  puts $f " output q;"
  puts $f " wire one_;"
  puts $f " DFF g5 (.CLK(a),"
  puts $f "    .RN( zero_ ),"
  puts $f "    .SN(one_),"
  puts $f "    .D(a),"
  puts $f "    .Q(q));"
  puts $f " AND2 g6 (.A(one_x), .B(one_), .Y(w));"
  puts $f "endmodule"
  close $f
}
"""


def run(script: str) -> tuple[list[list[str]], str]:
    async def scenario():
        s = tclsh_session(NETLIST_DRIVER_TCL + FAKES)
        try:
            return parse_records(await s.run(script))
        finally:
            await s.close()

    return asyncio.run(scenario())


@requires_tclsh
def test_with_disabled_always_reenables():
    records, _ = run(
        "set rc [catch {__nl_with_disabled {g1 g2} { error boom }} msg]\n"
        "__mcp_rec rc $rc $msg\n"
        "__mcp_rec value [__nl_with_disabled {g3} { expr {6 * 7} }]\n"
        "foreach c $::calls { __mcp_rec call $c }"
    )
    assert ["rc", "1", "boom"] in records
    assert ["value", "42"] in records
    calls = [r[1] for r in records if r[0] == "call"]
    assert calls == ["disable g1", "disable g2", "enable g1", "enable g2", "disable g3", "enable g3"]


@requires_tclsh
def test_exact_lookup_ignores_glob_matches():
    records, _ = run(
        "__mcp_rec glob {*}[get_ports {n42[0]}]\n"
        "__mcp_rec exact {*}[__nl_exact get_ports {n42[0]}]\n"
        "__mcp_rec none {*}[__nl_exact get_ports n42]"
    )
    assert records[0] == ["glob", "n42[0]", "n420"]  # the fake treats [0] as a character class
    assert records[1] == ["exact", "n42[0]"]
    assert records[2] == ["none"]


@requires_tclsh
def test_write_verilog_restores_constants(tmp_path):
    out = tmp_path / "o.v"
    records, _ = run(
        "set ::__nl_const [dict create one_ 1 zero_ 0]\n"
        f"__nl_write_verilog {{{out.as_posix()}}} 1 1"
    )
    rec = next(r for r in records if r[0] == "written")
    assert dict(zip(rec[2::2], map(int, rec[3::2]))) == {"1'b1": 2, "1'b0": 1}
    text = out.read_text()
    assert ".SN(1'b1)" in text and ".RN(1'b0)" in text and ".B(1'b1)" in text
    assert ".A(one_x)" in text  # only whole constant-net connections are rewritten
    assert "wire one_" not in text and "one_)" not in text


@requires_tclsh
def test_write_verilog_refuses_to_overwrite(tmp_path):
    out = tmp_path / "o.v"
    out.write_text("keep")
    with pytest.raises(Exception, match="already exists"):
        run(f"set ::__nl_const [dict create]\n__nl_write_verilog {{{out.as_posix()}}} 0 1")
    assert out.read_text() == "keep"


@requires_tclsh
def test_constant_text():
    records, _ = run(
        "set ::__nl_const [dict create one_ 1 zero_ 0]\n"
        "__mcp_rec c [__nl_const_text one_] [__nl_const_text zero_] [__nl_const_text n5]"
    )
    assert records == [["c", "1'b1", "1'b0", "n5"]]
