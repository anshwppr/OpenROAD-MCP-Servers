import asyncio

import pytest

import mcp_odb.server as odb
from conftest import requires_tclsh, tclsh_session
from openroad_common import parse_records


@pytest.fixture
def dbu_2000(monkeypatch):
    monkeypatch.setattr(odb.SESSION.design, "dbu_per_micron", 2000)


def test_unit_conversion(dbu_2000):
    assert odb.um("200260") == 100.13
    assert odb.um2(4_000_000) == 1.0
    assert odb.to_dbu(1.5) == 3000


def test_record_converters(dbu_2000):
    inst = odb.inst_dict(["inst", "_401_", "INV_X1", "2000", "4000", "R0", "PLACED", "2000", "4000", "2380", "6800"])
    assert inst == {
        "name": "_401_",
        "master": "INV_X1",
        "location": [1.0, 2.0],
        "orient": "R0",
        "status": "PLACED",
        "bbox": [1.0, 2.0, 1.19, 3.4],
    }
    net = odb.net_dict(["net", "clk", "CLOCK", "35", "1", "27.085", "0", "0", "1"])
    assert net["routed_length_um"] == 27.085 and net["inst_pins"] == 35
    assert net["special"] is False and net["has_detailed_wire"] is True


def test_fixed_maps_to_firm():
    assert odb.db_status("FIXED") == "FIRM"
    assert odb.db_status("PLACED") == "PLACED"


def test_template_fill():
    assert odb.tcl("set a @A@; set b @B@", A='"x"', B=3) == 'set a "x"; set b 3'


# Minimal fake of the OpenDB Tcl API, enough to execute the ODB helper procs in tclsh.
FAKE_ODB = r"""
namespace eval ord {}
proc ord::get_db_block {} { return fake_block }
proc fake_block {method args} {
  switch $method {
    findInst { return [expr {[lindex $args 0] in {u1 u2} ? [lindex $args 0] : "NULL"}] }
    getInsts { return {u1 u2} }
    findNet { return NULL }
    getNets { return {} }
    getDieArea { return fake_rect }
  }
}
proc fake_rect {method} {
  switch $method { xMin {return 0} yMin {return 0} xMax {return 200260} yMax {return 201600} }
}
foreach __i {u1 u2} {
  proc $__i {method args} {
    set name [lindex [info level 0] 0]
    switch $method {
      getName { return $name }
      getMaster { return fake_master }
      getLocation { return {2000 4000} }
      getOrient { return R0 }
      getPlacementStatus { return PLACED }
      getBBox { return fake_rect }
    }
  }
}
proc fake_master {method args} { return INV_X1 }
"""


@requires_tclsh
def test_odb_driver_procs_run():
    async def scenario():
        s = tclsh_session(odb.ODB_DRIVER_TCL + FAKE_ODB)
        try:
            records, _ = parse_records(await s.run("foreach i [__odb_insts u*] { __odb_inst_rec $i }"))
            assert [r[1] for r in records] == ["u1", "u2"]
            assert records[0][2:7] == ["INV_X1", "2000", "4000", "R0", "PLACED"]
            exact, _ = parse_records(await s.run("__mcp_rec n [llength [__odb_insts u2]]"))
            assert exact == [["n", "1"]]
            with pytest.raises(Exception, match="Instance not found: nope"):
                await s.run("__odb_inst nope")
            box, _ = parse_records(await s.run("__mcp_rec box {*}[__odb_bbox [fake_block getDieArea]]"))
            assert box == [["box", "0", "0", "200260", "201600"]]
            assert (await s.run("puts [__odb_try {error x} fallback]")) == "fallback"
        finally:
            await s.close()

    asyncio.run(scenario())
