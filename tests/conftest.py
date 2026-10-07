import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openroad_common import Config, OpenRoadSession

TCLSH_CANDIDATES = [
    shutil.which("tclsh"),
    r"C:\msys64\ucrt64\bin\tclsh.exe",
    r"C:\msys64\mingw64\bin\tclsh.exe",
]
TCLSH = next((p for p in TCLSH_CANDIDATES if p and Path(p).exists()), None)

requires_tclsh = pytest.mark.skipif(TCLSH is None, reason="no tclsh found to stand in for openroad")


def tclsh_session(extra_tcl: str = "", timeout: float = 30) -> OpenRoadSession:
    """A session whose 'openroad' is a local tclsh running the real driver script."""
    cfg = Config.from_env("TEST")
    cfg = Config(**{**cfg.__dict__, "timeout": timeout, "max_output": 2000})
    return OpenRoadSession(cfg, "test", extra_tcl, command_builder=lambda _cfg, driver: [TCLSH, str(driver)])


@functools.cache
def openroad_available() -> bool:
    """OpenROAD reachable through WSL (NETLIST_E2E=1 forces yes, NETLIST_E2E=0 forces no)."""
    forced = os.environ.get("NETLIST_E2E")
    if forced in ("0", "1"):
        return forced == "1"
    if sys.platform != "win32" or not shutil.which("wsl.exe"):
        return False
    cfg = Config.from_env("NETLIST")
    try:
        out = subprocess.run(
            ["wsl.exe", "-d", cfg.wsl_distro, "--exec", "bash", "-lc", f"command -v {cfg.binary}"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


requires_openroad = pytest.mark.skipif(
    not openroad_available(), reason="OpenROAD is not reachable in WSL (set NETLIST_E2E=1 to force)"
)


@pytest.fixture
def netlist_server(monkeypatch):
    """The mcp-netlist server module with room for large recipe outputs and slow designs."""
    import mcp_netlist.server as srv

    cfg = srv.SESSION.cfg
    monkeypatch.setattr(
        srv.SESSION,
        "cfg",
        Config(**{**cfg.__dict__, "max_output": 50_000_000, "timeout": max(cfg.timeout, 1800)}),
    )
    return srv
