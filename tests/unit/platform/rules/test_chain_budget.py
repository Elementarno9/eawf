"""The loaded instruction chain is held to the prompt-budget ceiling, not only the host cap.

SURF-001 and SURF-071: zone-1 size is measured per render, reported against
the ceiling resolved from the economics configuration, and a render over it
fails. SURF-003: a direct host session loads exactly one projection; a managed
Run reads none. SURF-004: the whole loaded chain is measured, imports
included. SURF-006: the measurement reads the renderer's output rather than
restating what a projection contains. SURF-023 and SURF-106: skills are
charged to their declared class, never zone 1, and count against the budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.economics.prompt_budget import (
    DEFAULT_PROMPT_BUDGET,
    BudgetClassId,
    ClassStatus,
    ClassVerdict,
)
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.platform.rules import chain_budget
from eawf.platform.rules.carriers import builtin_carrier_roles, carrier_target
from eawf.platform.rules.chain_budget import (
    ChainBudget,
    ChainBudgetReport,
    ContextContributor,
    ContributorMeasure,
    judge_loaded_chains,
)
from eawf.platform.rules.host_facts import load_host_facts
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    RuleProjectionBudgetError,
    plan_rule_projections,
    render_rule_projections,
)
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalog

_PROJECTIONS = {"card": CARD_TARGET, "policy": POLICY_TARGET}
_SKILL = ".claude/skills/why/SKILL.md"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(yaml.safe_dump({"schema_version": 1, "rules": []}), encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndescription = "A demo project"\n', encoding="utf-8"
    )
    return root


def _configure(root: Path, class_id: BudgetClassId, **ceiling: Any) -> None:
    """Write a prompt budget whose *class_id* allocation carries *ceiling*."""
    policy = DEFAULT_PROMPT_BUDGET.model_dump(mode="json")
    for row in policy["allocations"]:
        if row["class_id"] == class_id.value:
            row.update(ceiling)
    config = {"schema_version": "1.0", "economics": {"prompt_budget": policy}}
    (root / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _report(root: Path) -> ChainBudgetReport:
    report = plan_rule_projections(root).manifest.prompt_budget
    assert report is not None
    return report


def _chain(report: ChainBudgetReport, runtime: str) -> ChainBudget:
    return next(chain for chain in report.chains if chain.runtime == runtime)


def _verdict(chain: ChainBudget, class_id: BudgetClassId) -> ClassVerdict:
    return next(row for row in chain.outcome.verdicts if row.class_id is class_id)


def _rows(chain: ChainBudget, contributor: ContextContributor) -> list[ContributorMeasure]:
    return [row for row in chain.contributors if row.contributor is contributor]


def _judge(outputs: dict[str, str], installed: dict[str, str] | None = None) -> ChainBudgetReport:
    return judge_loaded_chains(
        DEFAULT_PROMPT_BUDGET,
        load_host_facts(),
        projections=_PROJECTIONS,
        outputs=outputs,
        installed_skills=installed or {},
    )


def _largest_zone1(report: ChainBudgetReport) -> int:
    zone1 = BudgetClassId.STEERING_ZONE1
    sizes = [_verdict(chain, zone1).measured_bytes for chain in report.chains]
    return max(size for size in sizes if size is not None)


# ---- SURF-001 / SURF-071: measured per render, enforced against the ceiling ----


def test_surf_001_zone1_is_reported_against_the_resolved_ceiling(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    report = plan.manifest.prompt_budget
    assert report is not None
    codex = _verdict(_chain(report, "codex"), BudgetClassId.STEERING_ZONE1)
    policy_bytes = next(p.record.byte_count for p in plan.projections if p.record.kind == "policy")
    assert codex.measured_bytes == policy_bytes
    assert (
        codex.ceiling_bytes
        == DEFAULT_PROMPT_BUDGET.allocation(BudgetClassId.STEERING_ZONE1).max_bytes
    )
    assert codex.status is not ClassStatus.EXHAUSTED


def test_surf_001_a_render_over_the_ceiling_fails_and_writes_nothing(repo: Path) -> None:
    _configure(repo, BudgetClassId.STEERING_ZONE1, max_bytes=1_024)
    with pytest.raises(RuleProjectionBudgetError, match="steering_zone1") as caught:
        render_rule_projections(repo)
    assert "1024-byte ceiling" in str(caught.value)
    assert not (repo / POLICY_TARGET).exists()
    assert not (repo / CARD_TARGET).exists()


def test_surf_071_a_zone1_render_exactly_at_the_ceiling_passes(repo: Path) -> None:
    largest = _largest_zone1(_report(repo))
    _configure(repo, BudgetClassId.STEERING_ZONE1, max_bytes=largest)
    assert _largest_zone1(_report(repo)) == largest


def test_surf_071_a_zone1_render_one_byte_over_the_ceiling_fails(repo: Path) -> None:
    largest = _largest_zone1(_report(repo))
    _configure(repo, BudgetClassId.STEERING_ZONE1, max_bytes=largest - 1)
    with pytest.raises(RuleProjectionBudgetError, match=f"{largest} bytes"):
        plan_rule_projections(repo)


def test_surf_071_the_ceiling_binds_below_the_host_cap(repo: Path) -> None:
    host_cap = next(p.record.cap_bytes for p in plan_rule_projections(repo).projections)
    largest = _largest_zone1(_report(repo))
    assert largest < host_cap
    _configure(repo, BudgetClassId.STEERING_ZONE1, max_bytes=largest // 2)
    with pytest.raises(RuleProjectionBudgetError, match="prompt-budget policy"):
        plan_rule_projections(repo)


def test_surf_071_a_dropping_policy_still_fails_the_render(repo: Path) -> None:
    policy = DEFAULT_PROMPT_BUDGET.model_dump(mode="json")
    policy["on_exhaustion"] = "degrade_by_priority"
    for row in policy["allocations"]:
        if row["class_id"] == "steering_zone1":
            row["max_bytes"] = 1_024
    config = {"schema_version": "1.0", "economics": {"prompt_budget": policy}}
    (repo / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(RuleProjectionBudgetError, match="steering_zone1"):
        plan_rule_projections(repo)


def test_surf_071_an_invalid_economics_configuration_fails_the_render(repo: Path) -> None:
    config = {"schema_version": "1.0", "economics": {"prompt_budget": {"policy_id": "x"}}}
    (repo / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(RuleProjectionBudgetError, match="economics configuration is invalid"):
        plan_rule_projections(repo)


# ---- SURF-003: one projection per direct session, none for a managed Run ----


def test_surf_003_each_direct_session_loads_exactly_one_projection(repo: Path) -> None:
    report = _report(repo)
    loaded = {
        chain.runtime: {
            source
            for row in chain.contributors
            if row.class_id is BudgetClassId.STEERING_ZONE1
            for source in row.sources
        }
        & {CARD_TARGET, POLICY_TARGET}
        for chain in report.chains
    }
    assert loaded == {
        "claude": {POLICY_TARGET},
        "codex": {POLICY_TARGET},
        "opencode": {CARD_TARGET},
    }


def test_surf_003_a_managed_run_spec_carries_no_rendered_projection() -> None:
    fields = set(CompiledRunSpec.model_fields)
    assert not {name for name in fields if "projection" in name or "rule" in name}


# ---- SURF-004: the actual loaded chain, imports included ----


def test_surf_004_an_imported_projection_counts_against_the_ceiling(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    assert plan.manifest.prompt_budget is not None
    claude = _chain(plan.manifest.prompt_budget, "claude")
    outputs = dict(plan.outputs)
    (imported,) = _rows(claude, ContextContributor.IMPORTED)
    assert imported.sources == (POLICY_TARGET,)
    expected = len(outputs["CLAUDE.md"].encode()) + len(outputs[POLICY_TARGET].encode())
    assert _verdict(claude, BudgetClassId.STEERING_ZONE1).measured_bytes == expected


def _lines(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def test_surf_004_nested_imports_are_followed_once() -> None:
    outputs = {
        "CLAUDE.md": "@AGENTS.override.md\n",
        POLICY_TARGET: _lines("policy", "@docs/extra.md", "@CLAUDE.md"),
        "docs/extra.md": _lines("extra", "@AGENTS.override.md"),
        CARD_TARGET: "card\n",
    }
    (imported,) = _rows(_chain(_judge(outputs), "claude"), ContextContributor.IMPORTED)
    assert imported.sources == (POLICY_TARGET, "docs/extra.md")
    assert imported.byte_count == len(outputs[POLICY_TARGET]) + len(outputs["docs/extra.md"])


def test_surf_004_a_chain_with_no_imports_measures_zero_imported_bytes() -> None:
    outputs = {"CLAUDE.md": "@AGENTS.override.md\n", POLICY_TARGET: "p\n", CARD_TARGET: "c\n"}
    (imported,) = _rows(_chain(_judge(outputs), "codex"), ContextContributor.IMPORTED)
    assert imported.sources == ()
    assert imported.byte_count == 0


# ---- SURF-006: the measurement reads the renderer's output ----


def test_surf_006_zone1_follows_whatever_the_renderer_emits() -> None:
    for size in (0, 1, 4_096):
        outputs = {"CLAUDE.md": "", POLICY_TARGET: "x" * size, CARD_TARGET: "card"}
        codex = _chain(_judge(outputs), "codex")
        assert _verdict(codex, BudgetClassId.STEERING_ZONE1).measured_bytes == size


def test_surf_006_zone1_equals_the_rendered_projection_rows(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    assert plan.manifest.prompt_budget is not None
    rows = {p.record.target: p.record.byte_count for p in plan.projections}
    for chain in plan.manifest.prompt_budget.chains:
        (root,) = _rows(chain, ContextContributor.ROOT)
        if root.sources[0] in rows:
            assert root.byte_count == rows[root.sources[0]]


# ---- SURF-023 / SURF-106: skills load into a declared class, never zone 1 ----


def test_surf_023_role_carriers_are_charged_to_zone2(repo: Path) -> None:
    claude = _chain(_report(repo), "claude")
    (skills,) = _rows(claude, ContextContributor.SKILL)
    assert skills.class_id is BudgetClassId.STEERING_ZONE2
    assert set(skills.sources) == {carrier_target(role) for role in builtin_carrier_roles()}
    zone1 = [row for row in claude.contributors if row.class_id is BudgetClassId.STEERING_ZONE1]
    assert not [source for row in zone1 for source in row.sources if source.endswith("SKILL.md")]


def test_surf_023_an_installed_skill_counts_against_the_budget(repo: Path) -> None:
    skill = repo / _SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: why\n---\n" + "y" * 2_000, encoding="utf-8")
    (skills,) = _rows(_chain(_report(repo), "claude"), ContextContributor.SKILL)
    assert _SKILL in skills.sources
    _configure(repo, BudgetClassId.STEERING_ZONE2, max_bytes=1_024)
    with pytest.raises(RuleProjectionBudgetError, match="steering_zone2"):
        plan_rule_projections(repo)


def test_surf_023_a_stale_stamped_skill_on_disk_is_not_charged(repo: Path) -> None:
    render_rule_projections(repo)
    stale = repo / ".claude/skills/eawf-rules-retired/SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_bytes((repo / carrier_target(builtin_carrier_roles()[0])).read_bytes())
    (skills,) = _rows(_chain(_report(repo), "claude"), ContextContributor.SKILL)
    assert stale.relative_to(repo).as_posix() not in skills.sources


def test_surf_023_a_runtime_that_discovers_no_skills_charges_none() -> None:
    outputs = {"CLAUDE.md": "", POLICY_TARGET: "p", CARD_TARGET: "c", _SKILL: "s" * 50}
    (skills,) = _rows(_chain(_judge(outputs), "codex"), ContextContributor.SKILL)
    assert skills.sources == ()
    assert skills.byte_count == 0


def test_surf_106_every_catalog_skill_declares_a_class_outside_zone1() -> None:
    assert SKILL_CATALOG.entries
    for entry in SKILL_CATALOG.entries:
        assert entry.budget_class is not BudgetClassId.STEERING_ZONE1


def test_surf_106_a_skill_declaring_zone1_is_refused() -> None:
    body = SKILL_CATALOG.entries[0].model_dump()
    body["budget_class"] = BudgetClassId.STEERING_ZONE1
    with pytest.raises(ValidationError, match="always-on zone 1"):
        type(SKILL_CATALOG.entries[0]).model_validate(body)


def test_surf_106_a_skill_that_declares_no_class_is_refused() -> None:
    body = SKILL_CATALOG.entries[0].model_dump()
    del body["budget_class"]
    with pytest.raises(ValidationError, match="budget_class"):
        type(SKILL_CATALOG.entries[0]).model_validate(body)


def test_surf_106_an_installed_skill_is_charged_to_its_declared_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    why = SKILL_CATALOG.entry("why")
    assert why is not None
    declared = why.model_copy(update={"budget_class": BudgetClassId.TOOL_CATALOG})
    monkeypatch.setattr(chain_budget, "SKILL_CATALOG", SkillCatalog(entries=(declared,)))
    outputs = {"CLAUDE.md": "", POLICY_TARGET: "p", CARD_TARGET: "c"}
    claude = _chain(_judge(outputs, {_SKILL: "w" * 30}), "claude")
    (skills,) = _rows(claude, ContextContributor.SKILL)
    assert skills.class_id is BudgetClassId.TOOL_CATALOG
    assert skills.byte_count == 30


# ---- the report's own shape ----


def test_a_contributor_row_needs_a_measurement_or_its_reason() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ContributorMeasure(
            contributor=ContextContributor.ROOT,
            class_id=BudgetClassId.STEERING_ZONE1,
            byte_count=None,
        )
    with pytest.raises(ValidationError, match="exactly one"):
        ContributorMeasure(
            contributor=ContextContributor.ROOT,
            class_id=BudgetClassId.STEERING_ZONE1,
            byte_count=1,
            unmeasured_reason="both",
        )


def test_a_report_with_no_chain_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        ChainBudgetReport(chains=())
