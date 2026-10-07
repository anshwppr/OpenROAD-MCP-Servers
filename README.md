# OpenROAD MCP Servers — `mcp-sta`, `mcp-odb`, `mcp-netlist`, `mcp-place`, `mcp-opt`, `mcp-route` and `mcp-signoff`

Independent [MCP](https://modelcontextprotocol.io) servers that give AI assistants (Claude Desktop,
Claude Code, or any MCP client) access to OpenROAD:

| Server | MCP name | What it covers |
|---|---|---|
| **`mcp-sta`** | `openroad-sta` | **Static timing analysis** with OpenSTA: WNS/TNS, critical paths, violations, clocks, power, what-if clock changes. 20 tools, 6 prompts. |
| **`mcp-odb`** | `openroad-odb` | **The OpenDB physical design database**: tech layers, cells, instances, nets, ports, rows, blockages, placement, wirelength, plus ECO edits and `write_def` / `write_db`. 33 tools, 5 prompts. See [OpenDB server](#opendb-server-mcp-odb). |
| **`mcp-netlist`** | `openroad-netlist` | **Gate-level netlist analysis** on a unit-delay library: gate counts, fanin/fanout cones, logic depth, path existence and dominance, cuts, constants, floating signals, renaming and `write_verilog`. 23 tools, 1 prompt. See [Netlist analysis server](#netlist-analysis-server-mcp-netlist). |
| **`mcp-opt`** | `openroad-opt` | **Design optimization**, the first of four physical-design stage servers: parasitic estimation, DRV repair, tie cells, clock tree synthesis, setup/hold repair, power recovery, legalization. Technology-independent (platforms), with checkpoints to hand designs to the other stages. 26 tools, 4 prompts. See [Optimization server](#optimization-server-mcp-opt). |
| **`mcp-place`** | `openroad-place` | **Floorplan and placement**, the stage before `mcp-opt`: floorplan, I/O pins, macro placement, tap cells, power grid, global and detailed placement, fillers, or the whole stage in one call. Technology-independent (platforms), hands off through checkpoints. 30 tools, 3 prompts. See [Placement server](#placement-server-mcp-place). |
| **`mcp-route`** | `openroad-route` | **Routing**, the stage after `mcp-opt`: routing layers and capacity adjustments, global routing with congestion reports, antenna check and repair, detailed routing, DRC reports, or the whole stage in one call. Technology-independent (platforms), hands off through checkpoints. 22 tools, 3 prompts. See [Routing server](#routing-server-mcp-route). |
| **`mcp-signoff`** | `openroad-signoff` | **Signoff**, the last stage: parasitic extraction (OpenRCX), signoff timing and power, power-grid connectivity and IR drop, metal density fill, a PASS/WARN/FAIL checklist, the final ODB/DEF/Verilog/SDC/SPEF files and an HTML timing report. Technology-independent (platforms). 22 tools, 3 prompts. See [Signoff server](#signoff-server-mcp-signoff). |

All run on Windows. Each drives its own long-running `openroad` process inside **WSL Ubuntu**.
The design is loaded once, and after that every tool call is a quick query against the live design.
Windows paths (`C:\...`) are converted to WSL paths (`/mnt/c/...`) automatically.

The servers share the session code in `src/openroad_common/`. Everything from
[How it works](#how-it-works) to [Prompts](#prompts) below describes `mcp-sta`. The
[OpenDB server](#opendb-server-mcp-odb) and [Netlist analysis server](#netlist-analysis-server-mcp-netlist)
sections cover what is different for `mcp-odb` and `mcp-netlist`.

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
    },
    "openroad-odb": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-odb"],
      "env": {
        "ODB_WSL_DISTRO": "Ubuntu"
      }
    },
    "openroad-netlist": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-netlist"],
      "env": {
        "NETLIST_WSL_DISTRO": "Ubuntu"
      }
    },
    "openroad-opt": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-opt"],
      "env": {
        "OPENROAD_PLATFORM": "nangate45"
      }
    },
    "openroad-place": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-place"],
      "env": {
        "OPENROAD_PLATFORM": "nangate45"
      }
    },
    "openroad-route": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-route"],
      "env": {
        "OPENROAD_PLATFORM": "nangate45"
      }
    },
    "openroad-signoff": {
      "command": "uv",
      "args": ["--directory", "C:\\Users\\anshb\\Downloads\\MCP Server\\mcp_sta", "run", "mcp-signoff"],
      "env": {
        "OPENROAD_PLATFORM": "nangate45"
      }
    }
  }
}
```

Restart Claude Desktop. The tools show up under **openroad-sta**, **openroad-odb** and **openroad-netlist**,
and the prompts are in the prompt menu. You can add only the servers you need.

### Claude Code

```powershell
claude mcp add openroad-sta -e STA_WSL_DISTRO=Ubuntu -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-sta
claude mcp add openroad-odb -e ODB_WSL_DISTRO=Ubuntu -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-odb
claude mcp add openroad-netlist -e NETLIST_WSL_DISTRO=Ubuntu -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-netlist
claude mcp add openroad-opt -e OPENROAD_PLATFORM=nangate45 -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-opt
claude mcp add openroad-place -e OPENROAD_PLATFORM=nangate45 -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-place
claude mcp add openroad-route -e OPENROAD_PLATFORM=nangate45 -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-route
claude mcp add openroad-signoff -e OPENROAD_PLATFORM=nangate45 -- uv --directory "C:\Users\anshb\Downloads\MCP Server\mcp_sta" run mcp-signoff
```

### Run directly

```powershell
uv run mcp-sta        # speaks MCP over stdio; normally started by a client, not by hand
uv run mcp-odb
uv run mcp-netlist
uv run mcp-opt
uv run mcp-place
uv run mcp-route
uv run mcp-signoff
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

## OpenDB server (`mcp-odb`)

`mcp-odb` works on the **physical design database** (OpenDB): the technology, the cell library, and the
placed or routed design. It uses the same WSL session mechanism as `mcp-sta`, but its own `openroad`
process, so the two servers never affect each other.

Differences from `mcp-sta`:
- **Units:** every coordinate and size, in and out, is in **microns**.
- **Results:** tools return structured JSON (lists and objects), not report text.
- **Editing:** edits change the design **in memory only**. `session_status` lists the unsaved edits.
  They are written to disk only by `write_def` or `write_db`, and lost on `reset_session` or a new `load_design`.
