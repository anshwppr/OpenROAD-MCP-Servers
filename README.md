# mcp-sta — OpenROAD Static Timing Analysis MCP Server

An [MCP](https://modelcontextprotocol.io) server that gives AI assistants (Claude Desktop, Claude Code,
or any MCP client) access to **static timing analysis with OpenROAD / OpenSTA**.

The server runs on Windows and drives one long-running `openroad` process inside **WSL Ubuntu**.
The design is loaded once. After that every tool call is a quick query against the live timing graph,
so you can ask for WNS/TNS, look at critical paths, chase down violations and try what-if clock
changes without reloading anything.

- 20 tools: loading, parasitics, timing summaries, path reports, design rules, clocks, power, object queries, what-ifs
- 6 prompts: ready-made workflows such as a sign-off review and setup/hold debugging
- Windows paths (`C:\...`) are converted to WSL paths (`/mnt/c/...`) automatically

---

## How it works

```
MCP client (Claude, Inspector)
        │ stdio (MCP protocol)
        ▼
mcp-sta server (Python, Windows)
        │ wsl.exe -d Ubuntu --exec bash -lc "exec openroad ... driver.tcl"
        ▼
openroad (inside WSL) running driver.tcl
```

- **One persistent process.** The first tool call starts `openroad` with a small Tcl *driver* script
  that is written to `%TEMP%\mcp_sta\driver.tcl`. It stays running until the server exits or you call
  `reset_session`.
- **Protocol.** Each tool builds a Tcl script and sends it on stdin as a single line,
  `<tag> <base64 script>`. The driver evaluates it and prints the output, then an error block if
  there was one, then an end marker. Base64 means file names and patterns never need escaping.
- **Errors.** Tcl and OpenROAD errors come back to the client as tool errors that include the command output.
- **Recovery.** If a command times out or `openroad` crashes, the session resets and the next call
  starts a fresh process. You then need to run `load_design` again.
- Calls are serialized, one Tcl command at a time.

---

## Requirements

| What | Version / notes |
|---|---|
| Windows 10/11 with **WSL 2** | an Ubuntu distro (default name `Ubuntu`) |
| **OpenROAD** installed *inside WSL* | tested with `26Q2-1164` (OpenSTA 3.1.0) |
| **uv** | for the Python environment (`winget install astral-sh.uv`) |
| Python | 3.13 (uv installs it if needed) |
| Node.js *(optional)* | only for testing with the MCP Inspector |

### Install OpenROAD in WSL

1. Open Ubuntu (`wsl -d Ubuntu`) and check the version with `lsb_release -a`.
2. Download the matching prebuilt `.deb` (Ubuntu 22.04 or 24.04, amd64) from
   <https://github.com/Precision-Innovations/OpenROAD/releases>.
3. Install it:
   ```bash
   cd /mnt/c/Users/<you>/Downloads
   sudo apt update
   sudo apt install ./openroad_*.deb
   openroad -version          # should print the version
   ```

