"""Parsers that turn OpenROAD log output into structured results.

Every parser is written against real log lines (message IDs from OpenROAD's golden
``.ok`` files) and tolerates missing lines: absent values are simply left out.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def number(text: str) -> float | str:
    """Float for numeric text (``+4.6%`` -> 4.6); the text itself otherwise or when not finite."""
    try:
        value = float(text.strip().rstrip("%").replace("+", ""))
    except ValueError:
        return text.strip()
    return value if math.isfinite(value) else text.strip()


def grab(log: str, pattern: str, cast: Callable[[str], Any] = number, group: int = 1) -> Any:
    """The last match of ``pattern`` (multiline), cast; None when absent."""
    matches = re.findall(pattern, log, re.MULTILINE)
    if not matches:
        return None
    match = matches[-1]
    value = match[group - 1] if isinstance(match, tuple) else match
    return cast(value)


def grab_all(log: str, pattern: str, cast: Callable[[str], Any] = number) -> list[Any]:
    out = []
    for match in re.findall(pattern, log, re.MULTILINE):
        out.append(cast(match) if not isinstance(match, tuple) else tuple(cast(m) for m in match))
    return out


def grab_sum(log: str, pattern: str) -> int | None:
    values = grab_all(log, pattern, int)
    return sum(values) if values else None


def messages(log: str, level: str = "WARNING", tool: str = "[A-Z]+") -> list[str]:
    """Distinct ``[LEVEL TOOL-NNNN] text`` lines, in order (optionally only one tool, e.g. ``DPL``)."""
    seen: list[str] = []
    for line in re.findall(rf"^\[{level} {tool}-\d+\].*$", log, re.MULTILINE):
        if line not in seen:
            seen.append(line)
    return seen


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v not in (None, [], {})}


def _snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def final_rows(log: str, label: str = "final") -> list[dict[str, Any]]:
    """Rows labelled ``label`` in ``|`` tables, keyed by their (possibly two-line) column headers.

    OpenROAD's progress tables (repair_design, repair_timing setup/hold, ...) change columns
    between versions, so names are taken from the header lines above each table, e.g.
    ``Removed / Buffers`` -> ``removed_buffers``.
    """
    rows: list[dict[str, Any]] = []
    header: list[list[str]] = []
    in_header = False
    for line in log.splitlines():
        if "|" not in line:
            in_header = False
            continue
        cells = [c.strip() for c in line.split("|")]
        first_cell = cells[0].rstrip("*")
        if first_cell == label:
            names = [_snake(" ".join(h[i] for h in header if i < len(h))) or f"col{i}"
                     for i in range(len(cells))]
            row = {}
            for name, value in zip(names[1:], cells[1:]):
                row[name] = number(value) if re.fullmatch(r"[-+]?[\d.]+%?", value) else value
            rows.append(row)
        elif first_cell and not re.fullmatch(r"[-\d.]+", first_cell):
            header = header + [cells] if in_header else [cells]
            in_header = True
        elif not first_cell and in_header:
            header.append(cells)
        else:
            in_header = False
    return rows


# ---------------------------------------------------------------------------
# OpenSTA
# ---------------------------------------------------------------------------

_SLACK_RE = re.compile(r"^(worst slack|tns|wns)\s+(max|min)\s+(\S+)", re.MULTILINE)


def parse_slack_report(text: str) -> dict[str, dict[str, float | str]]:
    """Parse ``worst slack max X`` / ``tns max X`` / ``wns max X`` lines."""
    result: dict[str, dict[str, float | str]] = {"setup": {}, "hold": {}}
    for kind, min_max, value in _SLACK_RE.findall(text):
        check = "setup" if min_max == "max" else "hold"
        result[check][kind.replace(" ", "_")] = number(value)
    return result


def parse_design_area(log: str) -> dict[str, Any]:
    """``Design area 751 um^2 12% utilization.`` (rsz report_design_area)."""
    m = re.search(r"Design area\s+(\S+)\s+um\^2\s+(\S+)%\s+utilization", log)
    return {"area_um2": number(m.group(1)), "utilization_pct": number(m.group(2))} if m else {}


# ---------------------------------------------------------------------------
# Resizer (rsz)
# ---------------------------------------------------------------------------

def parse_repair_timing(log: str) -> dict[str, Any]:
    # The setup table has "Removed Buffers"/"Inserted Buffers" columns; the hold table does not.
    finals = final_rows(log)
    setup = next((r for r in finals if "removed_buffers" in r or "inserted_buffers" in r), None)
    hold = next((r for r in finals if r is not setup and "wns" in r), None)
    return compact(
        {
            "setup_violating_endpoints_found": grab(log, r"RSZ-0094\] Found (\d+) endpoints with setup", int),
            "hold_violating_endpoints_found": grab(log, r"RSZ-0046\] Found (\d+) endpoints with hold", int),
            "setup_final": setup,
            "hold_final": hold,
            "inserted_buffers": grab_sum(log, r"RSZ-(?:0040|0045)\] Inserted (\d+) buffers"),
            "hold_buffers": grab(log, r"RSZ-0032\] Inserted (\d+) hold buffers", int),
            "resized": grab_sum(log, r"RSZ-(?:0051|0132)\] Resized (\d+) instances"),
            "removed_buffers": grab(log, r"RSZ-0059\] Removed (\d+) buffers", int),
            "pin_swaps": grab(log, r"RSZ-0043\] Swapped pins on (\d+) instances", int),
            "cloned": grab(log, r"RSZ-0049\] Cloned (\d+) instances", int),
            "no_setup_violations": "RSZ-0098" in log or None,
            "no_hold_violations": "RSZ-0033" in log or None,
            "warnings": messages(log),
        }
    )


def parse_repair_design(log: str) -> dict[str, Any]:
    final = next(iter(final_rows(log)), None)
    inserted = re.search(r"RSZ-(?:0038|0055)\] Inserted (\d+) buffers in (\d+) nets", log)
    return compact(
        {
            "slew_violations_found": grab(log, r"RSZ-0034\] Found (\d+) slew", int),
            "fanout_violations_found": grab(log, r"RSZ-0035\] Found (\d+) fanout", int),
            "cap_violations_found": grab(log, r"RSZ-0036\] Found (\d+) cap", int),
            "long_wires_found": grab(log, r"RSZ-0037\] Found (\d+) long wires", int),
            "inserted_buffers": int(inserted.group(1)) if inserted else None,
            "nets_buffered": int(inserted.group(2)) if inserted else None,
            "resized": grab(log, r"RSZ-0039\] Resized (\d+) instances", int),
            "final": final,
            "warnings": messages(log),
        }
    )


def parse_rsz_counts(log: str) -> dict[str, Any]:
    """Buffer insertion/removal and tie-cell messages from smaller rsz commands."""
    return compact(
        {
            "input_buffers": grab(log, r"RSZ-0027\] Inserted (\d+) \S+ input buffers", int),
            "output_buffers": grab(log, r"RSZ-0028\] Inserted (\d+) \S+ output buffers", int),
            "removed_buffers": grab(log, r"RSZ-0026\] Removed (\d+) buffers", int),
            "tie_cells_inserted": grab_sum(log, r"RSZ-0042\] Inserted (\d+) tie"),
            "warnings": messages(log),
        }
    )


# ---------------------------------------------------------------------------
# Clock tree synthesis (cts)
# ---------------------------------------------------------------------------


def parse_cts(log: str) -> dict[str, Any]:
    sinks = {net: int(n) for net, n in re.findall(r'CTS-0010\]\s+Clock net "([^"]+)" has (\d+) sinks', log)}
    return compact(
        {
            "clock_nets": grab(log, r"CTS-0008\] TritonCTS found (\d+) clock nets", int),
            "sinks": sinks,
            "buffers_created": grab_sum(log, r"CTS-0018\]\s+Created (\d+) clock buffers"),
            "min_buffers_in_path": grab(log, r"CTS-0012\]\s+Minimum number of buffers in the clock path: (\d+)", int),
            "max_buffers_in_path": grab(log, r"CTS-0013\]\s+Maximum number of buffers in the clock path: (\d+)", int),
            "leaf_buffers": grab(log, r"CTS-0100\]\s+Leaf buffers (\d+)", int),
            "avg_sink_wire_um": grab(log, r"CTS-0101\]\s+Average sink wire length (\S+) um"),
            "root_buffer": grab(log, r"CTS-0050\] Root buffer is (\S+?)\.?$", str),
            "warnings": messages(log),
        }
    )


def parse_clock_skew(log: str) -> dict[str, Any]:
    """``report_clock_skew``: ``Clock <name>`` blocks ending in ``<value> setup|hold skew``."""
    out: dict[str, dict[str, float]] = {}
    clock = None
    for line in log.splitlines():
        m = re.match(r"^Clock (\S+)\s*$", line)
        if m:
            clock = m.group(1)
            continue
        m = re.match(r"^\s*(-?[\d.]+)\s+(setup|hold) skew\s*$", line)
        if m and clock:
            out.setdefault(clock, {})[m.group(2)] = float(m.group(1))
    return out


# ---------------------------------------------------------------------------
# Detailed placement (dpl)
# ---------------------------------------------------------------------------

_DPL_KEYS = {
    "total moves": "total_moves",
    "total displacement": "total_displacement_um",
    "average displacement": "average_displacement_um",
    "max displacement": "max_displacement_um",
    "original HPWL": "original_hpwl_um",
    "legalized HPWL": "legalized_hpwl_um",
    "delta HPWL": "delta_hpwl_pct",
}


def parse_dpl(log: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, key in _DPL_KEYS.items():
        value = grab(log, rf"^\s*{re.escape(label)}\s+(-?[\d.]+)")
        if value is not None:
            out[key] = value
    out["utilization_pct"] = grab(log, r"Utilization: (\S+?)%")
    out["warnings"] = messages(log, tool="DPL")
    return compact(out)


def parse_check_placement(log: str) -> dict[str, Any]:
    failures = re.findall(r"\[WARNING DPL-\d+\] (.+? check failed \((\d+)\))", log)
    return {
        "passed": not failures and "DPL-0033" not in log,
        "failures": [f[0] for f in failures],
    }


def parse_optimize_mirroring(log: str) -> dict[str, Any]:
    return compact(
        {
            "mirrored": grab(log, r"DPL-0020\] Mirrored (\d+) instances", int),
            "hpwl_before_um": grab(log, r"DPL-0021\] HPWL before\s+(\S+) u"),
            "hpwl_after_um": grab(log, r"DPL-0022\] HPWL after\s+(\S+) u"),
            "delta_hpwl_pct": grab(log, r"DPL-0023\] HPWL delta\s+(\S+) %"),
        }
    )


def parse_improve_placement(log: str) -> dict[str, Any]:
    """``improve_placement``: the ``Detailed Improvement Results`` block."""
    return compact(
        {
            "original_hpwl_um": grab(log, r"^Original HPWL\s+(\S+) u"),
            "final_hpwl_um": grab(log, r"^Final HPWL\s+(\S+) u"),
            "delta_hpwl_pct": grab(log, r"^Delta HPWL\s+(\S+) %"),
            "cell_flips": grab(log, r"DPL-0383\] Performed (\d+) cell flips", int),
        }
    )


def parse_filler(log: str) -> dict[str, Any]:
    return compact({"fillers_placed": grab(log, r"DPL-0001\] Placed (\d+) filler instances", int),
                    "warnings": messages(log, tool="DPL")})


# ---------------------------------------------------------------------------
# Floorplan (ifp), pins (ppl), taps (tap), power grid (pdn), global placement (gpl)
# ---------------------------------------------------------------------------

_BBOX = r"\(\s*(\S+)\s+(\S+)\s*\)\s*\(\s*(\S+)\s+(\S+)\s*\)"


def _bbox(log: str, pattern: str) -> list[float] | None:
    m = re.findall(pattern + r"\s*" + _BBOX, log)
    return [float(v) for v in m[-1]] if m else None


def parse_ifp(log: str) -> dict[str, Any]:
    rows = re.search(r"IFP-0001\] Added (\d+) rows of (\d+) site (\S+?)\.?$", log, re.MULTILINE)
    return compact(
        {
            "rows": int(rows.group(1)) if rows else None,
            "sites_per_row": int(rows.group(2)) if rows else None,
            "site": rows.group(3) if rows else None,
            "die_um": _bbox(log, r"IFP-0100\] Die BBox:"),
            "core_um": _bbox(log, r"IFP-0101\] Core BBox:"),
            "core_area_um2": grab(log, r"IFP-0102\] Core area:\s+(\S+) um"),
            "instances_area_um2": grab(log, r"IFP-0103\] Total instances area:\s+(\S+) um"),
            "utilization": grab(log, r"IFP-0104\] Effective utilization:\s+(\S+)"),
            "instances": grab(log, r"IFP-0105\] Number of instances:\s+(\d+)", int),
            "removed_buffers": grab(log, r"RSZ-0026\] Removed (\d+) buffers", int),
            "warnings": messages(log),
        }
    )


def parse_ppl(log: str) -> dict[str, Any]:
    return compact(
        {
            "slots": grab(log, r"PPL-0001\] Number of available slots\s+(\d+)", int),
            "io_pins": grab(log, r"PPL-0002\] Number of I/O\s+(\d+)", int),
            "io_with_sink": grab(log, r"PPL-0003\] Number of I/O w/sink\s+(\d+)", int),
            "io_without_sink": grab(log, r"PPL-0004\] Number of I/O w/o sink\s+(\d+)", int),
            "io_hpwl_um": grab(log, r"PPL-0012\] I/O nets HPWL: (\S+?)\s*um"),
            "warnings": messages(log),
        }
    )


def parse_tapcell(log: str) -> dict[str, Any]:
    return compact(
        {
            "endcaps": grab_sum(log, r"TAP-0004\] Inserted (\d+) endcaps"),
            "tapcells": grab_sum(log, r"TAP-0005\] Inserted (\d+) tapcells"),
            "warnings": messages(log),
        }
    )


def parse_pdn(log: str) -> dict[str, Any]:
    # ORD-0046 (-defer_connection deprecated) comes from the platform's PDN script, not the grid.
    return compact(
        {
            "grids": grab_all(log, r"PDN-0001\] Inserting grid: (.+?)\s*$", str),
            "warnings": [w for w in messages(log) if "ORD-0046" not in w],
            "errors": messages(log, level="ERROR"),
        }
    )


_GPL_ROW = re.compile(r"^\s*(\d+)\s*\|\s*([\d.]+)\s*\|\s*([\d.eE+-]+)\s*\|", re.MULTILINE)


def parse_gpl(log: str) -> dict[str, Any]:
    """``global_placement``: last Nesterov row (overflow, HPWL), area and routability/timing results."""
    rows = _GPL_ROW.findall(log)
    last = rows[-1] if rows else None
    td_overflow = grab(log, r"GPL-0101\]\s+Iter: \d+, overflow: (\S+?),")
    return compact(
        {
            "iterations": grab(log, r"GPL-1001\] Global placement finished at iteration (\d+)", int),
            "final_overflow": float(last[1]) if last else td_overflow,
            "final_hpwl_um": float(last[2]) if last else None,
            "instances": grab(log, r"GPL-0006\] Number of instances:\s+(\d+)", int),
            "movable_instances": grab(log, r"GPL-0007\] Movable instances:\s+(\d+)", int),
            "fixed_instances": grab(log, r"GPL-0008\] Fixed instances:\s+(\d+)", int),
            "utilization_pct": grab(log, r"GPL-0019\] Utilization:\s+(\S+) %"),
            "target_density": grab(log, r"GPL-0023\] Placement target density:\s+(\S+)"),
            "placed_cell_area_um2": grab(log, r"GPL-1002\] Placed Cell Area\s+(\S+)"),
            "minimum_feasible_density": grab(log, r"GPL-1004\] Minimum Feasible Density\s+(\S+)"),
            "final_placement_area_um2": grab(log, r"GPL-1014\] Final placement area: (\S+)"),
            "routability_iterations": grab(log, r"GPL-1017\] Routability mode iteration count: (\d+)", int),
            "routability_final_congestion": grab(log, r"GPL-1005\] Routability final weighted congestion: (\S+)"),
            "timing_driven_worst_slack": grab(log, r"GPL-0106\] Timing-driven: worst slack (\S+)"),
            "did_not_converge": "GPL-1010" in log or None,
            "warnings": messages(log),
        }
    )


# ---------------------------------------------------------------------------
# Routing: global route (grt), antennas (ant/grt), detailed route (drt)
# ---------------------------------------------------------------------------

_CONGESTION_ROW = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+([\d.]+)%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$")


def parse_congestion(log: str) -> dict[str, Any]:
    """The last ``GRT-0096 Final congestion report`` table: per layer and the Total row."""
    layers: list[dict[str, Any]] = []
    total: dict[str, Any] | None = None
    for line in log.splitlines():
        if "GRT-0096]" in line:
            layers, total = [], None
            continue
        m = _CONGESTION_ROW.match(line.strip())
        if not m:
            continue
        name, resource, demand, usage, max_h, max_v, overflow = m.groups()
        row = {"resource": int(resource), "demand": int(demand), "usage_pct": float(usage),
               "max_h_overflow": int(max_h), "max_v_overflow": int(max_v), "overflow": int(overflow)}
        if name == "Total":
            total = row
        else:
            layers.append({"layer": name, **row})
    return compact({"layers": [r for r in layers if r["resource"] or r["demand"]], "total": total})


def parse_grt(log: str) -> dict[str, Any]:
    congestion = parse_congestion(log)
    total = congestion.get("total") or {}
    return compact(
        {
            "routed_nets": grab(log, r"GRT-0014\] Routed nets: (\d+)", int),
            "clock_nets": grab(log, r"GRT-0019\] Found (\d+) clock nets", int),
            "wirelength_um": grab(log, r"GRT-0018\] Total wirelength: (\S+) um"),
            "vias": grab(log, r"GRT-0111\] Final number of vias: (\d+)", int),
            "min_layer": grab(log, r"GRT-0020\] Min routing layer: (\S+)", str),
            "max_layer": grab(log, r"GRT-0021\] Max routing layer: (\S+)", str),
            "overflow": total.get("overflow"),
            "usage_pct": total.get("usage_pct"),
            "congestion": congestion.get("layers"),
            "runtime": grab(log, r"GRT-0303\] Global routing runtime = (\S+)", str),
            "warnings": messages(log),
            "errors": messages(log, level="ERROR"),
        }
    )


_WL_ROW = re.compile(r"^(\S+)\s+([\d.]+)um\s+(\d+)%\s*$", re.MULTILINE)


def parse_wire_length_table(log: str) -> dict[str, Any]:
    """``report_wire_length -summary`` (GRT-0278 global / GRT-0279 detailed): per-layer microns and %."""
    layers = [{"layer": n, "um": float(um), "pct": int(pct)} for n, um, pct in _WL_ROW.findall(log)]
    return compact({"layers": layers, "total_um": round(sum(r["um"] for r in layers), 2) if layers else None})


def parse_antennas(log: str) -> dict[str, Any]:
    """``check_antennas`` (ANT-0001/0002) and ``repair_antennas`` (GRT-0006/0012/0015/0302/0009) messages."""
    found = grab_all(log, r"GRT-0012\] Found (\d+) antenna violations", int)
    return compact(
        {
            "net_violations": grab(log, r"ANT-0002\] Found (\d+) net violations", int),
            "pin_violations": grab(log, r"ANT-0001\] Found (\d+) pin violations", int),
            "repair_iterations": grab(log, r"GRT-0006\] Repairing antennas, iteration (\d+)", int),
            "violations_found": found[0] if found else None,
            "violations_left": found[-1] if found else None,
            "diodes_inserted": grab_sum(log, r"GRT-0015\] Inserted (\d+) diodes"),
            "jumpers_inserted": grab_sum(log, r"GRT-0302\] Inserted (\d+) jumpers"),
            "nets_rerouted": grab_sum(log, r"GRT-0009\] rerouting (\d+) nets"),
            "no_diode": "GRT-0246" in log or None,
        }
    )


# "Start 0th optimization iteration." / "Start 60th stubborn tiles iteration." / ...
_DRT_ITER = re.compile(r"DRT-0195\] Start (\d+)\w* ([a-z ]*?)\s*iteration")


def _viol_table(block: str) -> dict[str, dict[str, int]]:
    """``Viol/Layer`` table after a DRT-0199 line: {violation type: {layer: count}}."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("Viol/Layer"):
            continue
        layers = line.split()[1:]
        table: dict[str, dict[str, int]] = {}
        for row in lines[i + 1:]:
            m = re.match(r"^(\D+?)\s+((?:\d+\s*)+)$", row)
            if not m:
                break
            counts = [int(v) for v in m.group(2).split()]
            table[m.group(1).strip()] = {lay: c for lay, c in zip(layers, counts) if c}
        return table
    return {}


