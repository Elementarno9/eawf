"""Tests for the shared spec primitives in ``eawf.kernel.spec.common``.

Covers ``EvidenceRef`` and the ``EvidenceKind`` Literal added in
P28-I01-W01, the agent-report reconciliation invariant, and the
P28-I01-W03 :class:`CriterionSpec` / :class:`GateSpec` strict models
plus the ``CriterionEvidenceKind`` Literal (deterministic / jury /
attested) the readiness compute (W06) and compile-gate (W08) will
consume.
"""

from __future__ import annotations

from typing import Literal, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionEvidenceKind,
    CriterionSpec,
    EvidenceKind,
    EvidenceRef,
    GateSpec,
)
from eawf.kernel.store.kinds.agent_report import AgentReportEvidenceRef


def _ref(kind: str) -> EvidenceRef:
    """Return an EvidenceRef with the given ``kind`` and minimal valid fields."""
    return EvidenceRef.model_validate({"kind": kind, "ref": "x", "summary": "summary text here"})


# Happy path -------------------------------------------------------------


def test_evidence_ref_accepts_decision_kind() -> None:
    """The W01 deliverable: ``kind="decision"`` validates without error."""
    ref = EvidenceRef(
        kind="decision",
        ref="urn:eawf:v1:decision:OWNER/D17",
        summary="ratified per the v0.4 finalization roundtable",
    )
    assert ref.kind == "decision"


# Boundary: each of the 5 allowed kinds round-trips ---------------------


@pytest.mark.parametrize(
    "kind",
    ["audit", "artifact", "decision", "store_record", "external_url"],
)
def test_evidence_ref_round_trip_each_kind(kind: str) -> None:
    """Every member of the spec vocabulary round-trips through JSON."""
    ref = _ref(kind)
    assert ref.kind == kind
    payload = ref.model_dump_json()
    reloaded = EvidenceRef.model_validate_json(payload)
    assert reloaded.kind == kind
    assert reloaded.ref == "x"
    assert reloaded.summary == "summary text here"


def test_evidence_kind_literal_has_five_members() -> None:
    """The exported Literal carries exactly the five canonical kinds."""
    assert set(get_args(EvidenceKind)) == {
        "audit",
        "artifact",
        "decision",
        "store_record",
        "external_url",
    }


# Error: invalid kind ---------------------------------------------------


def test_evidence_ref_rejects_garbage_kind() -> None:
    """An unknown kind raises ValidationError and the message names the bad value."""
    with pytest.raises(ValidationError) as exc_info:
        EvidenceRef.model_validate({"kind": "garbage", "ref": "x", "summary": "y" * 20})
    message = str(exc_info.value)
    assert "garbage" in message
    assert "kind" in message


# Reconciliation invariant ----------------------------------------------


def test_agent_report_kind_vocabulary_equals_spec_vocabulary() -> None:
    """AgentReportEvidenceRef.kind is a strict subset (equal) of EvidenceKind.

    Per W01: the spec layer wins; the agent-report model is a strict
    subset (same kind set, no extras). We assert SET equality so a
    future kind added on one side without the other fails this test
    immediately.
    """
    spec_kinds = set(get_args(EvidenceKind))
    agent_report_kinds = set(get_args(AgentReportEvidenceRef.model_fields["kind"].annotation))
    assert agent_report_kinds == spec_kinds
    assert not agent_report_kinds - spec_kinds, "agent-report has extras"


@pytest.mark.parametrize(
    "kind",
    ["audit", "artifact", "decision", "store_record", "external_url"],
)
def test_agent_report_ref_accepts_each_spec_kind(kind: str) -> None:
    """Every spec kind validates through AgentReportEvidenceRef as well."""
    ref = AgentReportEvidenceRef(kind=kind, ref="x")  # type: ignore[arg-type]
    assert ref.kind == kind


def test_agent_report_ref_rejects_garbage_kind() -> None:
    """Invalid kinds are rejected at the agent-report boundary too."""
    with pytest.raises(ValidationError) as exc_info:
        AgentReportEvidenceRef.model_validate({"kind": "garbage", "ref": "x"})
    assert "garbage" in str(exc_info.value)


def test_evidence_kind_is_a_literal_type() -> None:
    """EvidenceKind is a typing.Literal (not an Enum or bare str)."""
    # Literal[...] origin is typing.Literal; get_args returns the value tuple.
    args = get_args(EvidenceKind)
    assert len(args) == 5
    # Cross-check: a Literal of the same shape is structurally equivalent.
    expected = Literal["audit", "artifact", "decision", "store_record", "external_url"]
    assert set(get_args(expected)) == set(args)


