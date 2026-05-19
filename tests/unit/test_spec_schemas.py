"""Unit tests for the C03 spec-infrastructure Pydantic models.

Covers boundary cases (empty list / single / multi) and error paths
(extra key, wrong type, schema_version mismatch, missing required
fields, id-nesting violations) for PhaseSpec, IterSpec, WaveSpec and
their shared building blocks under :mod:`eawf.spec.common`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.spec.common import (
    EvidenceRef,
    VerdictCitation,
)
from eawf.spec.iter import IterAuditCadence, IterSpec, IterWaveGroup
from eawf.spec.phase import (
    PhaseEUEnvelope,
    PhaseKPI,
    PhaseShipCriterion,
    PhaseSpec,
)
from eawf.spec.wave import WaveBehavior, WaveMockup, WaveSpec
from eawf.state.enums import AgentSessionRole, EffortBucket

# ---- Common building blocks -------------------------------------------------


def _verdict_citation() -> VerdictCitation:
    return VerdictCitation(
        verdict_id="V12",
        brief=".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
    )


def test_common_imports_clean() -> None:
    # Re-import smoke test: the four public symbols load without circular
    # import (W01 success criterion #1).
    from eawf.spec.common import (  # noqa: F401
        EvidenceRef,
        VerdictCitation,
    )


def test_verdict_citation_minimal() -> None:
    cit = VerdictCitation(
        verdict_id="V12",
        brief=".ea/local/research/2026-05-19-x.md",
    )
    assert cit.verdict_id == "V12"
    assert cit.line is None
    assert cit.note is None


def test_verdict_citation_full() -> None:
    cit = VerdictCitation(
        verdict_id="H03-12",
        brief=".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
        line=42,
        note="references the audit-DSL kind table",
    )
    assert cit.verdict_id == "H03-12"
    assert cit.line == 42


@pytest.mark.parametrize(
    "verdict_id",
    ["V12", "V12-RC3", "D17", "R5", "H03-12", "D17-CORE"],
)
def test_verdict_citation_accepts_valid_verdict_ids(verdict_id: str) -> None:
    cit = VerdictCitation(
        verdict_id=verdict_id,
        brief=".ea/local/research/2026-05-19-x.md",
    )
    assert cit.verdict_id == verdict_id


@pytest.mark.parametrize(
    "bad_verdict_id",
    ["v12", "X12", "V", "12V", "V12-rc3", "", "V-12"],
)
def test_verdict_citation_rejects_invalid_verdict_ids(bad_verdict_id: str) -> None:
    with pytest.raises(ValidationError):
        VerdictCitation(
            verdict_id=bad_verdict_id,
            brief=".ea/local/research/2026-05-19-x.md",
        )


@pytest.mark.parametrize(
    "bad_brief",
    [
        "research/2026-05-19-x.md",  # missing .ea/
        ".ea/research/2026-05-19-x.md",  # missing local|artifacts
        ".ea/local/research/2026-05-19-x.txt",  # wrong suffix
        ".ea/artifacts/2026-05-19-x.md",  # missing /research/
        "",
    ],
)
def test_verdict_citation_rejects_invalid_brief_paths(bad_brief: str) -> None:
    with pytest.raises(ValidationError):
        VerdictCitation(verdict_id="V12", brief=bad_brief)


def test_verdict_citation_rejects_line_zero() -> None:
    with pytest.raises(ValidationError):
        VerdictCitation(
            verdict_id="V12",
            brief=".ea/local/research/2026-05-19-x.md",
            line=0,
        )


def test_verdict_citation_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        VerdictCitation.model_validate(
            {
                "verdict_id": "V12",
                "brief": ".ea/local/research/2026-05-19-x.md",
                "extra_field": "nope",
            }
        )


def test_evidence_ref_minimal() -> None:
    ref = EvidenceRef(
        kind="audit",
        ref="urn:eawf:v1:audit:AU-01",
        summary="audit cleared the hypothesis",
    )
    assert ref.kind == "audit"


@pytest.mark.parametrize(
    "kind",
    ["audit", "artifact", "store_record", "external_url"],
)
def test_evidence_ref_accepts_each_kind(kind: str) -> None:
    ref = EvidenceRef(kind=kind, ref="x", summary="summary text here")  # type: ignore[arg-type]
    assert ref.kind == kind


def test_evidence_ref_rejects_invalid_kind() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate({"kind": "bogus", "ref": "x", "summary": "y"})


def test_evidence_ref_rejects_empty_summary() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(kind="audit", ref="x", summary="")


def test_evidence_ref_rejects_oversized_summary() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(kind="audit", ref="x", summary="x" * 401)


def test_evidence_ref_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(
            {
                "kind": "audit",
                "ref": "x",
                "summary": "summary text here",
                "extra": "nope",
            }
        )


@pytest.mark.parametrize(
    "good_path",
    [
        "tests/unit/test_x.py",
        "tests/integration/test_y.py",
        "tests/golden/snapshot.svg",
        "tests/fixtures/data.json",
    ],
)
def test_test_ref_accepts_repo_relative_test_paths(good_path: str) -> None:
    # TestRef is an Annotated[str, ...] — exercise it through a model
    # that uses it (WaveBehavior.test_refs).
    behavior = WaveBehavior(
        id="B1",
        text="observable behaviour described in twenty characters or more",
        test_refs=[good_path],
    )
    assert behavior.test_refs == [good_path]


@pytest.mark.parametrize(
    "bad_path",
    [
        "src/eawf/foo.py",  # not under tests/
        "test_x.py",  # missing tests/ prefix
        "",  # empty
    ],
)
def test_test_ref_rejects_non_test_paths(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        WaveBehavior(
            id="B1",
            text="observable behaviour described in twenty characters or more",
            test_refs=[bad_path],
        )


@pytest.mark.parametrize(
    "good_path",
    [
        "src/eawf/spec/wave.py",
        "tools/scripts/x.py",
        ".ea/artifacts/research/x.md",
        "docs/architecture/x.md",
        "build/foo.txt",
        "tests/unit/test_x.py",
    ],
)
def test_file_scope_ref_accepts_known_roots(good_path: str) -> None:
    # Exercise FileScopeRef through PhaseSpec.related_file_scopes.
    spec = _phase_spec_factory(related_file_scopes=[good_path])
    assert spec.related_file_scopes == [good_path]


@pytest.mark.parametrize(
    "bad_path",
    [
        "node_modules/x.js",
        "/absolute/path.py",
        "scripts/foo.py",  # not under one of the known roots
        "",
    ],
)
def test_file_scope_ref_rejects_unknown_roots(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(related_file_scopes=[bad_path])


# ---- PhaseSpec --------------------------------------------------------------


def _phase_spec_factory(**overrides: Any) -> PhaseSpec:
    defaults: dict[str, Any] = {
        "id": "P25",
        "title": "Spec infrastructure + 4-way parallel cluster waves",
        "outcome": "Deliver typed phase/iter/wave specs and parallel clusters",
        "failure_modes": ["specs drift from state tree"],
        "ship_criteria": [PhaseShipCriterion(id="SC1", text="all waves closed")],
    }
    defaults.update(overrides)
    return PhaseSpec.model_validate(defaults)


def test_phase_spec_minimal() -> None:
    spec = _phase_spec_factory()
    assert spec.schema_version == "1.0"
    assert spec.kind == "PhaseSpec"
    assert spec.id == "P25"
    assert spec.kpis == []
    assert spec.depends_on == []


def test_phase_spec_full() -> None:
    spec = _phase_spec_factory(
        kpis=[
            PhaseKPI(
                metric="agent_eu_total",
                target=100.0,
                direction="min",
                threshold_kind="soft",
            )
        ],
        success_modes=["four clusters land in parallel"],
        depends_on=["P24"],
        eu_envelope=PhaseEUEnvelope(
            expected_eu_total=90.0,
            pessimistic_eu_total=120.0,
            confidence="medium",
        ),
        iter_ids=["P25-I01"],
        profile_constraints=["engineering"],
        implements=[_verdict_citation()],
        consumed_by=["P26"],
        related_file_scopes=["src/eawf/spec/common.py"],
    )
    assert spec.eu_envelope is not None
    assert spec.eu_envelope.expected_eu_total == pytest.approx(90.0)
    assert spec.iter_ids == ["P25-I01"]


def test_phase_spec_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        PhaseSpec.model_validate(
            {
                "id": "P25",
                "title": "x",
                "outcome": "y" * 21,
                "failure_modes": ["m"],
                "ship_criteria": [{"id": "SC1", "text": "t"}],
                "bogus_field": "nope",
            }
        )


def test_phase_spec_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(schema_version="1.1")


def test_phase_spec_rejects_wrong_kind() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(kind="WaveSpec")


def test_phase_spec_rejects_missing_failure_modes() -> None:
    with pytest.raises(ValidationError):
        PhaseSpec.model_validate(
            {
                "id": "P25",
                "title": "x",
                "outcome": "y" * 21,
                "ship_criteria": [{"id": "SC1", "text": "t"}],
            }
        )


def test_phase_spec_rejects_empty_failure_modes() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(failure_modes=[])


def test_phase_spec_rejects_missing_ship_criteria() -> None:
    with pytest.raises(ValidationError):
        PhaseSpec.model_validate(
            {
                "id": "P25",
                "title": "x",
                "outcome": "y" * 21,
                "failure_modes": ["m"],
            }
        )


def test_phase_spec_rejects_empty_ship_criteria() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(ship_criteria=[])


def test_phase_spec_rejects_short_outcome() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(outcome="too short")


def test_phase_spec_rejects_wrong_id_pattern() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(id="not-a-phase-id")


def test_phase_spec_rejects_wrong_type_for_kpis() -> None:
    with pytest.raises(ValidationError):
        _phase_spec_factory(kpis="not a list")


def test_phase_kpi_rejects_bad_direction() -> None:
    with pytest.raises(ValidationError):
        PhaseKPI.model_validate(
            {
                "metric": "x",
                "target": 1.0,
                "direction": "bogus",
                "threshold_kind": "hard",
            }
        )


def test_phase_eu_envelope_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        PhaseEUEnvelope(expected_eu_total=-1.0)


# ---- IterSpec ---------------------------------------------------------------


def _iter_spec_factory(**overrides: Any) -> IterSpec:
    defaults: dict[str, Any] = {
        "id": "P25-I01",
        "phase_id": "P25",
        "title": "Cluster waves — 4-way parallel",
        "sub_goal": "land C03 + C07a + C07b + C08 deliverables together",
        "ordering_rationale": "C03/C07b/C08 dispatch from t=0; C07a after C07b W06",
    }
    defaults.update(overrides)
    return IterSpec.model_validate(defaults)


def test_iter_spec_minimal() -> None:
    spec = _iter_spec_factory()
    assert spec.schema_version == "1.0"
    assert spec.kind == "IterSpec"
    assert spec.wave_groups == []
    assert spec.wave_ids == []


def test_iter_spec_full() -> None:
    spec = _iter_spec_factory(
        wave_groups=[
            IterWaveGroup(
                label="C03",
                wave_ids=["P25-I01-W01", "P25-I01-W02"],
                rationale="schemas + audit-DSL kind sequenced behind W01",
            )
        ],
        audit_cadence=IterAuditCadence(
            on_iter_close=["spec-grammar"],
            on_phase_close=["verify-implements"],
        ),
        profile_constraints=["engineering"],
        implements=[_verdict_citation()],
        wave_ids=["P25-I01-W01", "P25-I01-W02"],
    )
    assert len(spec.wave_groups) == 1
    assert spec.audit_cadence.on_iter_close == ["spec-grammar"]


def test_iter_spec_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        IterSpec.model_validate(
            {
                "id": "P25-I01",
                "phase_id": "P25",
                "title": "x",
                "sub_goal": "y" * 20,
                "ordering_rationale": "z" * 20,
                "extra": "nope",
            }
        )


def test_iter_spec_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        _iter_spec_factory(schema_version="2.0")


def test_iter_spec_rejects_short_sub_goal() -> None:
    with pytest.raises(ValidationError):
        _iter_spec_factory(sub_goal="too short")


def test_iter_spec_rejects_short_ordering_rationale() -> None:
    with pytest.raises(ValidationError):
        _iter_spec_factory(ordering_rationale="because")


def test_iter_spec_rejects_wrong_id_pattern() -> None:
    with pytest.raises(ValidationError):
        _iter_spec_factory(id="not-an-iter")


def test_iter_wave_group_rejects_empty_wave_ids() -> None:
    with pytest.raises(ValidationError):
        IterWaveGroup(
            label="x",
            wave_ids=[],
            rationale="rationale text twenty chars min",
        )


def test_iter_wave_group_rejects_short_rationale() -> None:
    with pytest.raises(ValidationError):
        IterWaveGroup(label="x", wave_ids=["P25-I01-W01"], rationale="short")


# ---- WaveSpec ---------------------------------------------------------------


def _wave_spec_factory(**overrides: Any) -> WaveSpec:
    defaults: dict[str, Any] = {
        "id": "P25-I01-W01",
        "iter_id": "P25-I01",
        "phase_id": "P25",
        "title": "C03 schemas + common types",
        "agent_role": AgentSessionRole.EXECUTOR,
        "effort_bucket": EffortBucket.L,
        "file_scopes": ["src/eawf/spec/common.py"],
        "implements": [_verdict_citation()],
        "behaviors": [
            WaveBehavior(
                id="B1",
                text="phase/iter/wave Pydantic schemas import without cycles",
            )
        ],
        "failure_modes": ["circular import between spec modules"],
    }
    defaults.update(overrides)
    return WaveSpec.model_validate(defaults)


def test_wave_spec_minimal() -> None:
    spec = _wave_spec_factory()
    assert spec.schema_version == "1.0"
    assert spec.kind == "WaveSpec"
    assert spec.tests == []
    assert spec.mockup is None


def test_wave_spec_full() -> None:
    spec = _wave_spec_factory(
        deps=[],
        file_scopes=[
            "src/eawf/spec/common.py",
            "src/eawf/spec/phase.py",
            "src/eawf/spec/iter.py",
            "src/eawf/spec/wave.py",
        ],
        behaviors=[
            WaveBehavior(
                id="B1",
                text="phase/iter/wave Pydantic schemas import without cycles",
                latency_budget_ms=50,
                test_refs=["tests/unit/test_spec_schemas.py"],
            ),
            WaveBehavior(
                id="B2",
                text="extra keys raise pydantic.ValidationError on model_validate",
            ),
        ],
        tests=["tests/unit/test_spec_schemas.py"],
        mockup=WaveMockup(ascii="(no UI)", note="non-UI scope"),
    )
    assert len(spec.behaviors) == 2
    assert spec.behaviors[0].latency_budget_ms == 50


def test_wave_spec_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        WaveSpec.model_validate(
            {
                "id": "P25-I01-W01",
                "iter_id": "P25-I01",
                "phase_id": "P25",
                "title": "x",
                "agent_role": "executor",
                "effort_bucket": "L",
                "file_scopes": ["src/eawf/spec/common.py"],
                "implements": [
                    {
                        "verdict_id": "V12",
                        "brief": ".ea/local/research/2026-05-19-x.md",
                    }
                ],
                "behaviors": [
                    {
                        "id": "B1",
                        "text": "x" * 21,
                    }
                ],
                "failure_modes": ["m"],
                "extra": "nope",
            }
        )


def test_wave_spec_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(schema_version="1.1")


def test_wave_spec_rejects_empty_file_scopes() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(file_scopes=[])


def test_wave_spec_rejects_empty_implements() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(implements=[])


def test_wave_spec_rejects_empty_behaviors() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(behaviors=[])


def test_wave_spec_rejects_empty_failure_modes() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(failure_modes=[])


def test_wave_spec_rejects_inconsistent_iter_phase_nesting() -> None:
    # iter id "P25-I01" does not nest under phase "P26"
    with pytest.raises(ValidationError) as excinfo:
        _wave_spec_factory(phase_id="P26")
    assert "iter id does not nest under phase" in str(excinfo.value)


def test_wave_spec_rejects_inconsistent_wave_iter_nesting() -> None:
    # wave id "P25-I01-W01" does not nest under iter "P25-I02"
    with pytest.raises(ValidationError) as excinfo:
        _wave_spec_factory(iter_id="P25-I02")
    assert "wave id does not nest under iter" in str(excinfo.value)


def test_wave_spec_rejects_wrong_agent_role() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(agent_role="bogus-role")


def test_wave_spec_rejects_wrong_effort_bucket() -> None:
    with pytest.raises(ValidationError):
        _wave_spec_factory(effort_bucket="XXL")


def test_wave_behavior_rejects_bad_id() -> None:
    with pytest.raises(ValidationError):
        WaveBehavior(id="bogus", text="x" * 21)


def test_wave_behavior_rejects_short_text() -> None:
    with pytest.raises(ValidationError):
        WaveBehavior(id="B1", text="short")


def test_wave_behavior_rejects_negative_latency() -> None:
    with pytest.raises(ValidationError):
        WaveBehavior(id="B1", text="x" * 21, latency_budget_ms=-1)


def test_wave_mockup_rejects_empty_ascii() -> None:
    with pytest.raises(ValidationError):
        WaveMockup(ascii="")


# ---- Cross-spec schema_version lock -----------------------------------------


@pytest.mark.parametrize(
    "model_cls, factory",
    [
        (PhaseSpec, _phase_spec_factory),
        (IterSpec, _iter_spec_factory),
        (WaveSpec, _wave_spec_factory),
    ],
)
def test_schema_version_defaults_to_1_0(model_cls: type, factory: Any) -> None:
    spec = factory()
    assert spec.schema_version == "1.0"


@pytest.mark.parametrize(
    "bad_version",
    ["1.1", "2", "2.0", "0.9", "", "1", "1.0.0"],
)
@pytest.mark.parametrize(
    "factory",
    [_phase_spec_factory, _iter_spec_factory, _wave_spec_factory],
)
def test_schema_version_rejects_non_1_0(bad_version: str, factory: Any) -> None:
    with pytest.raises(ValidationError):
        factory(schema_version=bad_version)


# ---- Boundary: list cardinalities (empty / single / multi) ------------------


def test_phase_spec_accepts_single_failure_mode() -> None:
    spec = _phase_spec_factory(failure_modes=["one"])
    assert len(spec.failure_modes) == 1


def test_phase_spec_accepts_multi_failure_modes() -> None:
    spec = _phase_spec_factory(failure_modes=["one", "two", "three"])
    assert len(spec.failure_modes) == 3


def test_phase_spec_default_kpis_empty() -> None:
    spec = _phase_spec_factory()
    assert spec.kpis == []


def test_wave_spec_accepts_single_behavior() -> None:
    spec = _wave_spec_factory()
    assert len(spec.behaviors) == 1


def test_wave_spec_accepts_multi_file_scopes() -> None:
    spec = _wave_spec_factory(
        file_scopes=[
            "src/eawf/spec/common.py",
            "src/eawf/spec/phase.py",
            "src/eawf/spec/iter.py",
        ]
    )
    assert len(spec.file_scopes) == 3


# ---- Public re-exports from package root ------------------------------------


def test_package_reexports() -> None:
    from eawf import spec

    expected = {
        "BriefPathStr",
        "EvidenceRef",
        "FileScopeRef",
        "IterAuditCadence",
        "IterSpec",
        "IterWaveGroup",
        "PhaseEUEnvelope",
        "PhaseKPI",
        "PhaseShipCriterion",
        "PhaseSpec",
        "TestRef",
        "VerdictCitation",
        "VerdictIdStr",
        "WaveBehavior",
        "WaveMockup",
        "WaveSpec",
    }
    assert expected.issubset(set(spec.__all__))
    # Ensure each name actually resolves on the module.
    for name in expected:
        assert hasattr(spec, name)
