"""Minimal Liberty reader: pin directions, sequential cells and clock pins per cell."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_CELL_RE = re.compile(r"\bcell\s*\(\s*\"?([\w$]+)\"?\s*\)\s*\{")
_PIN_RE = re.compile(r"\bpin\s*\(\s*\"?([\w$]+)\"?\s*\)\s*\{")
_DIR_RE = re.compile(r"direction\s*:\s*(\w+)")


@dataclass
class LibCell:
    name: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    sequential: bool = False
    clock_pins: list[str] = field(default_factory=list)


def _block(text: str, open_brace: int) -> str:
    """Text of the {...} block whose '{' is at ``open_brace`` (exclusive)."""
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : i]
    raise ValueError("unbalanced braces")


def parse_liberty(path: str | Path) -> dict[str, LibCell]:
    text = re.sub(r"/\*.*?\*/", " ", Path(path).read_text(encoding="utf-8"), flags=re.DOTALL)
    cells: dict[str, LibCell] = {}
    for m in _CELL_RE.finditer(text):
        body = _block(text, m.end() - 1)
        cell = LibCell(m.group(1), sequential=bool(re.search(r"\b(ff|latch)\s*\(", body)))
        for pm in _PIN_RE.finditer(body):
            pin_body = _block(body, pm.end() - 1)
            dm = _DIR_RE.search(pin_body)
            direction = dm.group(1) if dm else "input"
            (cell.outputs if direction == "output" else cell.inputs).append(pm.group(1))
            if re.search(r"\bclock\s*:\s*true", pin_body):
                cell.clock_pins.append(pm.group(1))
        cells[cell.name] = cell
    return cells