# CriterionEvidenceKind Literal -----------------------------------------


def test_criterion_evidence_kind_has_three_members() -> None:
    """The verification-flavor vocabulary has exactly three values."""
    assert set(get_args(CriterionEvidenceKind)) == {
        "deterministic",
        "jury",
        "attested",
    }


def test_criterion_evidence_kind_distinct_from_evidence_kind() -> None:
    """The verification flavor is a different vocabulary than the reference kind."""
    assert set(get_args(CriterionEvidenceKind)).isdisjoint(set(get_args(EvidenceKind)))


# CriterionSpec — happy path --------------------------------------------


def _criterion(**overrides: object) -> CriterionSpec:
    """Return a CriterionSpec with the given overrides on minimal-valid defaults."""
    payload: dict[str, object] = {
        "id": "C1",
        "text": "the ship CI must exit zero on the phase PR",
        "kind": "ci_exit_zero",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G1"],
    }
    payload.update(overrides)
    return CriterionSpec.model_validate(payload)


def test_criterion_spec_happy_path_round_trip() -> None:
    """Minimal valid CriterionSpec serialises and deserialises through JSON."""
    crit = _criterion()
    payload = crit.model_dump_json()
    reloaded = CriterionSpec.model_validate_json(payload)
    assert reloaded == crit
    assert reloaded.required is True
    assert reloaded.waiver_reason is None


# CriterionSpec — boundary cases ----------------------------------------


@pytest.mark.parametrize("style", ["binary", "graded"])
def test_criterion_spec_each_acceptance_style(style: str) -> None:
    """Every member of CriterionAcceptanceStyle round-trips."""
    crit = _criterion(acceptance_style=style)
    assert crit.acceptance_style == style


@pytest.mark.parametrize("flavor", ["deterministic", "jury", "attested"])
def test_criterion_spec_each_evidence_kind(flavor: str) -> None:
    """Every CriterionEvidenceKind value validates on CriterionSpec."""
    crit = _criterion(evidence_kind=flavor)
    assert crit.evidence_kind == flavor


def test_criterion_spec_empty_gate_ids_allowed() -> None:
    """A criterion with no gates is valid (gate_ids defaults to empty list)."""
    crit = _criterion(gate_ids=[])
    assert crit.gate_ids == []


def test_criterion_spec_single_gate_id() -> None:
    """One gate id is valid (boundary: single-element list)."""
    crit = _criterion(gate_ids=["G42"])
    assert crit.gate_ids == ["G42"]


def test_criterion_spec_max_length_text_accepted() -> None:
    """Text at exactly 500 chars (the max) validates."""
    crit = _criterion(text="x" * 500)
    assert len(crit.text) == 500


def test_criterion_spec_waiver_reason_set() -> None:
    """A waiver_reason string is accepted (set only via W11 waiver flow)."""
    crit = _criterion(waiver_reason="superseded by C2 in P28-I01-W11")
    assert crit.waiver_reason == "superseded by C2 in P28-I01-W11"


# CriterionSpec — error cases -------------------------------------------


def test_criterion_spec_rejects_unknown_field() -> None:
    """extra='forbid' rejects keys that are not declared on the model (rule 2)."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionSpec.model_validate(
            {
                "id": "C1",
                "text": "x" * 20,
                "kind": "k",
                "acceptance_style": "binary",
                "evidence_kind": "deterministic",
                "gate_ids": [],
                "bogus_field": True,
            }
        )
    assert "bogus_field" in str(exc_info.value)


def test_criterion_spec_rejects_invalid_acceptance_style() -> None:
    """An out-of-vocabulary acceptance_style is rejected and the value appears."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionSpec.model_validate(
            {
                "id": "C1",
                "text": "x" * 20,
                "kind": "k",
                "acceptance_style": "weighted",
                "evidence_kind": "deterministic",
                "gate_ids": [],
            }
        )
    message = str(exc_info.value)
    assert "weighted" in message
    assert "acceptance_style" in message