def parse_drt(log: str) -> dict[str, Any]:
    """``detailed_route -verbose 1``: one row per optimization iteration, plus the final totals."""
    starts = [(m.start(), int(m.group(1)), m.group(2)) for m in _DRT_ITER.finditer(log)]
    iterations = []
    for idx, (pos, number, kind) in enumerate(starts):
        block = log[pos: starts[idx + 1][0] if idx + 1 < len(starts) else len(log)]
        iterations.append(compact({
            "iteration": number,
            "kind": kind if kind != "optimization" else None,
            "violations": grab(block, r"DRT-0199\]\s+Number of violations = (\d+)", int),
            "wirelength_um": grab(block, r"^Total wire length = (\S+) um", int),
            "vias": grab(block, r"^Total number of vias = (\d+)", int),
            "by_type": _viol_table(block) or None,
        }))
    final_block = log[starts[-1][0]:] if starts else log
    layers = {n: int(v) for n, v in re.findall(r"^Total wire length on LAYER (\S+) = (\d+) um", final_block, re.M)}
    last = iterations[-1] if iterations else {}
    return compact(
        {
            "iterations": iterations,
            "final_violations": last.get("violations"),
            "violations_by_type": last.get("by_type"),
            "wirelength_um": grab(log, r"^Total wire length = (\S+) um", int),
            "vias": grab(log, r"^Total number of vias = (\d+)", int),
            "wirelength_by_layer_um": {k: v for k, v in layers.items() if v},
            "completed": "DRT-0198" in log or None,
            "warnings": messages(log),
        }
    )