- **Placement status:** OpenDB has no `FIXED` status; DEF `FIXED` is stored as **`FIRM`**. Tools accept `FIXED`
  as input and report `FIRM`.

### Quick start

The placed and routed `gcd` from OpenROAD's test data works out of the box (paths inside WSL):

```json
{
  "lef_files": ["/home/ansh/OpenROAD/test/Nangate45/Nangate45_tech.lef",
                "/home/ansh/OpenROAD/test/Nangate45/Nangate45_stdcell.lef"],
  "def_file": "/home/ansh/OpenROAD/test/gcd_nangate45.def"
}
```

`load_design` returns the design summary: 734 instances, 499 nets, a die of 32.74 × 32.74 µm, and
2720 µm of routed wire.

### Tools

**Session and files**

| Tool | What it does |
|---|---|
| `load_design` | Fresh session: `lef_files` (tech LEF first) plus **one** of `def_file`, `db_file`, or `verilog_files` + `top_module`; `liberty_files` optional. Returns `design_summary`. |
| `write_def` / `write_db` | Write the design, including edits, to DEF or `.odb`. Refuses to overwrite an existing file unless `overwrite=true`. |
| `session_status` | Process state, loaded files, **unsaved edits**, config; `check_connection` reports the OpenROAD version |
| `reset_session` | Stop OpenROAD and discard the design and any unsaved edits |

**Inspection** (read-only)

| Tool | Returns |
|---|---|
| `tech_info` | DBU per micron, LEF version, manufacturing grid, layer and via counts, libraries, sites |
| `list_layers` | Per layer: type, direction, routing level, pitch, width, spacing, resistance, capacitance (`kind` = routing / all) |
| `list_masters` | Library cells: type, size, area, pin count (`pattern`, `master_type`, `limit`) |
| `master_info` | One cell: size, type, site, symmetry, pins |
| `design_summary` | Die and core area, object counts, placement-status and cell-type histograms, utilization, total routed wirelength, routed or not |
| `find_instances` | Instances matching `pattern`, optionally filtered by `master_pattern` and `status`: master, location, orientation, status, bbox |
| `instance_info` | One instance with every pin and its net |
| `find_nets` | Nets matching `pattern` / `sig_type`: pin counts, routed length, special or not |
| `net_info` | One net: drivers, all terminals and ports with locations, routed length, HPWL |
| `list_ports` | Top-level ports: direction, signal type, net, pin bbox, placement status |
| `list_rows` | Placement rows: site, origin, orientation, site count |
| `list_blockages` | Placement blockages and routing obstructions |
| `placement_report` | Status counts, unplaced instances, placed instances outside the core, macros |
| `wirelength_report` | Total HPWL, total routed wirelength, and the `top_n` longest nets |
| `region_query` | Instances overlapping a rectangle (`x1, y1, x2, y2`) |

**Editing** (in memory until written)

| Tool | What it does |
|---|---|
| `place_instance` | Place an instance at (x, y) with orientation and status; with `master`, creates it if missing |
| `move_instances` | Shift matching instances by (dx, dy); FIRM/LOCKED/COVER cells are skipped unless `include_fixed` |
| `set_placement_status` | Set the status of matching instances (e.g. FIXED to lock them) |
| `swap_master` | Replace an instance's cell with a pin-compatible one (e.g. resize a gate) |
| `delete_instance` | Delete an instance |
| `rename_object` | Rename an instance or a net |
| `create_net` / `delete_net` | Create an empty net with a signal type, or delete a net |
| `connect_pin` / `disconnect_pin` | Connect an instance pin to a net, or disconnect it |
| `create_blockage` | Placement blockage over a rectangle (`soft`, `max_density`) |
| `create_obstruction` | Routing obstruction on one layer |
| `run_tcl` | Any OpenROAD Tcl (escape hatch; edits made here are not tracked). Disable with `ODB_ALLOW_RAW_TCL=0`. |

Name `pattern`s are exact names or Tcl glob patterns (`*`, `?`). Bus brackets must be escaped:
`req_msg\[0\]`, or use an exact name.

### Prompts

| Prompt | Arguments | What it does |
|---|---|---|
| `design_overview` | none | Tech, summary, placement and wirelength, described in plain language |
| `placement_health_check` | none | Unplaced or out-of-core cells, utilization, blockages, macro positions, with an issue list |
| `inspect_net` | `net` | Explains a net's driver, loads and routing, and compares routed length with HPWL |
| `macro_placement_review` | none | Positions and spacing of BLOCK (macro) instances |
| `eco_move` | `instance`, `x`, `y` | Checks the target area for room, moves the cell and verifies it, then reminds you to write the design |

---

## Netlist analysis server (`mcp-netlist`)

`mcp-netlist` answers structural questions about **flat gate-level netlists**: the kind of questions in
`openroad_bundle/openroad_tasks.xlsx` (310 prompts over 39 testcases, test02 to test40). It uses OpenSTA's
timing graph and OpenDB in its own `openroad` process.

The bundled netlists (`openroad_bundle/netlists/<case>.v`) are mapped onto `unit_delay.lib`, where every gate
has delay 1. So a path's arrival time is its number of gates, and logic depth is read from unconstrained
timing paths. `unit_delay.lef` is a dummy LEF, needed only because OpenROAD will not link without one.

### Quick start

Call `load_design` with a case name such as `"test02"`. A file name, `"testcase/test02/test02.v"` or any path
also works. The bundled library and LEF are used by default. Then ask, for example:
- "How many NOT gates are in the design?" (`gate_counts`)
- "What is the maximum logic depth from n0[0] to n3?" (`logic_depth`)
- "Does a path from n0[0] to n4 exist that avoids n719?" (`path_exists`)

### Conventions

- **Gate** = cell instance. Prompt names map to library cells: NOT=INV, AND=AND2, OR=OR2, NAND=NAND2,
  NOR=NOR2, XOR=XOR2, XNOR=XNOR2, BUF, DFF.
- **Depth** = number of gates on a path.
  - Combinational paths start at primary inputs or DFF outputs, and end at primary outputs or DFF D pins.
  - The flip-flop's own clock-to-Q stage is not counted.