def test_criterion_spec_rejects_invalid_evidence_kind() -> None:
    """An out-of-vocabulary evidence_kind is rejected and the value appears."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionSpec.model_validate(
            {
                "id": "C1",
                "text": "x" * 20,
                "kind": "k",
                "acceptance_style": "binary",
                "evidence_kind": "hand_wave",
                "gate_ids": [],
            }
        )
    message = str(exc_info.value)
    assert "hand_wave" in message
    assert "evidence_kind" in message


def test_criterion_spec_rejects_text_over_max_length() -> None:
    """Text longer than 500 chars is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionSpec.model_validate(
            {
                "id": "C1",
                "text": "x" * 501,
                "kind": "k",
                "acceptance_style": "binary",
                "evidence_kind": "deterministic",
                "gate_ids": [],
            }
        )
    assert "text" in str(exc_info.value)


def test_criterion_spec_rejects_empty_text() -> None:
    """Empty text fails the min_length=1 bound."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionSpec.model_validate(
            {
                "id": "C1",
                "text": "",
                "kind": "k",
                "acceptance_style": "binary",
                "evidence_kind": "deterministic",
                "gate_ids": [],
            }
        )
    assert "text" in str(exc_info.value)


def test_criterion_spec_rejects_missing_required_field() -> None:
    """A missing required field raises ValidationError naming the field."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionSpec.model_validate(
            {
                "id": "C1",
                "text": "x" * 20,
                "kind": "k",
                # missing acceptance_style
                "evidence_kind": "deterministic",
                "gate_ids": [],
            }
        )
    assert "acceptance_style" in str(exc_info.value)


# GateSpec — happy path -------------------------------------------------


def _gate(**overrides: object) -> GateSpec:
    """Return a GateSpec with the given overrides on minimal-valid defaults.

    The default ``kind`` is ``command_exit_zero`` so ``args`` carries a
    valid ``argv`` vector (W09 model-validator requires one when the
    kind is argv-bearing). Tests that need a non-argv kind override
    ``kind`` (and may clear ``args``) explicitly.
    """
    payload: dict[str, object] = {
        "id": "G1",
        "criterion_id": "C1",
        "kind": "command_exit_zero",
        "args": {"argv": ["uv", "run", "pytest", "-q"]},
        "policy": "block",
        "cadence": "every-wave",
    }
    payload.update(overrides)
    return GateSpec.model_validate(payload)


def test_gate_spec_happy_path_round_trip() -> None:
    """Minimal valid GateSpec serialises and deserialises through JSON."""
    gate = _gate()
    payload = gate.model_dump_json()
    reloaded = GateSpec.model_validate_json(payload)
    assert reloaded == gate
    assert reloaded.required is True
    assert reloaded.timeout_s is None


# GateSpec — boundary cases ---------------------------------------------


@pytest.mark.parametrize("policy", ["block", "warn", "advisory"])
def test_gate_spec_each_policy(policy: str) -> None:
    """Every member of GatePolicy round-trips."""
    gate = _gate(policy=policy)
    assert gate.policy == policy


@pytest.mark.parametrize(
    "cadence",
    ["every-wave", "every-iter", "every-phase", "ship", "manual"],
)
def test_gate_spec_each_cadence(cadence: str) -> None:
    """Every member of GateCadence round-trips."""
    gate = _gate(cadence=cadence)
    assert gate.cadence == cadence


def test_gate_spec_empty_args_allowed_on_non_argv_kind() -> None:
    """A non-argv-bearing kind tolerates an empty ``args`` dict.

    The W09 model-validator requires ``args['argv']`` only when the
    kind is in
    :data:`eawf.kernel.spec.promotion.ARGV_BEARING_GATE_KINDS`. A
    schema-validate gate (or any other non-argv kind) is free to
    default to ``args={}``.
    """
    gate = _gate(kind="schema_validate", args={})
    assert gate.args == {}


def test_gate_spec_timeout_zero_allowed() -> None:
    """timeout_s=0 is the boundary value the ge=0 bound permits."""
    gate = _gate(timeout_s=0)
    assert gate.timeout_s == 0


def test_gate_spec_timeout_set_explicit() -> None:
    """An explicit timeout_s overrides the inherit-default None."""
    gate = _gate(timeout_s=120)
    assert gate.timeout_s == 120


# GateSpec — error cases ------------------------------------------------


def test_gate_spec_rejects_unknown_field() -> None:
    """extra='forbid' rejects keys that are not declared on the model (rule 2)."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {},
                "policy": "block",
                "cadence": "every-wave",
                "extra_knob": 42,
            }
        )
    assert "extra_knob" in str(exc_info.value)


def test_gate_spec_rejects_invalid_policy() -> None:
    """An out-of-vocabulary policy is rejected and the value appears."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {},
                "policy": "soft-block",
                "cadence": "every-wave",
            }
        )
    message = str(exc_info.value)
    assert "soft-block" in message
    assert "policy" in message


