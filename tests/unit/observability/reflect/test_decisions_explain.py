"""``explain_decision`` answers ``eawf why`` from the native and imported decisions."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.decision import Decision, ratify, supersede
from eawf.observability.reflect.decisions import WhyReference, WhyResult, explain_decision

pytestmark = pytest.mark.unit

_SCOPE = "eawf://WSP-MAIN/PRJ-QR/REP-QR/milestone/MLS-0001"
_EVIDENCE = "eawf://WSP-MAIN/PRJ-QR/REP-QR/evidence/EVD-0001"


def _decision(key: str, **overrides: Any) -> Decision:
    fields: dict[str, Any] = {
        "key": key,
        "scope_ref": _SCOPE,
        "title": f"Verdict {key}",
        "decision": "Do the thing.",
        "rationale": "It is the thing to do.",
        "alternatives": [{"key": "do", "label": "Do it"}, {"key": "skip", "label": "Skip it"}],
        "chosen_option_key": "do",
        "consequences": ["The thing is done"],
        "evidence_refs": [_EVIDENCE],
        "effective_policy_revision": 1,
        "created_at": "2026-05-10T00:00:00Z",
    }
    fields.update(overrides)
    proposed = Decision.model_validate(fields)
    return ratify(proposed, by="OP-0001", at=proposed.created_at)


def test_explains_a_decision_by_key_with_its_scope_and_evidence() -> None:
    result = explain_decision([_decision("D01")], (), "D01")
    assert (result.kind, result.id, result.title, result.status) == (
        "decision",
        "D01",
        "Verdict D01",
        "active",
    )
    assert result.summary == "Do the thing. Rationale: It is the thing to do."
    assert result.refs == [
        WhyReference(ref=_SCOPE, kind="scope"),
        WhyReference(ref=_EVIDENCE, kind="evidence"),
    ]


def test_an_address_ending_in_the_decision_key_resolves_it() -> None:
    decisions = [_decision("D01"), _decision("D02")]
    result = explain_decision(decisions, (), "eawf://WSP-MAIN/PRJ-QR/REP-QR/decision/D02")
    assert result.id == "D02"


def test_a_superseded_decision_names_its_successor() -> None:
    old = _decision("D01")
    new = _decision("D02", supersedes="D01")
    retired = supersede(old, new)
    result = explain_decision([retired, new], (), "D01")
    assert result.status == "superseded"
    assert WhyReference(ref="D02", kind="superseded_by") in result.refs
    assert WhyReference(ref="D01", kind="supersedes") in explain_decision([new], (), "D02").refs


@pytest.mark.parametrize("decisions", [[], [_decision("D01")]], ids=["empty", "other-key"])
def test_an_unknown_key_raises_naming_it(decisions: list[Decision]) -> None:
    with pytest.raises(KeyError, match="D99"):
        explain_decision(decisions, (), "D99")


def test_the_result_refuses_an_unknown_reference_kind() -> None:
    with pytest.raises(ValidationError):
        WhyResult(
            id="D01",
            title="t",
            status="active",
            summary="s",
            refs=[{"ref": "x", "kind": "phase"}],  # type: ignore[list-item]
        )
