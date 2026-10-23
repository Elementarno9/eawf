"""A rendering claim cannot be backed by a source scan alone.

A criterion whose question is what a reader actually sees carries the
``rendered_run`` evidence kind. Its gates run like deterministic ones and
may not be a static source scan. A receipt row quoting a rate names the
population (selector and filter) the rate was drawn from, and a receipt
persisted before that rule still loads.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

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
from eawf.kernel.store.kinds.agent_report import AgentReportEvidenceRef, StatisticPopulation
from eawf.workflow.evidence.resolve import ResolveStatus, resolve
from eawf.workflow.verify.compile import compile_gate

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