- **Cones stop at flip-flops.** DFFs at the boundary are reported separately (`boundary_dffs`).
  - The fanout cone of an internal net does not include the gate that drives it.
- **Names are literal.** Pass bus bits as `n42[0]`, with no braces or escaping.
  - A bus base name (`n42`) returns an error that lists its bits.
  - A gate name stands for its output signal.
- **Constants** (`1'b0` / `1'b1`):
  - OpenROAD reads them as the nets `zero_` / `one_`.
  - `load_design` marks their pins as don't-care (`set_logic_dc`), so constant propagation does not
    hide gates fed by constants. Pass `neutralize_constants=false` to keep OpenSTA's behaviour.
  - `write_verilog` writes them back as `1'b0` / `1'b1`. OpenROAD itself writes an undeclared `one_` net.
- **Edits.** `rename_object` changes the design in memory, and later questions see the new name.
  `write_verilog` writes the design (default `<case>_out.v` in `openroad_bundle/out/`).

### Tools

| Tool | Answers |
|---|---|
| `load_design` | Load a netlist (case name, file name or path) and summarize it |
| `write_verilog` | Write the current design (with renames); constants restored |
| `rename_object` | Rename a gate or an internal net (`kind` = auto / gate / net) |
| `gate_counts` | Total and per-type counts; `cell_type="NOT"` for one type |
| `io_summary` | Number of input/output bits, ports with bit widths |
| `list_gates` | Gates of a type with their pins; `clock_net` lists the flip-flops on a clock (paginated, `save_to`) |
| `gate_info` | Type and pin connections of one gate, with input drivers and output fanout |
| `fanout` | Gates driven directly by a gate, net or primary input |
| `fanout_ranking` | Highest-fanout primary inputs (or nets), with ties |
| `fanin_cone` | Transitive fanin cone: size, gates, per-type counts, boundary DFFs |
| `fanout_cone` | Transitive fanout cone / all reachable gates, and the outputs reached |
| `cone_intersection` | Gates shared by the fanin (or fanout) cones of several signals |
| `logic_depth` | Max depth from A to B, or of a signal's whole cone |
| `depth_summary` | Design-wide max depth per group (input→output, input→DFF, DFF→DFF, DFF→output); with `through_gate`, whether a gate is on a max-depth path |
| `output_cone_stats` | Per-output depth and cone size: deepest output, largest cone, outputs deeper than N |
| `path_exists` | Is there a combinational path from A to B (optionally avoiding nets or gates)? |
| `articulation_points` | Gates on every A→B path; with `gate`, "does every path pass through G?" |
| `cut_analysis` | Is a wire a cut between some primary input and output (the disconnected pairs) |
| `constant_inputs` | Gates with inputs tied to 0 / 1, optionally of one type |
| `floating_signals` | Unused inputs, undriven outputs, dangling nets, unconnected pins |
| `session_status`, `reset_session`, `run_tcl` | As in the other servers. The bundled `orhelp.tcl` procs (`node`, `gdepth`, ...) are loaded, so the spreadsheet's recipes run as written. |

The `netlist_question_guide` prompt maps question types to tools.

### How it is implemented

- Driver procs (`src/mcp_netlist/tcl_procs.py`) are hardened versions of `orhelp.tcl`.
- **Path existence, articulation points and cuts** are structural:
  - `get_fanout -trace_arcs enabled` from the start node, with gates blocked by `set_disable_timing`.
  - Every disable is undone, even on errors.
- **Depth** comes from `find_timing_paths -unconstrained`. An internal end node uses `-through`, because
  `-to` an internal pin finds nothing.

Three OpenSTA behaviours (26Q2) the server works around, which also affect the spreadsheet's recipes:
1. **Constant propagation.** A gate with a `1'b0`/`1'b1` input can block paths and cones entirely
   (`get_fanin` of such an output returns nothing). This is solved by `set_logic_dc` at load.
2. **Stale arrivals.** If the first timing update happens while a cell is disabled, `unset_disable_timing`
   leaves stale arrivals behind.
   - Example: test11 n29→n31[0] then reports 8 instead of 15.
   - The xlsx recipe sequence "Path Existence, then Logic Depth" hits this in a fresh session.
   - `load_design` therefore runs a full timing update first.
3. **Freed path objects.** Each `find_timing_paths` call frees the paths of the previous one, and reading an
   old path crashes OpenROAD. The procs read every result before the next search.

### Settings

