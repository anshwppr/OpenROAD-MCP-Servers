"""The mcp-netlist server as a real subprocess over MCP stdio (as an MCP client runs it)."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from conftest import openroad_available
from test_netlist_unit import EXPECTED_TOOLS


def server_params() -> StdioServerParameters:
    exe = Path(sys.executable).parent / ("mcp-netlist.exe" if os.name == "nt" else "mcp-netlist")
    if exe.exists():
        command, args = str(exe), []
    else:
        command, args = sys.executable, ["-c", "from mcp_netlist import main; main()"]
    # Pass the full environment: wsl.exe needs SYSTEMROOT and friends.
    return StdioServerParameters(command=command, args=args, env=dict(os.environ))


def test_stdio_lists_tools_and_status():
    async def scenario():
        async with Client(server_params(), read_timeout_seconds=120) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == EXPECTED_TOOLS
            status = (await client.call_tool("session_status", {})).structured_content
            assert status["design_loaded"] is False and status["raw_tcl_enabled"] is True

    asyncio.run(scenario())


@pytest.mark.openroad
@pytest.mark.skipif(not openroad_available(), reason="OpenROAD is not reachable in WSL")
def test_stdio_answers_a_question():
    async def scenario():
        async with Client(server_params(), read_timeout_seconds=600) as client:
            r = await client.call_tool("load_design", {"netlist": "test04"})
            assert not r.is_error, r.content
            counts = (await client.call_tool("gate_counts", {})).structured_content
            assert counts["total"] == 66 and counts["by_type"]["NOT"] == 10
            depth = (await client.call_tool("logic_depth", {"to_node": "n3", "from_node": "n0[0]"})).structured_content
            assert depth["depth"] == 10
            summary = (await client.call_tool("depth_summary", {})).structured_content
            assert summary["overall"]["depth"] == 14
            err = await client.call_tool("fanout", {"name": "nope"})
            assert err.is_error and "No port, net or gate named 'nope'" in err.content[0].text

    assert shutil.which("wsl.exe")
    asyncio.run(scenario())