def test_gate_spec_rejects_invalid_cadence() -> None:
    """An out-of-vocabulary cadence is rejected and the value appears."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {},
                "policy": "block",
                "cadence": "nightly",
            }
        )
    message = str(exc_info.value)
    assert "nightly" in message
    assert "cadence" in message


def test_gate_spec_rejects_negative_timeout() -> None:
    """timeout_s < 0 fails the ge=0 bound."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {},
                "policy": "block",
                "cadence": "every-wave",
                "timeout_s": -1,
            }
        )
    assert "timeout_s" in str(exc_info.value)


def test_gate_spec_rejects_missing_required_field() -> None:
    """A missing required field raises ValidationError naming the field."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {},
                # missing policy
                "cadence": "every-wave",
            }
        )
    assert "policy" in str(exc_info.value)


# Wave.success_criteria stays untouched ---------------------------------


def test_wave_success_criteria_field_remains_list_of_str() -> None:
    """The state-model Wave.success_criteria field stays list[str] for v0.4.0.

    The W03 deliverable is the TYPED shape (CriterionSpec / GateSpec) that
    downstream waves operate on; the migration of the state-model field is
    out of scope until a later release. This test pins the contract so a
    drive-by edit that re-types the field fails fast.
    """
    from eawf.kernel.state.models import Wave

    annotation = Wave.model_fields["success_criteria"].annotation
    assert annotation == list[str]


# W09 — GateSpec model-validator catches argv at construction time -------


def test_gate_spec_command_exit_zero_accepts_clean_argv() -> None:
    """``command_exit_zero`` with an L0-clean argv constructs without error.

    The W09 model-validator routes ``args['argv']`` through the L0
    argv-policy when the kind is in
    :data:`eawf.kernel.spec.promotion.ARGV_BEARING_GATE_KINDS`. A clean
    ``uv run pytest -q`` (allowlisted wrapper + tool) passes both the
    field-level checks and the model-level argv check.
    """
    gate = _gate(args={"argv": ["uv", "run", "pytest", "-q"]})
    assert gate.args["argv"] == ["uv", "run", "pytest", "-q"]


def test_gate_spec_command_exit_zero_rejects_shell_deny_argv() -> None:
    """``args['argv']=["sh", "-c", "evil"]`` raises at construction time.

    Defense-in-depth: any constructor of a GateSpec (model_validate
    from a body parser, hand-build in code) trips the
    ``@model_validator`` before the row reaches the spec layer.
    """
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {"argv": ["sh", "-c", "rm -rf /"]},
                "policy": "block",
                "cadence": "every-wave",
            }
        )
    message = str(exc_info.value)
    assert "G1" in message
    assert "rejected by L0 policy" in message


def test_gate_spec_command_exit_zero_rejects_missing_argv() -> None:
    """``command_exit_zero`` with no ``args['argv']`` raises at construction time."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G1",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {},
                "policy": "block",
                "cadence": "every-wave",
            }
        )
    message = str(exc_info.value)
    assert "G1" in message
    assert "missing required args['argv']" in message


def test_gate_spec_non_argv_kind_skips_argv_check() -> None:
    """Non-argv-bearing kinds (e.g. ``schema_validate``) skip the argv check.

    The validator is opt-in by ``kind`` — kinds outside
    :data:`eawf.kernel.spec.promotion.ARGV_BEARING_GATE_KINDS` are
    unaffected so a ``schema_validate`` gate can use whatever
    ``args`` shape its per-kind validator (W08) defines.
    """
    gate = _gate(kind="regex_match", args={"pattern": r"^OK$", "input": "OK"})
    assert gate.kind == "regex_match"
    assert "argv" not in gate.args


def test_gate_spec_rejects_shell_metachars_in_argv() -> None:
    """Shell metacharacters inside ``argv`` elements raise at construction time."""
    with pytest.raises(ValidationError) as exc_info:
        GateSpec.model_validate(
            {
                "id": "G2",
                "criterion_id": "C1",
                "kind": "command_exit_zero",
                "args": {"argv": ["pytest", "tests/*"]},
                "policy": "block",
                "cadence": "every-wave",
            }
        )
    message = str(exc_info.value)
    assert "G2" in message
    assert "rejected by L0 policy" in message
