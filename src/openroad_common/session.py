"""Persistent OpenROAD process running in WSL, shared by the OpenROAD MCP servers.

A small Tcl driver script is the openroad command file: it reads one request per
stdin line (``<tag> <base64 Tcl script>``), evaluates it at global scope and prints
sentinel lines so the Python side knows where each command's output ends and
whether it failed. Each server adds its own helper procs to the driver.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from openroad_common.tcl import to_wsl_path, truncate


class OpenRoadError(ToolError):
    """An OpenROAD failure whose message is shown to the MCP client."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(prefix: str, name: str, default: str) -> str:
    """``{prefix}_{name}``, then ``OPENROAD_{name}``, then the default."""
    for key in (f"{prefix}_{name}", f"OPENROAD_{name}"):
        if key in os.environ:
            return os.environ[key]
    return default


@dataclass(frozen=True)
class Config:
    prefix: str
    wsl_distro: str
    binary: str
    args: list[str]
    threads: str
    wsl_setup: str
    timeout: float
    startup_timeout: float
    max_output: int
    allow_raw_tcl: bool

    @classmethod
    def from_env(cls, prefix: str, default_timeout: float = 600) -> Config:
        return cls(
            prefix=prefix,
            wsl_distro=_env(prefix, "WSL_DISTRO", "Ubuntu"),
            binary=_env(prefix, "BINARY", "openroad"),
            args=shlex.split(_env(prefix, "ARGS", "-no_init -no_splash")),
            threads=_env(prefix, "THREADS", "max"),
            wsl_setup=_env(prefix, "WSL_SETUP", ""),
            timeout=float(_env(prefix, "TIMEOUT", str(default_timeout))),
            startup_timeout=float(_env(prefix, "STARTUP_TIMEOUT", "120")),
            max_output=int(_env(prefix, "MAX_OUTPUT", "60000")),
            allow_raw_tcl=_env(prefix, "ALLOW_RAW_TCL", "1") not in ("0", "false", "no", ""),
        )


# ---------------------------------------------------------------------------
# Tcl driver
# ---------------------------------------------------------------------------

READY_SENTINEL = "__MCP_READY__"
_EXTRA_MARKER = "# @@EXTRA_TCL@@"

BASE_DRIVER_TCL = r"""
# mcp driver: one request per stdin line, "<tag> <base64 utf-8 Tcl script>".
fconfigure stdin -translation lf
fconfigure stdout -translation lf -encoding utf-8

# Normalize a path and fail with a clear message if it does not exist in WSL.
proc __mcp_file {path} {
  set p [file normalize $path]
  if {![file exists $p]} {
    error "File not found inside WSL: $path"
  }
  return $p
}

# Structured output: one record per line, "@@<TAB>field<TAB>field...".
proc __mcp_clean {s} {
  string map [list "\t" " " "\n" " " "\r" " "] $s
}

proc __mcp_rec {args} {
  set fields {}
  foreach arg $args {
    lappend fields [__mcp_clean $arg]
  }
  puts "@@\t[join $fields \t]"
}

# @@EXTRA_TCL@@

puts "__MCP_READY__"
flush stdout
while {[gets stdin line] >= 0} {
  set line [string trim $line]
  if {$line eq ""} {
    continue
  }
  lassign [split $line " "] __mcp_tag __mcp_payload
  set __mcp_script [encoding convertfrom utf-8 [binary decode base64 $__mcp_payload]]
  set __mcp_rc [catch {uplevel #0 $__mcp_script} __mcp_err]
  flush stdout
  if {$__mcp_rc == 1} {
    puts "\n__MCP_${__mcp_tag}_ERR__"
    puts $__mcp_err
  }
  puts "\n__MCP_${__mcp_tag}_END__"
  flush stdout
}
exit
"""


def driver_text(extra_tcl: str = "") -> str:
    return BASE_DRIVER_TCL.replace(_EXTRA_MARKER, extra_tcl.strip())


def write_driver(name: str, text: str) -> Path:
    driver_dir = Path(tempfile.gettempdir()) / "mcp_sta"
    driver_dir.mkdir(parents=True, exist_ok=True)
    driver = driver_dir / f"driver_{name}.tcl"
    driver.write_text(text, encoding="utf-8", newline="\n")
    return driver


def build_command(cfg: Config, driver: Path) -> list[str]:
    """wsl.exe command line that starts openroad on the driver script."""
    parts = [cfg.binary, *cfg.args]
    if cfg.threads:
        parts += ["-threads", cfg.threads]
    parts.append(to_wsl_path(str(driver)))
    inner = "exec " + " ".join(shlex.quote(p) for p in parts)
    if cfg.wsl_setup:
        inner = f"{cfg.wsl_setup} && {inner}"
    return ["wsl.exe", "-d", cfg.wsl_distro, "--exec", "bash", "-lc", inner]


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class DesignState:
    loaded: bool = False
    files: dict[str, list[str]] = field(default_factory=dict)
    top_module: str | None = None
    parasitics: str | None = None
    dbu_per_micron: int | None = None
    edits: list[str] = field(default_factory=list)
    # Resolved technology platform (see openroad_common.platform), set by the stage servers.
    platform: Any = None

    def add(self, kind: str, *paths: str) -> None:
        self.files.setdefault(kind, []).extend(paths)


CommandBuilder = Callable[[Config, Path], list[str]]


