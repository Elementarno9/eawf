"""Contract: every shipped skill page is rendered through the six-slot prompt chassis.

Each page carries Authority, Context, Task, Method, Applicable rules (slot
4b), Constraints and Output in that order. The packagers ship what
:func:`~eawf.workflow.skills.catalog.shipped_skill_specs` renders, and the
committed plugin goldens must hold the same shape: a golden page carrying the
retired ``integration_request_unnamed`` text or an option outside the skill's
grammar reds here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.platform.rules.carriers import applied_builtin_records
from eawf.platform.rules.records import RuleRecord, RuleScope
from eawf.surfaces.render.skills.render import SkillSpec, render_skill_md_from_spec
from eawf.workflow.skills.bodies.chassis import (
    CHASSIS_SLOTS,
    SkillPageError,
    applicable_rules,
    assemble_skill_page,
    chassis_findings,
    check_skill_page,
    shipped_skill_page,
)
from eawf.workflow.skills.bodies.prompts import SKILL_PROMPTS, SkillPrompt, skill_prompt
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry, shipped_skill_specs

_GOLDEN = Path(__file__).resolve().parents[3] / "golden" / "plugin_install"
_CATALOG_IDS = tuple(entry.skill_id for entry in SKILL_CATALOG.entries)
_HEADING = re.compile(r"^## (.+)$", re.MULTILINE)


def _entry(skill_id: str) -> SkillCatalogEntry:
    entry = SKILL_CATALOG.entry(skill_id)
    assert entry is not None
    return entry


def _golden_pages() -> list[tuple[str, Path]]:
    claude = [
        (p.parent.name, p) for p in sorted((_GOLDEN / "claude" / "skills").glob("*/SKILL.md"))
    ]
    opencode = [
        (p.name.removesuffix(".md.golden"), p)
        for p in sorted((_GOLDEN / "opencode" / "commands").glob("*.md.golden"))
    ]
    return claude + opencode


def _slot(page: str, heading: str) -> str:
    start = page.index(f"## {heading}\n")
    end = page.find("\n## ", start + 1)
    return page[start : end if end != -1 else len(page)]


def _record(
    *, force: str = "must", activities: tuple[str, ...] = (), roles: tuple[str, ...] = ()
) -> RuleRecord:
    base = next(r for r in applied_builtin_records() if r.force == "must")
    return base.model_copy(
        update={"force": force, "scope": RuleScope(activities=activities, roles=roles)}
    )


@pytest.mark.parametrize("spec", shipped_skill_specs(), ids=lambda spec: spec.skill_name)
def test_shipped_skill_specs_render_every_slot_in_order(spec: SkillSpec) -> None:
    page = render_skill_md_from_spec(spec)
    entry = _entry(spec.skill_name)
    assert tuple(_HEADING.findall(page)) == CHASSIS_SLOTS
    assert chassis_findings(page, entry) == ()


def test_golden_pages_cover_the_catalog_for_both_page_packagers() -> None:
    names = [name for name, _ in _golden_pages()]
    assert sorted(names) == sorted(_CATALOG_IDS * 2)


@pytest.mark.parametrize(("skill_id", "path"), _golden_pages(), ids=lambda v: str(v)[-40:])
def test_golden_page_carries_the_chassis_and_current_grammar(skill_id: str, path: Path) -> None:
    assert chassis_findings(path.read_text(encoding="utf-8"), _entry(skill_id)) == ()


def test_chassis_findings_reds_on_the_stale_integration_code() -> None:
    entry = _entry("integrate")
    page = shipped_skill_page(entry).replace(
        "## 5. Constraints\n\n", "## 5. Constraints\n\n- Stop with `integration_request_unnamed`.\n"
    )
    assert chassis_findings(page, entry) == (
        "stale grammar 'integration_request_unnamed' is on the page",
    )
    with pytest.raises(SkillPageError, match="integration_request_unnamed"):
        check_skill_page(page, entry)


def test_chassis_findings_reds_on_a_retired_option_and_a_retired_skill() -> None:
    entry = _entry("research")
    page = shipped_skill_page(entry).replace(
        "## 4. Method\n\n", "## 4. Method\n\n0. Pass `--final` and hand off to /prep.\n"
    )
    findings = chassis_findings(page, entry)
    assert "option --final is not in the /research grammar" in findings
    assert "retired skill /prep is named; use /plan or /dispatch" in findings
    with pytest.raises(SkillPageError, match="--final"):
        check_skill_page(page, entry)


def test_chassis_findings_exempts_rule_text_but_not_stale_tokens_in_slot_4b() -> None:
    entry = _entry("verify")
    page = shipped_skill_page(entry)
    with_option = page.replace(
        "## 5. Constraints",
        "- must: **Hooks.** Never commit with --no-verify.\n\n## 5. Constraints",
    )
    assert chassis_findings(with_option, entry) == ()
    with_token = page.replace(
        "## 5. Constraints", "integration_request_unnamed\n\n## 5. Constraints"
    )
    assert len(chassis_findings(with_token, entry)) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda page: page.replace("## 4b. Applicable rules\n", ""),
        lambda page: (
            page.replace("## 5. Constraints", "## 5x")
            .replace("## 6. Output", "## 5. Constraints")
            .replace("## 5x", "## 6. Output")
        ),
        lambda page: page + "\n## 7. Extra\n",
        lambda page: "",
    ],
    ids=["missing-4b", "swapped", "extra-slot", "empty"],
)
def test_chassis_findings_reds_on_a_broken_slot_order(mutate: Callable[[str], str]) -> None:
    entry = _entry("plan")
    page = mutate(shipped_skill_page(entry))
    findings = chassis_findings(page, entry)
    assert findings
    assert findings[0].startswith("slots ")


def test_applicable_rules_selects_by_activity_or_role_and_drops_the_rest() -> None:
    prompt = skill_prompt("verify")
    by_activity = _record(activities=("review",))
    by_role = _record(roles=("auditor",))
    unscoped = _record()
    elsewhere = _record(activities=("release",), roles=("planner",))
    information = _record(force="information", activities=("review",))
    selected = applicable_rules(prompt, (unscoped, by_activity, elsewhere, information, by_role))
    assert selected == (by_activity, by_role)


def test_applicable_rules_orders_must_before_should() -> None:
    prompt = skill_prompt("plan")
    should = _record(force="should", activities=("plan",))
    must = _record(activities=("plan",))
    assert applicable_rules(prompt, (should, must)) == (must, should)


def test_applicable_rules_is_empty_for_no_records_or_no_selectors() -> None:
    assert applicable_rules(skill_prompt("plan"), ()) == ()
    assert applicable_rules(skill_prompt("why"), applied_builtin_records()) == ()


def test_shipped_page_composes_slot_4b_from_the_builtin_rule_graph() -> None:
    page = shipped_skill_page(_entry("plan"))
    slot = _slot(page, "4b. Applicable rules")
    expected = applicable_rules(skill_prompt("plan"), applied_builtin_records())
    assert expected, "the builtin graph scopes rules to planning"
    for rule in expected:
        assert f"- {rule.force}: **{rule.title}.** {rule.instruction}" in slot


def test_assemble_skill_page_renders_one_rule_or_the_no_rule_sentence() -> None:
    entry = _entry("why")
    prompt = skill_prompt("why")
    empty = _slot(assemble_skill_page(entry, prompt, ()), "4b. Applicable rules")
    assert "No rule in the effective rule graph is scoped to this skill" in empty
    one = _record(activities=("review",))
    single = _slot(assemble_skill_page(entry, prompt, (one,)), "4b. Applicable rules")
    assert single.count("\n- ") == 1


def test_assemble_skill_page_refuses_a_foreign_prompt() -> None:
    with pytest.raises(ValueError, match="cannot render catalog skill"):
        assemble_skill_page(_entry("plan"), skill_prompt("why"), ())


def test_authority_and_output_slots_render_from_the_catalog_entry() -> None:
    for entry in SKILL_CATALOG.entries:
        page = shipped_skill_page(entry)
        authority = _slot(page, "1. Authority")
        for rpc in entry.effects.rpcs:
            assert f"`{rpc}`" in authority
        for action in entry.operator_only_actions:
            assert f"`{action}`" in authority
        output = _slot(page, "6. Output")
        assert f"`{entry.output.schema_name}`" in output
        for outcome in entry.output.terminal_outcomes:
            assert f"`{outcome}`" in output
        assert f"```text\n{entry.grammar.usage}\n```" in _slot(page, "3. Task")


def test_plan_method_starts_at_step_zero() -> None:
    method = _slot(shipped_skill_page(_entry("plan")), "4. Method")
    assert "\n\n0. Triage uncertainty" in method
    assert "\n1. Read the Milestone contract" in method


def test_skill_prompts_cover_exactly_the_catalog() -> None:
    assert tuple(SKILL_PROMPTS) == _CATALOG_IDS


def test_skill_prompt_refuses_an_unknown_or_non_string_id() -> None:
    with pytest.raises(KeyError, match="no prompt is declared"):
        skill_prompt("prep")
    with pytest.raises(TypeError, match="must be str"):
        skill_prompt(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override",
    [
        {"method": ()},
        {"method": ("  ",)},
        {"constraints": ("same", "same")},
        {"method_start": 2},
        {"unknown": "field"},
        {"skill_id": "Bad Id"},
    ],
    ids=["empty-method", "blank-step", "duplicate-constraint", "start-two", "extra-key", "bad-id"],
)
def test_skill_prompt_model_rejects_a_malformed_record(override: dict[str, object]) -> None:
    fields = skill_prompt("why").model_dump() | override
    with pytest.raises(ValidationError):
        SkillPrompt.model_validate(fields)
