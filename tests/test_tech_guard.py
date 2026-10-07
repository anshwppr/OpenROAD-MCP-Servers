"""Guard: stage-server code must not hard-code any technology (cells, layers, PDK names).

Technology data lives only in platform presets (openroad_common/platforms/*.json) and in
OpenROAD .vars / user JSON files, so a new PDK never needs a code change.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
STAGE_PACKAGES = ["mcp_opt", "mcp_place", "mcp_route", "mcp_signoff"]
GUARDED = [SRC / "openroad_common" / "stage.py", SRC / "openroad_common" / "platform.py"] + [
    path for pkg in STAGE_PACKAGES for path in sorted((SRC / pkg).glob("*.py"))
]

TECH_PATTERNS = [
    r"\bmetal\d+\b", r"\bmet\d\b", r"\bM\d\b", r"nangate", r"freepdk", r"sky130", r"asap7", r"gf180", r"sg13g2",
    r"\bFILLCELL", r"\bTAPCELL", r"\bBUF_X\d", r"\bCLKBUF", r"\bLOGIC[01]_X", r"\bANTENNA_X", r"\bINV_X\d",
]


@pytest.mark.parametrize("path", GUARDED, ids=lambda p: str(p.relative_to(SRC)))
def test_no_technology_in_code(path):
    text = path.read_text(encoding="utf-8")
    # Lower-case patterns are PDK names (any case); upper-case ones are exact cell names.
    hits = [p for p in TECH_PATTERNS if re.search(p, text, re.IGNORECASE if p.islower() else 0)]
    assert not hits, f"{path.name} contains technology-specific text: {hits}"