class OpenRoadSession:
    """One long-lived openroad process, serialized with an asyncio lock."""

    def __init__(
        self,
        cfg: Config,
        driver_name: str,
        extra_tcl: str = "",
        command_builder: CommandBuilder = build_command,
    ) -> None:
        self.cfg = cfg
        self.driver_name = driver_name
        self.driver_tcl = driver_text(extra_tcl)
        self._command_builder = command_builder
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._counter = 0
        self.command: list[str] = []
        self.startup_log = ""
        self.design = DesignState()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.running else None

    async def _readline(self) -> str | None:
        assert self._proc is not None and self._proc.stdout is not None
        raw = await self._proc.stdout.readline()
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").replace("\x00", "").rstrip("\r\n")

    async def _start_locked(self) -> None:
        cfg = self.cfg
        self.command = self._command_builder(cfg, write_driver(self.driver_name, self.driver_tcl))
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=32 * 1024 * 1024,
                env={**os.environ, "WSL_UTF8": "1"},
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise OpenRoadError(
                f"Cannot start {self.command[0]!r}. Install WSL, or run the server where WSL is available."
            ) from exc

        banner: list[str] = []
        try:
            async with asyncio.timeout(cfg.startup_timeout):
                while True:
                    line = await self._readline()
                    if line is None:
                        code = await self._proc.wait()
                        self._proc = None
                        raise OpenRoadError(
                            f"OpenROAD failed to start (exit code {code}).\n"
                            f"Command: {subprocess.list2cmdline(self.command)}\n"
                            f"Output:\n{truncate(chr(10).join(banner), 4000)}\n\n"
                            f"Check that '{cfg.binary}' is installed in WSL distro '{cfg.wsl_distro}' "
                            f"({cfg.prefix}_WSL_DISTRO), or set {cfg.prefix}_WSL_SETUP to a command that "
                            "puts it on PATH, e.g. 'source ~/OpenROAD-flow-scripts/env.sh'."
                        )
                    if line == READY_SENTINEL:
                        break
                    banner.append(line)
        except TimeoutError:
            await self._kill_locked()
            raise OpenRoadError(
                f"OpenROAD did not become ready within {cfg.startup_timeout:.0f}s.\n"
                f"Output so far:\n{truncate(chr(10).join(banner), 4000)}"
            ) from None
        self.startup_log = "\n".join(banner).strip()

    async def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
        self.design = DesignState()
        if proc is None or proc.returncode is not None:
            return
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            pass

    async def run(self, script: str, timeout: float | None = None, max_chars: int | None = None) -> str:
        """Run a Tcl script in the session and return its output.

        Output longer than ``max_chars`` (default: the configured MAX_OUTPUT) is cut
        to its head and tail. Raises OpenRoadError (with the command output) if the
        script raises a Tcl error, times out, or the process dies. Timeouts and
        crashes reset the session.
        """
        timeout = timeout or self.cfg.timeout
        max_chars = max_chars or self.cfg.max_output
        async with self._lock:
            if not self.running:
                await self._start_locked()
            assert self._proc is not None and self._proc.stdin is not None
            self._counter += 1
            tag = f"{self._counter:06d}"
            end_line, err_line = f"__MCP_{tag}_END__", f"__MCP_{tag}_ERR__"
            payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
            output: list[str] = []
            error: list[str] = []
            in_error = False
            try:
                async with asyncio.timeout(timeout):
                    self._proc.stdin.write(f"{tag} {payload}\n".encode("ascii"))
                    await self._proc.stdin.drain()
                    while True:
                        line = await self._readline()
                        if line is None:
                            await self._kill_locked()
                            raise OpenRoadError(
                                "The OpenROAD process exited unexpectedly; the session was reset "
                                "and the design must be loaded again.\nLast output:\n"
                                + truncate("\n".join(output), 4000)
                            )
                        if line == end_line:
                            break
                        if line == err_line:
                            in_error = True
                            continue
                        (error if in_error else output).append(line)
            except TimeoutError:
                await self._kill_locked()
                raise OpenRoadError(
                    f"Command timed out after {timeout:.0f}s. The OpenROAD session was restarted, "
                    f"so the design must be loaded again (raise {self.cfg.prefix}_TIMEOUT for large "
                    "designs).\nPartial output:\n" + truncate("\n".join(output), 4000)
                ) from None
            except (BrokenPipeError, ConnectionResetError):
                await self._kill_locked()
                raise OpenRoadError("Lost the connection to the OpenROAD process; the session was reset.") from None

        # Strip only blank lines: trailing tabs are empty fields of __mcp_rec records.
        text = "\n".join(output).strip("\n")
        if in_error:
            message = "\n".join(error).strip()
            detail = f"{message}\n--- output ---\n{text}" if text.strip() else message
            raise OpenRoadError(truncate(detail, self.cfg.max_output))
        return truncate(text, max_chars)

    async def restart(self) -> None:
        async with self._lock:
            await self._kill_locked()
            await self._start_locked()

    async def close(self) -> None:
        async with self._lock:
            proc = self._proc
            if proc is not None and proc.returncode is None and proc.stdin is not None:
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    pass
            await self._kill_locked()

    def require_design(self) -> None:
        if not self.running or not self.design.loaded:
            raise ToolError("No design is loaded. Call load_design first.")
