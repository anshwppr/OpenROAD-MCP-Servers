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