Besides the usual `NETLIST_*` session variables (see [Configuration](#configuration)):

| Variable | Default | Meaning |
|---|---|---|
| `NETLIST_BUNDLE_DIR` | `openroad_bundle/` in the repo | Where the library, LEF, `orhelp.tcl` and netlists are |
| `NETLIST_LIB` / `NETLIST_LEF` | bundle `unit_delay.lib` / `unit_delay.lef` | Default library and LEF |
| `NETLIST_DIR` | bundle `netlists/` | Where case names and bare file names are looked up |
| `NETLIST_OUTPUT_DIR` | bundle `out/` | Where relative output paths go |

---

## Physical-design stage servers

The rest of the OpenROAD flow is split into four stage servers that run in this order:

```
mcp-place ─▶ checkpoint ─▶ mcp-opt ─▶ checkpoint ─▶ mcp-route ─▶ checkpoint ─▶ mcp-signoff
```

**All four stage servers are available**, so a synthesized netlist can be taken all the way to signed-off output
files through MCP. All of them share these rules:
- **Technology comes from a platform.** No tool contains a cell, layer or PDK name; a test enforces this.
- **Designs move between servers as checkpoints.**
- **The same 13 shared tools appear on every stage server.**

### Technology platforms

A platform holds everything technology-specific: LEF and Liberty files, site, tap and endcap cells, I/O and
wire-RC layers, CTS buffer, tie cells, fillers, dont_use list, routing layers and adjustments, PDN script,
RC files and RCX rules.

The keys are **OpenROAD's own `.vars` variable names** (`site`, `tapcell_args`, `wire_rc_layer`,
`cts_buffer`, `pdn_cfg`, `global_routing_layers`, ...), so any OpenROAD- or ORFS-style platform maps directly.

**Where platforms come from:**

| Kind | Where | Example |
|---|---|---|
| Built-in preset | `src/openroad_common/platforms/*.json` | `nangate45`, the test default (`"default": true`) |
| Discovered `.vars` | every `*/*.vars` under `OPENROAD_PLATFORMS_DIR` (default `/home/ansh/OpenROAD/test`) | `sky130hd`, `sky130hs`, `asap7` appear automatically |
| Your own JSON | `OPENROAD_USER_PLATFORMS_DIR` (default `%USERPROFILE%\.openroad_mcp\platforms`) or any path | your PDK |

**Adding your own technology (no code changes):**
1. **If you have an OpenROAD `.vars` file:** put it in a directory under `OPENROAD_PLATFORMS_DIR`, or pass its path
   as `platform=` to `load_design`.
2. **Otherwise, write a JSON file.** It can stand alone with absolute paths:
   ```json
   {
     "name": "mypdk",
     "base_dir": "/home/me/pdk",
     "tech_lef": "tech.lef", "std_cell_lef": "cells.lef", "liberty_file": "typ.lib",
     "site": "core", "tapcell_args": "-distance 20 -tapcell_master TAP -endcap_master ENDCAP",
     "io_placer_hor_layer": "M3", "io_placer_ver_layer": "M2",
     "wire_rc_layer": "M3", "wire_rc_layer_clk": "M5", "layer_rc_file": "rc.tcl",
     "cts_buffer": "CLKBUF_4", "tielo_port": "TIELO/L", "tiehi_port": "TIEHI/H",
     "filler_cells": "FILL*", "pdn_cfg": "pdn.tcl",
     "global_routing_layers": "M2-M6", "global_routing_clock_layers": "M4-M6"
   }
   ```
   Or it can extend a `.vars` file: `{"name": "x", "vars": "path/to/x.vars", "overrides": {"cts_buffer": "..."}}`.
   Relative paths are resolved against `base_dir`.
3. **Run `validate_platform`.** It checks that every file exists, and that the site, cells (tap, endcap, CTS buffer,
   tie cells, fillers, dont_use patterns) and layers exist in the LEF. It reports PASS / WARN / FAIL per key.
4. **Select it:** pass `platform="mypdk"` to `load_design`, or set `OPENROAD_PLATFORM`.

Every tool argument that has a platform default can also be overridden per call.

### Checkpoints (hand-off between stage servers)

**`save_checkpoint name`** writes three files to `$HOME/openroad_mcp/checkpoints` in WSL (`OPENROAD_CHECKPOINT_DIR`):
- `name.odb`: the design;
- `name.sdc`: the constraints, including propagated clocks after CTS;
- `name.json`: the manifest, holding the stage, server, time, note, Liberty files, the **fully resolved platform**
  and the step history.

**`load_checkpoint name`**, on any stage server:
- reloads Liberty → ODB → SDC;
- re-applies the platform's RC setup;
- re-estimates parasitics if the design is placed.

The checkpoint still works if the platform files change later. `list_checkpoints` shows what is available.

### Shared tools (every stage server)

| Tool | What it does |
|---|---|
| `list_platforms`, `platform_info`, `validate_platform` | See and check technology platforms |
| `load_design` | Fresh session: platform LEF/Liberty (or overrides), plus a Verilog + top, DEF or ODB netlist, plus SDC. Applies the platform's RC/dont_use/routing-layer setup. OpenROAD's reference SDC helper `set_all_input_output_delays` is available. |
| `save_checkpoint`, `load_checkpoint`, `list_checkpoints` | Hand-off between stages |
| `design_status` | Stage (netlist / floorplanned / placed / routed), counts, area and utilization, setup/hold WNS/TNS, DRV counts |
| `estimate_parasitics` | Placement or global-routing parasitics |
| `snapshot` | PNG image of the layout (headless renderer), with optional area and display options |
| `session_status`, `reset_session`, `run_tcl` | As in the other servers, plus the design's step history |

### Placement server (`mcp-place`)

Takes a synthesized netlist to a legal placement: floorplan, I/O pins, macros, tap cells, power grid, global and
detailed placement. Its `placed` checkpoint is what `mcp-opt` starts from.
- **Results:** like `mcp-opt`, every action returns `summary`, `before` / `after` and the `log` tail.
- **Defaults:** site, tracks file, pin layers, tapcell arguments, PDN script, densities, padding, macro halo and
  filler cells come from the platform. Every one can be overridden per call.
- **Timing follows placement:** after each placement step, placement parasitics are re-estimated (when the
  platform has wire RC), so `design_status` shows a meaningful setup/hold slack.

| Tool | What it does |
|---|---|
| `initialize_floorplan` | Die/core from `utilization` (+ `aspect_ratio`, `core_space`) or explicit `die_area` + `core_area`; rows, routing tracks (platform tracks file, else `make_tracks`), removes synthesis buffers, applies the platform's routing layers and adjustments |
| `place_io_pins` | Pin placement on the die edges (platform pin layers; min distance, corner avoidance, excluded edges, pin groups, annealing) |
| `set_io_pin_constraint`, `place_pin` | Restrict pins to edges/intervals by name or direction (or `clear`) / place one pin exactly |
| `report_io_pins` | Every port: direction, status, layer, location |
| `macro_placement`, `place_macro` | Hierarchical RTL macro placer (platform halo; reports when there are no macros) / place or move one macro |
| `insert_tapcells` | Tap and endcap cells from the platform's `tapcell_args` (distance and masters can be overridden) |
| `generate_pdn` | Platform PDN script + `pdngen`; grids built and wire counts per supply net. `replace=true` rips up an existing grid first |
| `global_placement` | Nesterov placement: density, routability- and timing-driven, incremental. **I/O pins are skipped automatically while unplaced.** Returns iterations, overflow, HPWL, congestion |
| `detailed_placement` | Legalization + `check_placement`: displacement and HPWL change |
| `optimize_placement` | `optimize_mirroring` + `improve_placement`: HPWL before → after |
| `check_placement` | Legality check |
| `filler_placement`, `remove_fillers` | Fill row gaps (platform filler pattern) / take them out |
| `floorplan_report` | Die/core, rows, tracks, pins placed, macros, tap cells, power-grid shapes, fillers |
| `place_design` | The whole stage in one call, in `flow.tcl` order: floorplan → macros → taps → PDN → global placement (skip I/O) → pins → routability-driven global placement → detailed placement (→ optional `optimize_placement`) |

Prompts: `floorplan_and_place`, `placement_review`, `io_pin_planning`.

**Verified results** (`place_design` with the die/core areas of OpenROAD's gcd test, compared with its reference run):

| Platform | Rows | Tap / endcap cells | I/O HPWL (reference) | Final overflow | Legal |
|---|---|---|---|---|---|
| nangate45 | 57 | 114 endcaps | 1967.32 µm (1967.3) | 0.099 | yes |
| sky130hd | 102 | 1040 taps | 6682.9 µm (6719.7) | 0.099 | yes |
| asap7 | 52 | 104 endcaps | 233.6 µm (243.6) | 0.099 | yes |

Only `platform=` differs between the three runs. The nangate45 and sky130hd `placed` checkpoints were then loaded into
`mcp-opt` and taken through `repair_design`, tie cells, legalization, CTS and `repair_timing`:

| Platform | Setup WNS (reference) | Hold WNS (reference) | DRV violations |
|---|---|---|---|
| nangate45 | −0.025 ns (−0.025) | 0.048 ns (0.048) | 0 |
| sky130hd | −0.590 ns (−0.567) | 0.485 ns (0.485) | 0 |

The macro placer was checked on its own test case (four macros placed, then one moved by hand).

### Walkthrough: netlist → placed → optimized → routed → signed off

```text
mcp-place:  load_design platform="nangate45" verilog_files=["/home/ansh/OpenROAD/test/gcd_nangate45.v"]
                        top_module="gcd" sdc_file="/home/ansh/OpenROAD/test/gcd_nangate45.sdc"
            place_design die_area=[0,0,100.13,100.8] core_area=[10.07,11.2,90.25,91]   (or utilization=30)
            save_checkpoint name="placed"
mcp-opt:    load_checkpoint name_or_path="placed"
            repair_design -> repair_tie_fanout -> legalize -> clock_tree_synthesis -> repair_timing
            save_checkpoint name="cts"
mcp-route:  load_checkpoint name_or_path="cts"
            route_design                        (global route -> antennas -> detailed route -> fillers)
            drc_report / routing_report
            save_checkpoint name="routed"
mcp-signoff: load_checkpoint name_or_path="routed"
            extract_parasitics -> signoff_timing -> power_analysis -> check_power_grid -> analyze_ir_drop
            signoff_checklist                   (PASS / WARN / FAIL per item)
            timing_report_html, write_outputs   (ODB, DEF, Verilog, SDC, SPEF)
```

### Optimization server (`mcp-opt`)

Optimizes a placed design: parasitic setup, DRV repair, tie cells, clock tree synthesis, setup/hold repair,
power recovery and legalization.
- **Results:** every action returns `summary` (parsed OpenROAD results), `before` / `after` (stage, area,
  utilization, WNS/TNS, DRV counts) and the `log` tail.
- **Defaults:** the CTS buffer, tie cells, padding and wire-RC layers come from the platform.

| Tool | What it does |
|---|---|
| `setup_parasitics` | Layer RC file + signal/clock wire RC (platform defaults), per-layer RC table, re-estimate |
| `set_dont_use`, `set_dont_touch` | Keep cells out of optimization / protect instances or nets (`unset` to undo) |
| `buffer_ports`, `remove_buffers` | Port buffering / remove buffers |
| `repair_design` | Fix max slew / cap / fanout and long wires; DRV counts before → after |
| `repair_tie_fanout` | Per-load tie cells (platform `tielo_port` / `tiehi_port`) |
| `clock_tree_synthesis` | `repair_clock_inverters` → CTS → propagated clocks → `repair_clock_nets` → legalize → re-estimate; sinks, buffers, depth, skew |
| `report_clock_tree` | CTS statistics and setup/hold skew |
| `repair_timing` | Setup and/or hold repair (margins, phases, utilization limit), then legalize; the final table row is read by its column headers |
| `recover_power` | Downsize gates with positive slack |
| `legalize` | Detailed placement + `check_placement` |
| `report_problems` | Floating/overdriven nets, longest wires, DRV violators |

Prompts: `fix_timing`, `cts_review`, `drv_cleanup`, `power_recovery`.

**Verified result:** gcd on nangate45, run as OpenROAD's `flow.tcl` does: placement, then `repair_design`,
legalization, CTS and `repair_timing`. It reaches final setup WNS **−0.025 ns**, the same as OpenROAD's reference run,
with 0 DRV violations and 35 clock sinks under 5 buffers. A checkpoint saved and reloaded in a fresh session
gives the same design.

### Routing server (`mcp-route`)

Routes a placed, clock-tree-synthesized design (the `cts` checkpoint from `mcp-opt`) to a DRC-clean,
antenna-clean layout. Its `routed` checkpoint is what `mcp-signoff` starts from.
- **Results:** every action returns `summary`, `before` / `after` and the `log` tail, like the other stage servers.
- **Defaults:** routing layers, layer adjustments, macro extension, diode cell and filler cells come from the
  platform. `configure_routing` changes them.
- **Reports** (DRC) go to `$HOME/openroad_mcp/reports` in WSL, not `/tmp`, because WSL clears `/tmp` when the
  distro goes idle.

| Tool | What it does |
|---|---|
| `configure_routing` | Signal/clock routing layers, per-layer and per-region capacity adjustments, macro extension; `reset_to_platform` re-applies the platform's settings |
| `global_route` | Pin access (once) → `global_route -verbose` → global-routing parasitics. Routed/clock nets, wirelength, vias, per-layer congestion table and overflow. On unremovable overflow it fails with the congestion table and what to try |
| `report_wire_length` | Wirelength per layer (global or detailed), or per net |
| `check_antennas` | Antenna violations (nets and pins) on the global or detailed routes |
| `repair_antennas` | Jumpers and/or diodes (platform `diode_cell`, else the library's antenna cell), then re-route. When there is no diode it says so instead of failing |
| `detailed_route` | Detailed routing with a DRC report: one row per iteration (violations, wirelength, vias), final violations by type × layer. Running it again continues from the existing wires (after antenna diodes); to start over, load the checkpoint again |
| `drc_report` | Reads the last DRC report (or `recheck=true` runs a fresh DRC check, optionally in an area): counts by type, layer, type × layer, the nets involved most, and each violation's box in µm |
| `routing_report` | One page: global/detailed routing done, guides, all nets routed, clocks propagated, antennas, wirelength per layer, DRC count |
| `route_design` | The whole stage in one call, in `flow.tcl` order: pin access → global route → antenna repair → detailed route → (antenna repair + detailed route) while violations remain → antenna check → fillers |

Prompts: `route_and_verify`, `fix_congestion`, `drc_triage`.

**Verified results** (`route_design` on the `cts` checkpoints made by `mcp-place` → `mcp-opt`, compared with this
OpenROAD binary's own run of `test/flow.tcl`):

| Platform | Global route | Detailed route (reference) | DRC | Antennas |
|---|---|---|---|---|
| nangate45 | 7897 µm, 2487 vias, overflow 0 (identical) | 5393 µm, 2191 vias (5392 µm), clean after 4 iterations (15 → 5 → 1 → 0) | 0 | 0 (no diode in the library, reported) |
| sky130hd | 20327 µm, 2234 vias, overflow 0 (20244 µm) | 14863 µm, 2023 vias (14817 µm) | 0 | 10 fixed by 9 jumpers + 1 diode after global routing; 11 fixed by 12 diodes after detailed routing, then re-routed; 0 left |

Only `platform=` differs. The `.metrics` files in OpenROAD's repository come from a newer OpenROAD than the
installed binary (for example 6521 µm / 2410 vias for nangate45), so the binary's own run of the reference flow
is the comparison. The routed databases were also loaded into `mcp-odb` (routed wirelength) and `mcp-sta`
(timing). A checkpoint saved after global routing keeps its route guides, so detailed routing can continue in a
new session (checked on asap7).

### Signoff server (`mcp-signoff`)

Signs off a routed design (the `routed` checkpoint from `mcp-route`) and writes the final files.
- **Defaults:** RCX rules, supply nets, supply voltage, fill rules and filler cells come from the platform. Without
  a `supply_voltage` the Liberty voltage is used.
- **Files** go to `$HOME/openroad_mcp/signoff/<design>_<platform>` and `$HOME/openroad_mcp/outputs/<design>_<platform>`
  in WSL. Every result also gives the Windows path (`\\wsl.localhost\Ubuntu\...`) so you can open it from Windows.
- **Extraction keeps the design intact.** OpenRCX re-encodes the routed wires while it extracts, and a design saved
  after that shows hundreds of false DRC shorts (OpenROAD's own flow never writes the design after extraction).
  `extract_parasitics` therefore saves the design first, extracts and writes the SPEF, then reloads the saved design
  with the SPEF. Timing uses the extracted parasitics; DRC checks and output files see the untouched routing.

| Tool | What it does |
|---|---|
| `extract_parasitics` | OpenRCX with the platform's rules (coupling threshold and resistor merging can be changed) → SPEF → read back. Nets, resistor segments, ground and coupling caps. A missing rules file is caught before OpenRCX reads it |
| `signoff_timing` | Setup/hold WNS and TNS, the worst endpoints, slew/cap/fanout violators, clock skew, minimum period (fmax) and total power, with a note when parasitics are not extracted |
| `power_analysis` | Internal / switching / leakage power per group (sequential, combinational, clock, …), optionally with an input activity or a VCD file |
| `check_power_grid` | Connectivity of each supply net (stripes, vias, cell supply pins); `require_terminals` also requires top-level supply ports |
| `analyze_ir_drop` | Static IR drop per supply net (PDNSim): worst and average drop, % of supply, the worst instances, voltage file, optional EM currents; voltage source file or source type can be given |
| `density_fill` | Metal fill from a fill-rules JSON (platform `fill_rules` or an argument): fill shapes per layer |
| `signoff_checklist` | PASS / WARN / FAIL for routed, DRC (fresh check), antennas, placement, floating nets, setup, hold, slew/cap/fanout, extracted parasitics, power-grid connectivity and IR drop, plus an overall verdict |
| `write_outputs` | ODB, DEF, Verilog (fillers left out, optional supply pins for LVS), SDC and SPEF. GDS needs KLayout or Magic and is not produced |
| `timing_report_html` | Interactive HTML report (layout, worst paths, timing, schematic and DRC panels) to open in a browser. The design data is inside the file, but the page loads its JavaScript libraries from public CDNs, so it needs internet access. OpenROAD 26Q2's exporter drops the three.js import, which leaves the page blank; the tool puts the import (and the schematic libraries) back |

Prompts: `signoff_review`, `ir_drop_review`, `tapeout_readiness`.

**Verified results** (the routed designs made by `mcp-place` → `mcp-opt` → `mcp-route`, compared with the final
report of this OpenROAD binary's own run of `test/flow.tcl`):

| Platform | Setup WNS / TNS (reference) | Hold WNS (reference) | Skew | Power | IR drop VDD / VSS | DRC on written ODB |
|---|---|---|---|---|---|---|
| nangate45 | −0.029 / −0.241 ns (−0.029 / −0.241) | 0.046 ns (0.046) | 0.003 ns (0.003) | 2.38 mW (2.38) | 0.57 % / 0.58 % | 0 |
| sky130hd | −0.547 ns (−0.538) | 0.483 ns (0.483) | | 1.48 mW | 0.01 % / 0.01 % | 0 |

The checklist passes every item except setup (the reference design misses timing by a few ps in OpenROAD's own run
too) and one max-capacitance pin, so its verdict is FAIL, as it should be. The written ODB + SPEF + SDC load
into `mcp-sta` with the same WNS. Density fill was checked on sky130hd with OpenROAD's fill rules (31,912 shapes on
met2–met5); nangate45 ships no fill rules, which the tool reports.

---

## Configuration

Set these as environment variables, for example in the `env` block of your MCP client config.
Each server reads its own prefix first (`STA_` for `mcp-sta`, `ODB_` for `mcp-odb`, `NETLIST_` for
`mcp-netlist`), then a shared `OPENROAD_` prefix, then the default. For example, `OPENROAD_WSL_DISTRO` applies to both servers, and
`ODB_TIMEOUT` only to `mcp-odb`. The table uses the `STA_` names.

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

**Stage server settings:**
- The prefixes are `PLACE_` (`mcp-place`), `OPT_` (`mcp-opt`), `ROUTE_` (`mcp-route`) and `SIGNOFF_`
  (`mcp-signoff`). Placement, optimization and signoff default to a 1800 s timeout and routing to 7200 s,
  because they take longer on large designs.
- Platforms and checkpoints use these shared variables:

| Variable | Default | Meaning |
|---|---|---|
| `OPENROAD_PLATFORM` | the preset marked `"default": true` (nangate45) | Platform used when a tool gets no `platform=` |
| `OPENROAD_PLATFORMS_DIR` | `/home/ansh/OpenROAD/test` | WSL directory scanned for `*/*.vars` platforms |
| `OPENROAD_USER_PLATFORMS_DIR` | `%USERPROFILE%\.openroad_mcp\platforms` | Your platform JSON files |
| `OPENROAD_CHECKPOINT_DIR` | `$HOME/openroad_mcp/checkpoints` (WSL) | Where checkpoints are written and read |

### File paths

- Windows paths are converted automatically: `C:\designs\top.v` becomes `/mnt/c/designs/top.v`,
  and `\\wsl$\Ubuntu\home\x` becomes `/home/x`.
- WSL paths (`/home/...`) are used as they are.
- **Use absolute paths.** Relative paths are resolved against the server's working directory,
  which is usually not what you expect. Prefer `/home/<you>/...` over `~/...`.
- Files are checked inside WSL before being read, so a wrong path gives `File not found inside WSL: ...`.

---

## Testing

### Unit tests

```powershell
uv run pytest
```

The tests cover path and quoting helpers, record parsing, unit conversion, and the full session protocol:
persistence, error capture, output truncation, and timeout and crash recovery. For the protocol tests a local
`tclsh` (for example `C:\msys64\ucrt64\bin\tclsh.exe`) stands in for `openroad`; they are skipped if none is found.
They do not need OpenROAD or WSL.

The stage-server tests come in three kinds:
- **`test_platform.py`:** Tcl list parsing, the three `.vars` layer-adjustment spellings, merge order and accessors.
- **`test_stage_parsing.py`:** the log parsers, run on real OpenROAD 26Q2 output.
- **`test_tech_guard.py`:** fails if any stage-server source names a technology.

`test_opt_e2e.py` (marker `openroad`) runs the real flow through `mcp-opt` on gcd. It covers platform
discovery/validation, every optimization tool, the snapshot and a checkpoint round trip.

`test_place_unit.py` checks the placement parsers on real logs, the routing setup and the tool/prompt lists.
`test_place_e2e.py` (marker `openroad`) covers:
- `place_design` on gcd for nangate45, sky130hd and asap7, compared with the reference I/O HPWL;
- the hand-off of the `placed` checkpoint to `mcp-opt`, through CTS and `repair_timing`;
- every placement tool step by step, including pin constraints, auto-skipped I/Os, PDN replace and fillers;
- macro placement on the macro placer's own test case.

`test_route_unit.py` checks the routing parsers (global route, congestion, wire length, antennas, detailed-route
iterations, DRC report) on real logs and the tool/prompt lists. `test_route_e2e.py` (marker `openroad`) covers:
- the full chain place → opt → `route_design` on gcd for nangate45 and sky130hd, compared with the binary's
  reference run, plus `mcp-odb` / `mcp-sta` checks of the routed database;
- every routing tool step by step, including a partial detailed route whose violations `drc_report` groups;
- asap7 global routing, and detailed routing from a checkpoint saved after global routing.

`test_signoff_unit.py` checks the signoff parsers (extraction, endpoints, slew/cap/fanout violators, power,
fmax, IR/EM reports, fill) on real output. `test_signoff_e2e.py` (marker `openroad`) builds routed gcd designs for
nangate45 and sky130hd through the place, opt and route servers, then runs every signoff tool and compares with
the reference run. It also checks that the written ODB is DRC clean in a new session and that `mcp-sta` gives the
same WNS from the written files, plus the error paths (unrouted design, missing rules, no SPEF yet).
To run only the offline tests: `uv run pytest -m "not openroad and not slow"`.

### Prompt acceptance tests for `mcp-netlist` (separate venv)

The 310 spreadsheet prompts are the acceptance tests for `mcp-netlist`. They run in their own virtual
environment, `.venv-tasks`, which leaves `.venv` and `uv.lock` alone:

```powershell
uv venv .venv-tasks --python 3.13
uv pip install --python .venv-tasks\Scripts\python.exe -e . pytest
.venv-tasks\Scripts\python.exe -m pytest tests -q                 # unit tests + small testcases
$env:NETLIST_TASKS_TIER="full"
.venv-tasks\Scripts\python.exe -m pytest tests\test_tasks_e2e.py -q  # all 39 testcases
```

How it works:
- `tests/tasks/export_tasks.py` turns the workbook into `tests/tasks/openroad_tasks.json`, using the standard
  library only. Rerun it if the workbook changes; a test checks that the two still agree.
- `tests/tasks/prompt_plans.py` maps every prompt to a check, using regexes on the prompt text.
  A test fails if any prompt is unmatched or matches twice.
- `tests/test_tasks_e2e.py` replays each testcase in order through an in-process MCP client, so renames
  carry over to later "now" questions. For each prompt it checks three things:
  - the tool's answer against **`tests/oracle/`**, an independent pure-Python netlist model;
  - the spreadsheet's own OpenROAD recipe, run through `run_tcl`, against the oracle;
  - for write prompts, a re-parse of the written netlist, which must be equivalent to the (renamed) design.
- Per-case reports (tool, oracle and recipe answers, timings) go to `tests/.artifacts/tasks/<case>.json`.
- `tests/test_netlist_capabilities.py` sweeps every signal and pair of small fixtures
  (`tests/fixtures/netlists/`: constants on gates, register paths, cuts, floating signals, bus names) against the oracle.

| Variable | Effect |
|---|---|
| `NETLIST_TASKS_TIER=full` | Also run the netlists over 250 KB (marked `slow`) |
| `NETLIST_TASKS_CASES=test31,test38` | Run only these cases |
| `NETLIST_TASKS_RECIPES=0` | Skip the spreadsheet recipes |
| `NETLIST_E2E=0` / `1` | Force the OpenROAD tests off / on (by default they run if `wsl.exe` finds `openroad`) |

### MCP Inspector (interactive)

```powershell
cd "C:\Users\anshb\Downloads\MCP Server\mcp_sta"
npx @modelcontextprotocol/inspector uv run mcp-sta     # or: uv run mcp-odb
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
| `mcp-odb` shows status `FIRM` after setting `FIXED` | Expected: OpenDB stores DEF `FIXED` as `FIRM`. |
| `mcp-odb` routed lengths are all 0 | The design has no detailed routing (routed lengths come from OpenROAD's `report_wire_length -detailed_route`). Use `wirelength_report`'s HPWL instead. |
| `place_io_pins`: `PPL-0111 ... does not have available slots` | A pin constraint region is too small for its pins at that spacing. Widen the region, lower `min_distance`, or make the die larger. |
| `global_route`: `GRT-0116` / congestion error | The design cannot be routed without overflow. Follow the `fix_congestion` prompt: smaller layer adjustments, more layers, or re-place at lower density. |
| `detailed_route` leaves DRC violations | Use `drc_report` to see the types and locations. Running `detailed_route` again continues from the current wires; to start over, `load_checkpoint` the unrouted checkpoint again. |
| `extract_parasitics`: `RCX-0487` / `RCX-0468` | The rules file could not be read. After a failed read OpenRCX stays broken for that session: `load_checkpoint` again, then extract with a valid rules file. |
| A DEF/ODB written by your own `run_tcl` after extraction shows DRC shorts | OpenRCX re-encodes wires. Use `write_outputs`, or write the design before extracting. |
| HTML timing report is blank | The browser has no internet access (the page loads Leaflet, Golden Layout and three.js from CDNs), or the file was written by `web_save_report` directly rather than by `timing_report_html` (the raw OpenROAD file stops at `THREE is not defined`). Regenerate it with `timing_report_html`. |
| `uv sync` fails with "file is being used by another process" | A server (for example an open MCP Inspector) is running `mcp-sta.exe` or `mcp-odb.exe`. Stop it and retry. |

---

## Limitations

- OpenROAD must be installed inside WSL. There is no native Windows or Docker backend yet.
- `set_clock_period` re-creates the clock with a 50% duty cycle, so latency or uncertainty set on that
  clock may need re-applying. `load_design` restores the original constraints.
- `estimate_parasitics` needs a placed design (placed DEF or ODB). A plain synthesized netlist has no placement.
- One design per server; tool calls run one at a time.
- `run_tcl` can run any command, including ones that change or write files. Turn it off with
  `STA_ALLOW_RAW_TCL=0` / `ODB_ALLOW_RAW_TCL=0` if the client should not have that power.
- `mcp-odb` has no undo. To throw edits away, run `load_design` again (or `reset_session`) before writing.
- `mcp-odb` queries walk the whole design in Tcl. So far they have only been tested on the small `gcd` design
  (734 instances). Very large designs may need a higher `ODB_TIMEOUT`, and `limit` and name patterns keep results small.
- The servers do not share a design. Load the files in each one you use.
- `mcp-netlist` expects flat netlists mapped onto library cells (like the bundled unit-delay netlists).
  Depth equals gate count only with a unit-delay library; with a real library `logic_depth` reports
  arrival times instead.

---

## Project layout

```
mcp_sta/
├── pyproject.toml              # all servers; dependency mcp[cli]>=2; entry points mcp-sta, mcp-odb, mcp-netlist, mcp-opt, mcp-place, mcp-route, mcp-signoff
├── README.md
├── openroad_bundle/            # unit_delay.lib/.lef, orhelp.tcl, netlists/test02..40.v, openroad_tasks.xlsx
├── src/
│   ├── openroad_common/        # shared by all servers
│   │   ├── session.py          # Config, Tcl driver, OpenRoadSession (WSL process, protocol, recovery)
│   │   ├── tcl.py              # to_wsl_path, tcl_quote, tcl_list, tcl_file, truncate, parse_records
│   │   ├── records.py          # tcl() templates, by_kind/first/total, query()
│   │   ├── parsing.py          # OpenROAD log parsers (slack, rsz tables by header, cts, dpl, ...)
│   │   ├── platform.py         # technology platforms: .vars reader, JSON, merge, discovery
│   │   ├── platforms/          # built-in presets (nangate45.json)
│   │   └── stage.py            # shared stage tools: platforms, load, checkpoints, status, snapshot
│   ├── mcp_sta/server.py       # STA tools and prompts
│   ├── mcp_odb/server.py       # OpenDB helper procs, tools and prompts
│   ├── mcp_netlist/            # netlist analysis: server.py (tools), tcl_procs.py (driver procs), names.py
│   ├── mcp_opt/server.py       # optimization stage server (est, rsz, cts, dpl)
│   ├── mcp_place/server.py     # placement stage server (ifp, ppl, mpl, tap, pdn, gpl, dpl)
│   ├── mcp_route/server.py     # routing stage server (pin access, grt, ant, drt)
│   └── mcp_signoff/server.py   # signoff stage server (rcx, sta, psm, fin, outputs)
└── tests/                      # pytest: helpers, parsing, session protocol (tclsh)
    ├── oracle/                 # independent pure-Python netlist model (reference answers)
    ├── tasks/                  # xlsx export, prompt -> check mapping, checks
    └── fixtures/netlists/      # small netlists for edge cases
```

How the pieces fit:

- **Driver script.** It is written to `%TEMP%\mcp_sta\driver_<sta|odb|netlist>.tcl` and run as the openroad
  command file. It reads one base64-encoded Tcl script per stdin line and prints sentinel lines around the output.
- **Structured output.** Tools emit records with `__mcp_rec`, one tab-separated line starting with `@@`,
  and `parse_records` turns them into JSON.
- **Server-specific helpers.** Each server adds its own Tcl helper procs to the driver: `__mcp_objs` for STA,
  `__odb_*` for OpenDB, `__nl_*` for the netlist server.
