"""Tool results and host advertising are declared context contributors.

SURF-151: the chain measurement names every contributor class a host session
is charged in, measured or not, and a report missing a declared class fails
its own completeness check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.economics.prompt_budget import (
    DEFAULT_PROMPT_BUDGET,
    BudgetClassId,
    evaluate_prompt_budget,
)
from eawf.platform.rules.chain_budget import ChainBudget, ContextContributor
from eawf.platform.rules.render import plan_rule_projections


@pytest.fixture
def chains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ChainBudget, ...]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(yaml.safe_dump({"schema_version": 1, "rules": []}), encoding="utf-8")
    report = plan_rule_projections(root).manifest.prompt_budget
    assert report is not None
    return report.chains


def test_surf_151_every_chain_names_every_contributor_class(
    chains: tuple[ChainBudget, ...],
) -> None:
    assert {chain.runtime for chain in chains} == {"claude", "codex", "opencode"}
    for chain in chains:
        assert {row.contributor for row in chain.contributors} == set(ContextContributor)


@pytest.mark.parametrize(
    ("contributor", "class_id"),
    [
        (ContextContributor.TOOL_RESULT, BudgetClassId.TOOL_CATALOG),
        (ContextContributor.HOST_ADVERTISING, BudgetClassId.STEERING_ZONE1),
        (ContextContributor.GLOBAL, BudgetClassId.STEERING_ZONE1),
    ],
)
def test_surf_151_an_unauthored_contributor_is_charged_and_explained(
    chains: tuple[ChainBudget, ...], contributor: ContextContributor, class_id: BudgetClassId
) -> None:
    for chain in chains:
        (row,) = [item for item in chain.contributors if item.contributor is contributor]
        assert row.class_id is class_id
        assert row.byte_count is None
        assert row.unmeasured_reason


def test_surf_151_a_report_missing_a_declared_class_fails_its_completeness_check(
    chains: tuple[ChainBudget, ...],
) -> None:
    chain = chains[0]
    body = chain.model_dump()
    body["contributors"] = [
        row for row in body["contributors"] if row["contributor"] != ContextContributor.TOOL_RESULT
    ]
    with pytest.raises(ValidationError, match="names no tool_result"):
        ChainBudget.model_validate(body)


def test_surf_151_a_report_naming_no_contributor_names_every_class_missing() -> None:
    with pytest.raises(ValidationError, match="global, host_advertising, imported, root"):
        ChainBudget(
            runtime="codex",
            contributors=(),
            outcome=evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, ()),
        )
