"""Export openroad_bundle/openroad_tasks.xlsx to tests/tasks/openroad_tasks.json.

Standard library only (the .xlsx is a zip of XML parts). Run it after the workbook changes:

    python tests/tasks/export_tasks.py
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
XLSX = HERE.parents[1] / "openroad_bundle" / "openroad_tasks.xlsx"
JSON_PATH = HERE / "openroad_tasks.json"

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _col(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_sheets(path: Path = XLSX) -> dict[str, list[list[str | None]]]:
    """Every sheet as a list of rows (cell text or None)."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("pr:Relationship", NS)}
        sheets: dict[str, list[list[str | None]]] = {}
        for sheet in wb.find("m:sheets", NS):
            target = targets[sheet.get(f"{{{NS['r']}}}id")].lstrip("/")
            part = target if target.startswith("xl/") else "xl/" + target
            root = ET.fromstring(z.read(part))
            rows: list[list[str | None]] = []
            for row in root.iter(f"{{{NS['m']}}}row"):
                cells: dict[int, str | None] = {}
                for c in row.findall("m:c", NS):
                    kind, v = c.get("t"), c.find("m:v", NS)
                    if kind == "s" and v is not None:
                        value = shared[int(v.text)]
                    elif kind == "inlineStr":
                        value = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
                    else:
                        value = v.text if v is not None else None
                    cells[_col(c.get("r"))] = value
                width = max(cells) + 1 if cells else 0
                rows.append([cells.get(i) for i in range(width)])
            sheets[sheet.get("name")] = rows
        return sheets


def _number(value: str | None):
    if value is None:
        return None
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except ValueError:
        return value


def export(path: Path = XLSX) -> dict:
    sheets = read_sheets(path)
    prompts = []
    for row in sheets["Prompts"][1:]:
        row = (row + [None] * 5)[:5]
        if not any(row):
            continue
        case, no, prompt, kind, category = row
        prompts.append({"case": case, "no": _number(no), "prompt": prompt, "type": kind, "category": category})
    summary = []
    for row in sheets["Summary"][1:]:
        row = (row + [None] * 7)[:7]
        if not any(row):
            continue
        kind, category, count, commands, confidence, verified, notes = row
        summary.append(
            {
                "type": kind,
                "category": category,
                "count": _number(count),
                "commands": commands,
                "confidence": _number(confidence),
                "verified": verified,
                "notes": notes,
            }
        )
    setup = {row[0]: row[1] for row in sheets["Setup"][1:] if row and row[0]}
    return {"source": path.name, "setup": setup, "summary": summary, "prompts": prompts}


def main() -> int:
    data = export()
    JSON_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {JSON_PATH} ({len(data['prompts'])} prompts, {len(data['summary'])} categories)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
