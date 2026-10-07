"""Every spreadsheet prompt maps to exactly one check plan whose names exist (no OpenROAD needed)."""

import functools
from collections import Counter

import pytest

from oracle import BUNDLE, Design
from tasks.checks import KINDS
from tasks.export_tasks import XLSX, export
from tasks.prompt_plans import all_plans, load_tasks

NAME_ARGS = ("node", "src", "dst", "gate", "a", "b", "wire", "clock", "old")


@functools.cache
def design(case: str) -> Design:
    return Design.from_file(BUNDLE / "netlists" / f"{case}.v")


def test_json_fixture_matches_the_workbook():
    assert export(XLSX) == load_tasks()


def test_every_prompt_has_exactly_one_plan():
    plans = all_plans()  # raises on unmatched or ambiguous prompts
    data = load_tasks()
    assert len(plans) == len(data["prompts"]) == 310
    assert {p.kind for p in plans} <= set(KINDS)


def test_category_counts_match_the_summary_sheet():
    data = load_tasks()
    summary = {row["category"]: row["count"] for row in data["summary"]}
    counts = Counter(p["category"] for p in data["prompts"])
    assert counts == summary
    assert sum(counts.values()) == 310


def test_prompt_numbers_increase_within_each_case():
    by_case: dict[str, list[int]] = {}
    for p in all_plans():
        by_case.setdefault(p.case, []).append(p.no)
    for case, numbers in by_case.items():
        assert numbers == sorted(numbers), case
        assert numbers[0] == 1, case


def test_spot_checks():
    plans = {(p.case, p.no): p for p in all_plans()}
    assert plans["test12", 4].args == {"src": "n24[0]", "dst": "n26[0]", "avoid": ["n86984"]}
    assert plans["test25", 7].args == {"what": "wire", "old": "n74", "new": "renamed_wire"}
    assert plans["test36", 6].args == {"value": "0", "cell_type": "AND"}
    assert plans["test31", 13].args == {"node": "renamed_sig"}
    assert plans["test32", 9].kind == "dominance"
    assert plans["test02", 2].args == {"netlist": "testcase/test02/test02.v"}


@pytest.mark.parametrize("case", sorted({p.case for p in all_plans()}))
def test_names_exist_in_the_netlist(case):
    d = design(case)
    renamed: set[str] = set()
    for p in (p for p in all_plans() if p.case == case):
        if p.kind == "rename":
            assert p.args["old"] in d.gates or p.args["old"] in d.nets()
            assert p.args["old"] not in d.nl.port_bits, "renaming a port net"
            renamed.add(p.args["new"])
        for key in NAME_ARGS:
            name = p.args.get(key)
            if name and key != "old" and name not in renamed:
                assert name in d.gates or name in d.nets(), f"{p.label}: {name}"
        for name in p.args.get("avoid", []):
            assert name in d.gates or name in d.nets(), f"{p.label}: {name}"