If `openroad` is installed somewhere not on the default `PATH` (for example a source build or
OpenROAD-flow-scripts), set `STA_WSL_SETUP` (see [Configuration](#configuration)).

### Install the server

```powershell
cd "C:\Users\anshb\Downloads\MCP Server\mcp_sta"
uv sync
```

---

## Using it from an MCP client

### Claude Desktop

Add this to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "openroad-sta": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-sta"],
      "env": {
        "STA_WSL_DISTRO": "Ubuntu"
      }
    }
  }
}
```

Restart Claude Desktop. The tools show up under **openroad-sta**, and the prompts are in the prompt menu.

### Claude Code

```powershell
claude mcp add openroad-sta -e STA_WSL_DISTRO=Ubuntu -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-sta
```

### Run directly

```powershell
uv run mcp-sta        # speaks MCP over stdio; normally started by a client, not by hand
```

---

## Quick start: the Nangate45 `gcd` design

OpenROAD's repository has small test designs you can use.

**1. Get the test data (in WSL):**

```bash
git clone --depth 1 https://github.com/The-OpenROAD-Project/OpenROAD.git ~/OpenROAD
```

**2. Write a plain SDC.** The repo's `gcd_nangate45.sdc` calls `set_all_input_output_delays`, a helper
that only OpenROAD's own test scripts define, so it fails when loaded on its own:

```bash
mkdir -p ~/mcp_test
cat > ~/mcp_test/gcd.sdc <<'EOF'
create_clock -name core_clock -period 0.5 [get_ports clk]
set non_clk_inputs [lsearch -inline -all -not -exact [all_inputs] [get_ports clk]]
set_input_delay  0.1 -clock core_clock $non_clk_inputs
set_output_delay 0.1 -clock core_clock [all_outputs]
EOF
```

**3. Ask your assistant**, or call `load_design` yourself with these arguments
(replace `/home/ansh` with your WSL home directory, from `echo $HOME`):

```json
{
  "lef_files": ["/home/ansh/OpenROAD/test/Nangate45/Nangate45_tech.lef",
                "/home/ansh/OpenROAD/test/Nangate45/Nangate45_stdcell.lef"],
  "liberty_files": ["/home/ansh/OpenROAD/test/Nangate45/Nangate45_typ.lib"],
  "verilog_files": ["/home/ansh/OpenROAD/test/gcd_nangate45.v"],
  "top_module": "gcd",
  "sdc_file": "/home/ansh/mcp_test/gcd.sdc"
}
```

Then try `timing_summary`, `report_timing`, `list_violations`, or the `timing_signoff_review` prompt.

---

## Tools

### Session and loading

| Tool | What it does | Main arguments |
|---|---|---|
| `load_design` | Starts a fresh session and reads LEF → Liberty → netlist → SDC → SPEF, then returns design statistics. If the SDC or SPEF fails, the netlist **stays loaded**: fix the file and call `read_sdc` / `read_spef`. | `liberty_files`, `lef_files`, and **one** netlist source: `verilog_files` + `top_module`, `def_file`, or `db_file`; optional `sdc_file`, `spef_file` |
| `read_sdc` | Reads more constraints | `sdc_file` |
| `read_spef` | Reads extracted parasitics | `spef_file` |
| `set_wire_rc` | Wire RC per unit length, for parasitic estimation | `layer`, or `resistance` + `capacitance`; `target` = both / signal / clock |
| `estimate_parasitics` | Estimates wire parasitics for a **placed** design | `source` = placement / global_routing |
| `session_status` | Process state, loaded files, config | `check_connection` (starts OpenROAD and reports its version) |
| `reset_session` | Stops OpenROAD and forgets the design | none |

`lef_files` are required unless you use `db_file`, because OpenROAD links netlists against LEF masters.
Put the technology LEF first.

### Analysis

| Tool | What it does | OpenROAD command(s) |
|---|---|---|
| `timing_summary` | Setup and hold worst slack, TNS and WNS, with MET / VIOLATED / UNCONSTRAINED status and the clock list (structured output) | `report_worst_slack`, `report_tns`, `report_wns` |
| `report_timing` | Detailed path report. Options: `path_delay` (max / min / min_max), `from_points` / `through_points` / `to_points`, `group_path_count`, `endpoint_path_count`, `format` (full, full_clock, full_clock_expanded, short, end, summary, slack_only, **json**), `fields` (capacitance, slew, fanout, input_pin, net, src_attr), `digits`, `unconstrained`, `slack_max` | `report_checks` |
| `list_violations` | Failing endpoints, worst first. Options: `path_delay` (max / min / both), `max_endpoints` | `report_checks -format end -slack_max 0` |
| `check_design_rules` | Max slew, capacitance and fanout violations | `report_check_types -violators` |
| `check_setup` | Constraint problems: missing clocks, unconstrained endpoints, missing I/O delays, loops | `check_setup -verbose` |
| `report_clocks` | Clock definitions and setup/hold skew | `report_clock_properties`, `report_clock_skew` |
| `report_power` | Power by group. Options: `instances` for specific cells, `highest_n` for the top N instances | `report_power` |
| `design_statistics` | Area/utilization, cell usage, object counts | `report_design_area`, `report_cell_usage` |
| `timing_histogram` | Histogram of endpoint slack. Options: `num_bins`, `mode` (setup / hold) | `report_timing_histogram` |
| `inspect_object` | Details of one net, instance or pin (for a pin: the worst paths through it). Options: `name`, `kind` | `report_net`, `report_instance`, `report_checks -through` |
| `find_objects` | Finds objects by glob pattern. Options: `pattern`, `kind` (instance, net, pin, port, clock, lib_cell), `limit` | `get_cells`, `get_nets`, `get_pins`, ... |
| `set_clock_period` | What-if: redefines a clock with a new period, then returns the new timing summary. Options: `clock`, `period` | `create_clock` |
| `run_tcl` | Runs any OpenROAD Tcl in the live session (escape hatch). Disable with `STA_ALLOW_RAW_TCL=0`. | anything |

`from_points`, `through_points`, `to_points` and the pin form of `inspect_object` accept pins, ports,
instances or clocks. Glob patterns such as `*reg*/D` are allowed. Times and capacitances are in the
units of the first Liberty library read (usually ns and pF or fF).

---

## Prompts

Prompts are ready-made instructions that tell the assistant which tools to call and how to summarize the results.

| Prompt | Arguments | What it does |
|---|---|---|
| `load_and_analyze` | `liberty_files`, `netlist_file`, `top_module`, `sdc_file`, `lef_files` | Loads the design and gives a first health check |
| `timing_signoff_review` | none | Full review: summary, violations, worst paths, design rules, constraints, then a PASS / FAIL verdict with a ranked issue list |
| `debug_setup_violation` | `endpoint` | Traces the worst setup path, finds the root cause, suggests fixes |
| `debug_hold_violation` | `endpoint` | Same for hold, including clock skew |
| `constraint_audit` | none | Finds missing or suspicious SDC constraints and suggests the commands that fix them |
| `what_if_clock_period` | `clock`, `period_ns` | Compares timing before and after a period change, and estimates Fmax |

---

## Configuration

Set these as environment variables, for example in the `env` block of your MCP client config.

| Variable | Default | Meaning |
|---|---|---|
| `STA_WSL_DISTRO` | `Ubuntu` | WSL distro that has OpenROAD |
| `STA_BINARY` | `openroad` | Binary to run inside WSL |
| `STA_ARGS` | `-no_init -no_splash` | Extra command-line flags |
| `STA_THREADS` | `max` | Value passed to `-threads` (empty to omit it) |
| `STA_WSL_SETUP` | *(empty)* | Shell command run before `openroad`, e.g. `source ~/OpenROAD-flow-scripts/env.sh` or `export PATH=/opt/openroad/bin:$PATH` |
| `STA_TIMEOUT` | `600` | Seconds allowed per command |
| `STA_STARTUP_TIMEOUT` | `120` | Seconds allowed for OpenROAD to start (the first WSL start can be slow) |
| `STA_MAX_OUTPUT` | `60000` | Longer reports are cut to their head and tail |
| `STA_ALLOW_RAW_TCL` | `1` | Set to `0` to remove the `run_tcl` tool |

### File paths

- Windows paths are converted automatically: `C:\designs\top.v` becomes `/mnt/c/designs/top.v`,
  and `\\wsl$\Ubuntu\home\x` becomes `/home/x`.
- WSL paths (`/home/...`) are used as they are.
- **Use absolute paths.** Relative paths are resolved against the server's working directory,
  which is usually not what you expect. Prefer `/home/<you>/...` over `~/...`.
- Files are checked inside WSL before being read, so a wrong path gives `File not found inside WSL: ...`.

---

## Testing

### MCP Inspector (interactive)

```powershell
cd "C:\Users\anshb\Downloads\MCP Server\mcp_sta"
npx @modelcontextprotocol/inspector uv run mcp-sta
```

Open the URL it prints and click **Connect**. You can then call tools from the **Tools** tab and see the
rendered prompts in the **Prompts** tab. After changing `server.py`, click **Disconnect** and then **Connect**
to reload it.

### MCP Inspector (command line)

Every `--cli` call starts a **new** server, so a design loaded in one call is gone in the next.
Use it for one-off checks and the UI for multi-step sessions.

```powershell
npx @modelcontextprotocol/inspector --cli uv run mcp-sta --method tools/list
npx @modelcontextprotocol/inspector --cli uv run mcp-sta --method prompts/list
npx @modelcontextprotocol/inspector --cli uv run mcp-sta --method tools/call --tool-name session_status --tool-arg check_connection=true
```

PowerShell 5.1 strips the quotes inside JSON arguments such as `--tool-arg 'lef_files=[...]'`.
Run calls with list arguments from **Git Bash** instead.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `OpenROAD failed to start ... openroad: not found` | OpenROAD isn't on the WSL `PATH`. Install it, or set `STA_WSL_SETUP`. Check with `wsl -d Ubuntu --exec bash -lc "command -v openroad"`. |
| `File not found inside WSL: ...` | Wrong path. Use an absolute `/home/<you>/...` path, or a Windows path under a drive letter. |
| `invalid command name "set_all_input_output_delays"` | The SDC uses OpenROAD test-suite helpers. Write a plain SDC (see [Quick start](#quick-start-the-nangate45-gcd-design)). |
| `No design is loaded. Call load_design first.` | Load a design first. The session also resets after a timeout or crash. |
| `Command timed out after ...` | Large design or long report. Raise `STA_TIMEOUT`, or narrow the query (lower path counts, use `from_points` / `to_points`). |
| A tool rejects a flag | Your OpenROAD version has different options for that command. Use `run_tcl` with the right flags, and adjust the tool in `server.py`. |
| First call is slow | WSL is starting up. Later calls are fast. |

---

## Limitations

- OpenROAD must be installed inside WSL. There is no native Windows or Docker backend yet.
- `set_clock_period` re-creates the clock with a 50% duty cycle, so latency or uncertainty set on that
  clock may need re-applying. `load_design` restores the original constraints.
- `estimate_parasitics` needs a placed design (placed DEF or ODB). A plain synthesized netlist has no placement.
- One design per server; tool calls run one at a time.
- `run_tcl` can run any command, including ones that change or write files. Turn it off with
  `STA_ALLOW_RAW_TCL=0` if the client should not have that power.

---

## Project layout

```
mcp_sta/
├── pyproject.toml          # package metadata; dependency mcp[cli]>=2; entry point mcp-sta
├── README.md
└── src/mcp_sta/
    ├── __init__.py         # exposes main()
    └── server.py           # config, Tcl driver, StaSession, helpers, tools, prompts
```

Main parts of `server.py`:

- `Config`: environment settings.
- `DRIVER_TCL`: the stdin loop and helper procs (`__mcp_file`, `__mcp_objs`, `__mcp_names`).
- `StaSession`: process lifecycle, protocol, timeouts and recovery.
- `to_wsl_path`, `tcl_quote`, `tcl_list`: path and quoting helpers.
- The `@mcp.tool()` and `@mcp.prompt()` definitions.
