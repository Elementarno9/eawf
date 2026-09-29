"""The review gate refuses a rendering claim a source scan alone backs.

A criterion whose question is what a reader actually sees carries the
``rendered_run`` evidence kind. Its gates run like deterministic ones, may
not be a static source scan, and the durable close auditor's row for it must
cite the rendered run's GateReceipt: an artifact path, a grep note or an
implementer's report beside it does not discharge the claim. A receipt row
quoting a rate must name the population (selector and filter) the rate was
drawn from, and the refusal is the review gate's rather than the model's so
a receipt persisted before the rule still loads.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    GATE_RUN_EVIDENCE_KINDS,
    CriterionEvidenceKind,
    CriterionSpec,
    GateSpec,
    QualityDimension,
    validate_criterion_gate_refs,
)
from eawf.kernel.state.models import Wave
from eawf.kernel.store.kinds.agent_report import AgentReportEvidenceRef, StatisticPopulation
from eawf.workflow.dispatch.verdict import (
    DurableAuditContext,
    DurableAuditCriterion,
    build_auditor_prompt,
    parse_auditor_report_body,
)
from eawf.workflow.dispatch.verdict_schema import _evidence_ref_schema
from eawf.workflow.evidence.resolve import ResolveStatus, resolve
from eawf.workflow.verify.compile import compile_gate
from tests._criteria_helpers import legacy_criteria

_WAVE_ID = "P41-I01-W07"
_TEXT = "the timeline draws one bar per phase at every zoom level"
_RECEIPT_URN = f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-render"
_SOURCE_SCAN = {
    "kind": "artifact",
    "ref": "src/eawf/observability/reflect/static/timeline.css",
    "note": "a stylesheet grep finds the hatch declaration unchanged",
}
_POPULATION = {"selector": "transcripts/**/*.jsonl", "filter": "sessions after the cut"}


def _criterion(evidence_kind: str, *, gate_ids: tuple[str, ...] = ("G-01",)) -> CriterionSpec:
    return CriterionSpec(
        id="CR-01",
        text=_TEXT,
        kind="behavior",
        acceptance_style="binary",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        gate_ids=list(gate_ids),
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the emitted page harness counts the bars it drew per lane",
    )


def _gate(kind: str, args: dict[str, object]) -> GateSpec:
    return GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind=kind,
        args=args,
        policy="block",
        cadence="every-wave",
    )


def _context(*, rendered_run: bool = True) -> DurableAuditContext:
    return DurableAuditContext(
        wave_id=_WAVE_ID,
        close_attempt_id="CA-07",
        integration_id="WI-07",
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        spec_digest="c" * 64,
        criteria_digest="d" * 64,
        gate_manifest_digest="e" * 64,
        policy_digest="f" * 64,
        runner_digest="1" * 64,
        dependency_binding_digest="2" * 64,
        criteria=(
            DurableAuditCriterion(
                criterion_id="CR-01",
                text=_TEXT,
                deterministic=True,
                rendered_run=rendered_run,
                gate_receipt_urns=(_RECEIPT_URN,),
            ),
        ),
    )


def _judged_context() -> DurableAuditContext:
    judged = DurableAuditCriterion(criterion_id="CR-01", text=_TEXT, deterministic=False)
    return replace(_context(), criteria=(judged,))


def _body(*evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "auditor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "read the rendered run against the criterion",
        "target_id": _WAVE_ID,
        "criteria": [{"criterion": _TEXT, "passed": True, "evidence_refs": list(evidence)}],
        "refutations": [],
    }


def _receipt(**extra: object) -> dict[str, Any]:
    return {"kind": "store_record", "ref": _RECEIPT_URN, **extra}


# ---------- the vocabulary ----------


def test_lint_037_rendered_run_is_a_criterion_evidence_kind() -> None:
    assert "rendered_run" in get_args(CriterionEvidenceKind)
    assert _criterion("rendered_run").evidence_kind == "rendered_run"


def test_lint_037_only_the_gate_running_kinds_run_gates() -> None:
    assert frozenset({"deterministic", "rendered_run"}) == GATE_RUN_EVIDENCE_KINDS
    assert frozenset(get_args(CriterionEvidenceKind)) > GATE_RUN_EVIDENCE_KINDS


def test_lint_037_an_unknown_evidence_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        _criterion("rendered")


def test_lint_037_a_rendered_run_gate_compiles_like_a_deterministic_one() -> None:
    gate = _gate("command_exit_zero", {"argv": ["uv", "run", "pytest", "-q"]})
    rendered = compile_gate(gate, criterion=_criterion("rendered_run"))
    deterministic = compile_gate(gate, criterion=_criterion("deterministic"))
    assert rendered is not None
    assert rendered == deterministic
    assert compile_gate(gate, criterion=_criterion("jury")) is None


def test_lint_037_resolve_routes_rendered_run_to_the_deterministic_check(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text("<main></main>", encoding="utf-8")
    result = resolve("page.html", "rendered_run", project_root=tmp_path)
    assert result.status is ResolveStatus.RESOLVED
    assert result.evidence_kind == "rendered_run"


# ---------- a static source scan cannot back a rendering claim ----------


def test_lint_037_a_static_source_scan_gate_cannot_back_a_rendered_run_criterion() -> None:
    gate = _gate("regex_in_file", {"path": "timeline.css", "pattern": "hatch"})
    with pytest.raises(ValueError, match="static source scan"):
        validate_criterion_gate_refs([_criterion("rendered_run")], [gate])


def test_lint_037_the_same_static_gate_still_backs_a_deterministic_criterion() -> None:
    gate = _gate("regex_in_file", {"path": "timeline.css", "pattern": "hatch"})
    validate_criterion_gate_refs([_criterion("deterministic")], [gate])


def test_lint_037_a_probe_gate_backs_a_rendered_run_criterion() -> None:
    gate = _gate("command_exit_zero", {"argv": ["uv", "run", "pytest", "-q"]})
    validate_criterion_gate_refs([_criterion("rendered_run")], [gate])


# ---------- the review gate ----------


def test_lint_037_review_gate_refuses_a_source_scan_as_the_only_evidence() -> None:
    with pytest.raises(ValidationError, match="source scan"):
        parse_auditor_report_body(_body(_SOURCE_SCAN), durable_context=_context())


def test_lint_037_review_gate_refuses_a_receipt_the_close_did_not_bind() -> None:
    stray = {"kind": "store_record", "ref": f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-x"}
    with pytest.raises(ValidationError, match="source scan"):
        parse_auditor_report_body(_body(stray, _SOURCE_SCAN), durable_context=_context())


def test_lint_037_review_gate_admits_the_rendered_run_receipt_beside_a_scan() -> None:
    body = parse_auditor_report_body(_body(_SOURCE_SCAN, _receipt()), durable_context=_context())
    assert [ref.ref for ref in body.criteria[0].evidence_refs][-1] == _RECEIPT_URN


def test_lint_037_review_gate_refuses_a_rate_with_no_named_population() -> None:
    quoted = _receipt(note="the defect appears in 12.5% of local transcripts")
    with pytest.raises(ValidationError, match="population"):
        parse_auditor_report_body(_body(quoted), durable_context=_context())


def test_lint_037_review_gate_refuses_an_unnamed_rate_on_a_judged_row() -> None:
    quoted = {"kind": "artifact", "ref": "docs/x.md", "note": "fails 3 % of the time"}
    with pytest.raises(ValidationError, match="population"):
        parse_auditor_report_body(_body(quoted), durable_context=_judged_context())


def test_lint_037_review_gate_admits_a_rate_with_its_population_named() -> None:
    quoted = _receipt(note="the defect appears in 12.5% of them", population=_POPULATION)
    body = parse_auditor_report_body(_body(quoted), durable_context=_context())
    population = body.criteria[0].evidence_refs[0].population
    assert population == StatisticPopulation(**_POPULATION)


@pytest.mark.parametrize("note", [None, "", "the bars render at every zoom level", "100 bars"])
def test_lint_037_a_note_quoting_no_rate_needs_no_population(note: str | None) -> None:
    ref = _receipt() if note is None else _receipt(note=note)
    parse_auditor_report_body(_body(ref), durable_context=_context())


# ---------- the persisted shape ----------


def test_lint_037_a_receipt_persisted_before_the_rule_still_loads() -> None:
    ref = AgentReportEvidenceRef.model_validate(
        {"kind": "store_record", "ref": _RECEIPT_URN, "note": "40% of runs"}
    )
    assert ref.population is None


@pytest.mark.parametrize(
    "population",
    [
        {"selector": "", "filter": "all"},
        {"selector": "a", "filter": ""},
        {"selector": "a"},
        {"selector": "a", "filter": "b", "rate": "40%"},
        {"selector": "a" * 241, "filter": "b"},
    ],
)
def test_lint_037_a_population_names_a_selector_and_a_filter(
    population: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        StatisticPopulation.model_validate(population)


def test_lint_037_the_forced_schema_admits_a_named_population() -> None:
    schema = _evidence_ref_schema()["properties"]["population"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["filter", "selector"]


def test_lint_037_the_durable_prompt_names_the_rendered_run_contract() -> None:
    wave_criteria = [c.model_dump(mode="json") for c in legacy_criteria(_TEXT)]
    wave = Wave.model_validate(
        {
            "id": _WAVE_ID,
            "iter_id": "P41-I01",
            "title": "gate the rendered run",
            "status": "claimed",
            "file_scopes": ["src/eawf/workflow/dispatch/verdict.py"],
            "success_criteria": wave_criteria,
            "agent_role": "executor",
            "effort_bucket": "XS",
            "opened_at": "2026-07-01T00:00:00Z",
        }
    )
    prompt = build_auditor_prompt(wave, diff_base="abc~1", durable_context=_context())
    assert "  - rendered run: true" in prompt
    assert "population" in prompt
    judged = build_auditor_prompt(wave, diff_base="abc~1", durable_context=_judged_context())
    assert "rendered run: true" not in judged
