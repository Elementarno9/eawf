"""Tests for the shared spec primitives in ``eawf.kernel.spec.common``.

Covers ``EvidenceRef`` and the ``EvidenceKind`` Literal added in
P28-I01-W01 plus the agent-report reconciliation invariant: the
:class:`eawf.kernel.store.kinds.agent_report.AgentReportEvidenceRef`
``kind`` field is a strict subset (equal) of the spec
:data:`eawf.kernel.spec.common.EvidenceKind` vocabulary.
"""

from __future__ import annotations

from typing import Literal, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import EvidenceKind, EvidenceRef
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
