import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import requires_tclsh, tclsh_session
from openroad_common import Config, parse_records, tcl_list, tcl_quote, to_wsl_path, truncate


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\Users\ansh\designs\gcd.v", "/mnt/c/Users/ansh/designs/gcd.v"),
        (r"D:/libs/nangate45.lib", "/mnt/d/libs/nangate45.lib"),
        (r"\\wsl$\Ubuntu\home\ansh\x.lib", "/home/ansh/x.lib"),
        (r"\\wsl.localhost\Ubuntu\opt\pdk\a.lef", "/opt/pdk/a.lef"),
        ("/home/ansh/x.sdc", "/home/ansh/x.sdc"),
        ("~/x.sdc", "~/x.sdc"),
        ('"C:\\quoted\\path.def"', "/mnt/c/quoted/path.def"),
    ],
)
def test_to_wsl_path(path, expected):
    assert to_wsl_path(path) == expected


def test_to_wsl_path_empty():
    with pytest.raises(ToolError):
        to_wsl_path("  ")


def test_tcl_quote_escapes_substitution():
    assert tcl_quote('a[1]/$x "q" \\ z') == '"a\\[1\\]/\\$x \\"q\\" \\\\ z"'
    assert tcl_quote("two\nlines") == '"two\\nlines"'
    assert tcl_list(["slew", "net"]) == '[list "slew" "net"]'


def test_truncate_keeps_head_and_tail():
    text = "".join(f"line {i}\n" for i in range(10000))
    out = truncate(text, 1000)
    assert out.startswith("line 0") and out.rstrip().endswith("line 9999")
    assert "truncated" in out
    assert truncate("short", 1000) == "short"


def test_parse_records_splits_log():
    records, log = parse_records("hello\n@@\tinst\tu1\tINV_X1\n[INFO] x\n@@\ttotal\t1")
    assert records == [["inst", "u1", "INV_X1"], ["total", "1"]]
    assert log == "hello\n[INFO] x"


def test_config_prefix_precedence(monkeypatch):
    monkeypatch.setenv("OPENROAD_WSL_DISTRO", "Shared")
    monkeypatch.setenv("ODB_WSL_DISTRO", "OdbOnly")
    monkeypatch.setenv("STA_TIMEOUT", "5")
    assert Config.from_env("ODB").wsl_distro == "OdbOnly"
    assert Config.from_env("STA").wsl_distro == "Shared"
    assert Config.from_env("STA").timeout == 5.0
    monkeypatch.setenv("STA_ALLOW_RAW_TCL", "0")
    assert Config.from_env("STA").allow_raw_tcl is False


@requires_tclsh
def test_session_protocol():
    async def scenario():
        s = tclsh_session()
        try:
            assert await s.run("puts hello") == "hello"
            await s.run("set x 41")
            assert await s.run("puts [expr {$x + 1}]") == "42"
            out = await s.run('__mcp_rec inst "u 1" "tab\there" "new\nline"')
            assert parse_records(out)[0] == [["inst", "u 1", "tab here", "new line"]]
            # An empty last field (trailing tab) on the final line must survive.
            out = await s.run('__mcp_rec obstruction metal3 0 0 10 10 ""')
            assert parse_records(out)[0] == [["obstruction", "metal3", "0", "0", "10", "10", ""]]
            assert await s.run('puts "brace { \\} \\$d \\[b\\] ünïcødé"') == "brace { } $d [b] ünïcødé"

            with pytest.raises(ToolError) as err:
                await s.run('puts partial\nerror "boom failed"')
            assert "boom failed" in str(err.value) and "partial" in str(err.value)
            assert await s.run("puts alive") == "alive"

            big = await s.run("for {set i 0} {$i < 2000} {incr i} {puts \"line $i\"}")
            assert "truncated" in big and big.endswith("line 1999")
        finally:
            await s.close()
        assert not s.running

    asyncio.run(scenario())


@requires_tclsh
def test_session_recovers_from_timeout_and_crash():
    async def scenario():
        s = tclsh_session()
        try:
            s.design.loaded = True
            with pytest.raises(ToolError, match="timed out"):
                await s.run("after 5000", timeout=1)
            assert not s.running and not s.design.loaded
            assert await s.run("puts back") == "back"

            with pytest.raises(ToolError, match="exited unexpectedly"):
                await s.run("exit 3")
            assert await s.run("puts again") == "again"
        finally:
            await s.close()

    asyncio.run(scenario())


@requires_tclsh
def test_require_design():
    s = tclsh_session()
    with pytest.raises(ToolError, match="No design is loaded"):
        s.require_design()
