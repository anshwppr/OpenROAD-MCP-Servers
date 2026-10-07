"""Acceptance tests: replay every prompt of openroad_tasks.xlsx through the mcp-netlist server.

Each testcase runs its prompts in order through an in-process MCP client (so renames carry
over to later "now" questions). Every answer is checked against the pure-Python oracle, and
the spreadsheet's own OpenROAD recipe is run through run_tcl and checked against the oracle
too. A per-case JSON report goes to tests/.artifacts/tasks/.

Environment:
  NETLIST_TASKS_TIER=full      also run the large netlists (default: netlists under 250 KB)
  NETLIST_TASKS_CASES=a,b      run only these cases (any size)
  NETLIST_TASKS_RECIPES=0      skip the xlsx recipes
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import Client

from conftest import requires_openroad
from oracle import BUNDLE
from tasks.checks import KNOWN_RECIPE_DEVIATIONS, Ctx, Outcome, run_plan
from tasks.prompt_plans import plans_by_case

PLANS = plans_by_case()
FAST_LIMIT = 250_000
ARTIFACTS = Path(__file__).resolve().parent / ".artifacts" / "tasks"


def case_params():
    only = [c for c in os.environ.get("NETLIST_TASKS_CASES", "").split(",") if c]
    full = os.environ.get("NETLIST_TASKS_TIER", "fast") == "full"
    params = []
    for case in sorted(PLANS):
        if only and case not in only:
            continue
        large = (BUNDLE / "netlists" / f"{case}.v").stat().st_size > FAST_LIMIT
        marks = [pytest.mark.openroad] + ([pytest.mark.slow] if large else [])
        if large and not (full or only):
            marks.append(pytest.mark.skip(reason="large netlist (set NETLIST_TASKS_TIER=full)"))
        params.append(pytest.param(case, marks=marks, id=case))
    return params


async def run_case(srv, case: str, tmp: Path, recipes: bool) -> list[Outcome]:
    outcomes = []
    async with Client(srv.mcp, read_timeout_seconds=7200) as client:
        ctx = Ctx(client, case, tmp, recipes)
        for plan in PLANS[case]:
            outcomes.append(await run_plan(ctx, plan))
            if plan.kind == "load" and outcomes[-1].error:
                break
    return outcomes


def describe(o: Outcome) -> str:
    lines = [f"{o.plan.label}: {o.plan.prompt}"]
    if o.error:
        lines.append(f"    error : {o.error}")
    else:
        lines.append(f"    tool  : {o.tool!r:.400}")
        lines.append(f"    oracle: {o.oracle!r:.400}")
        if o.recipe_ok is False:
            lines.append(f"    recipe: {o.recipe!r:.400}")
    return "\n".join(lines)


@requires_openroad
@pytest.mark.parametrize("case", case_params())
def test_prompts(case, tmp_path, netlist_server):
    recipes = os.environ.get("NETLIST_TASKS_RECIPES", "1") != "0"
    outcomes = asyncio.run(run_case(netlist_server, case, tmp_path, recipes))

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"{case}.json").write_text(
        json.dumps([o.as_dict() for o in outcomes], indent=1, default=str), encoding="utf-8"
    )

    assert len(outcomes) == len(PLANS[case]), "the testcase stopped early (load failed)"
    tool_failures = [o for o in outcomes if not o.tool_ok]
    recipe_failures = [
        o for o in outcomes if o.recipe_ok is False and (o.plan.case, o.plan.no) not in KNOWN_RECIPE_DEVIATIONS
    ]
    report = []
    if tool_failures:
        report.append("MCP tool answers that disagree with the oracle:")
        report += [describe(o) for o in tool_failures]
    if recipe_failures:
        report.append("xlsx recipes that disagree with the oracle:")
        report += [describe(o) for o in recipe_failures]
    assert not report, "\n".join(report)