_DRC_BBOX = re.compile(r"bbox = \(\s*([-\d.]+),\s*([-\d.]+)\) - \(\s*([-\d.]+),\s*([-\d.]+)\) on Layer (\S+)")


def parse_drc_report(text: str, limit: int = 50) -> dict[str, Any]:
    """A detailed-router DRC report (``-output_drc`` or ``drt::check_drc``)::

        violation type: Short
            srcs: net:a net:b
            bbox = (x1, y1) - (x2, y2) on Layer L
    """
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("violation type:"):
            items.append({"type": line.split(":", 1)[1].strip()})
        elif items and line.startswith("srcs:"):
            items[-1]["sources"] = line.split(":", 1)[1].split()
        elif items and (m := _DRC_BBOX.search(line)):
            items[-1]["bbox_um"] = [float(v) for v in m.groups()[:4]]
            items[-1]["layer"] = m.group(5)
    by_type: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    by_type_layer: dict[str, int] = {}
    nets: dict[str, int] = {}
    for item in items:
        layer = item.get("layer", "?")
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        by_layer[layer] = by_layer.get(layer, 0) + 1
        key = f"{item['type']} @ {layer}"
        by_type_layer[key] = by_type_layer.get(key, 0) + 1
        for src in item.get("sources", []):
            if src.startswith("net:"):
                nets[src[4:]] = nets.get(src[4:], 0) + 1
    top_nets = sorted(nets.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {
        "total": len(items),
        "by_type": by_type,
        "by_layer": by_layer,
        "by_type_layer": by_type_layer,
        "top_nets": [{"net": n, "violations": c} for n, c in top_nets],
        "violations": items[:limit],
        "truncated": len(items) > limit,
    }


# ---------------------------------------------------------------------------
# Signoff: extraction (rcx), timing/power reports (sta), power grid (psm), fill (fin)
# ---------------------------------------------------------------------------


def parse_rcx(log: str) -> dict[str, Any]:
    m = re.findall(r"RCX-0045\] Extract (\d+) nets, (\d+) rsegs, (\d+) caps, (\d+) ccs", log)
    nets, rsegs, caps, ccs = (int(v) for v in m[-1]) if m else (None,) * 4
    return compact(
        {
            "nets": nets,
            "resistor_segments": rsegs,
            "ground_caps": caps,
            "coupling_caps": ccs,
            "rc_segments": grab(log, r"RCX-0040\] Final (\d+) rc segments", int),
            "coupling_threshold_ff": grab(log, r"RCX-0440\] Coupling threshhold is (\S+) fF"),
            "warnings": messages(log),
        }
    )


_DRV_SECTIONS = {"max slew": "max_slew", "max capacitance": "max_cap", "max fanout": "max_fanout"}
_DRV_ROW = re.compile(r"^(\S+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+) \((VIOLATED|MET)\)")


def parse_drv_violators(log: str) -> dict[str, list[dict[str, Any]]]:
    """``report_check_types -max_slew -max_capacitance -max_fanout -violators``: rows per check."""
    out: dict[str, list[dict[str, Any]]] = {}
    section = None
    for line in log.splitlines():
        if line.strip() in _DRV_SECTIONS:
            section = _DRV_SECTIONS[line.strip()]
            continue
        m = _DRV_ROW.match(line.strip())
        if m and section:
            pin, limit, value, slack, _ = m.groups()
            out.setdefault(section, []).append(
                {"pin": pin, "limit": float(limit), "value": float(value), "slack": float(slack)})
    return out


_END_ROW = re.compile(r"^(\S+(?: \([^)]*\))?)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+) \((VIOLATED|MET)\)")


def parse_endpoint_report(log: str) -> dict[str, list[dict[str, Any]]]:
    """``report_checks -format end``: endpoints under ``max_delay/setup`` and ``min_delay/hold`` groups."""
    out: dict[str, list[dict[str, Any]]] = {}
    check = None
    for line in log.splitlines():
        m = re.match(r"^(max_delay/setup|min_delay/hold) group (\S+)", line)
        if m:
            check = "setup" if m.group(1).startswith("max") else "hold"
            continue
        m = _END_ROW.match(line.strip())
        if m and check:
            endpoint, required, arrival, slack, _ = m.groups()
            out.setdefault(check, []).append({"endpoint": endpoint, "required": float(required),
                                              "arrival": float(arrival), "slack": float(slack)})
    return out


_POWER_ROW = re.compile(r"^(Sequential|Combinational|Clock|Macro|Pad|Total)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)%",
                        re.MULTILINE)


def parse_power(log: str) -> dict[str, Any]:
    """``report_power`` table: internal/switching/leakage/total watts per group."""
    groups = {}
    for name, internal, switching, leakage, total_w, pct in _POWER_ROW.findall(log):
        groups[name.lower()] = {"internal_w": float(internal), "switching_w": float(switching),
                                "leakage_w": float(leakage), "total_w": float(total_w), "pct": float(pct)}
    total = groups.pop("total", None)
    return compact({"total_w": total["total_w"] if total else None, "total": total,
                    "groups": {k: v for k, v in groups.items() if v["total_w"]}})


def parse_clock_min_period(log: str) -> dict[str, dict[str, float]]:
    """``report_clock_min_period``: ``core_clock period_min = 0.49 fmax = 2033.83``."""
    return {clk: {"period_min": float(p), "fmax_mhz": float(f)}
            for clk, p, f in re.findall(r"^(\S+) period_min = (\S+) fmax = (\S+)", log, re.MULTILINE)}


_IR_KEYS = {
    "Net": ("net", str),
    "Corner": ("corner", str),
    "Total power": ("total_power_w", float),
    "Supply voltage": ("supply_voltage_v", float),
    "Worstcase voltage": ("worst_voltage_v", float),
    "Average voltage": ("average_voltage_v", float),
    "Average IR drop": ("average_ir_drop_v", float),
    "Worstcase IR drop": ("worst_ir_drop_v", float),
    "Percentage drop": ("drop_pct", float),
    "Maximum current": ("em_max_current_a", float),
    "Average current": ("em_average_current_a", float),
    "Number of resistors": ("em_resistors", int),
}


def parse_ir_report(log: str) -> list[dict[str, Any]]:
    """``analyze_power_grid``: one dict per ``IR report`` block (plus its ``EM analysis`` block)."""
    reports: list[dict[str, Any]] = []
    for line in log.splitlines():
        if line.startswith("########## IR report"):
            reports.append({})
            continue
        m = re.match(r"^([A-Za-z ]+?)\s*:\s*(\S+)", line)
        if m and reports and m.group(1) in _IR_KEYS:
            key, cast = _IR_KEYS[m.group(1)]
            value = m.group(2)
            try:
                reports[-1][key] = cast(value)
            except ValueError:
                reports[-1][key] = value
    return reports


def parse_fill(log: str) -> dict[str, Any]:
    """``density_fill``: FIN-0004 'Total fills' is cumulative, so per-layer counts are differences."""
    layers: dict[str, int] = {}
    current, last_total = None, 0
    for line in log.splitlines():
        m = re.search(r"FIN-0003\] Filling layer (\S+?)\.?$", line)
        if m:
            current = m.group(1)
            continue
        m = re.search(r"FIN-0004\] Total fills: (\d+)", line)
        if m and current:
            total = int(m.group(1))
            layers[current] = total - last_total
            last_total = total
    return compact({"fills_by_layer": layers, "total_fills": last_total if layers else None,
                    "skipped_layers": grab_all(log, r"FIN-0010\] Skipping layer (\S+?)\.?$", str),
                    "errors": messages(log, level="ERROR")})
