"""PLAN-007: source refs resolve and entailing claims retain their evidence.

A brief with a missing reference is refused at promotion, a brief whose dense
citations and references all resolve passes, and a claim whose entail rung did
not pass -- or passed without keeping its evidence -- cannot promote.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.epoch2.evidence_rung import (
    RUNG_NAMES,
    RUNG_QUESTIONS,
    EvidenceRungRecord,
    promotion_blockers,
)
from eawf.platform.artifacts.validation import validate_markdown_artifact

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
CLAIM = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
EVIDENCE = f"{SLOT}/evidence/EVD-0007"
DIGEST = "sha256:" + "b" * 64
AT = datetime(2026, 9, 18, tzinfo=UTC)


def _body(summary: str = "Replay keeps event order [1].", refs: str = "[1] docs/x.md") -> str:
    return "\n".join(
        [
            "# Plan",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## References",
            "",
            refs,
            "",
            "## Provenance",
            "",
            "- kind: plan",
            "",
            "## Scrub",
            "",
            "- status: clean",
            "",
        ]
    )


def _brief(*refs: str) -> IntentBrief:
    return IntentBrief(problem="p", desired_outcome="o", evidence_refs=list(refs))


def _rung(rung: int, outcome: str = "passed", **overrides: Any) -> EvidenceRungRecord:
    returned = outcome in ("passed", "failed")
    fields: dict[str, Any] = {
        "claim_ref": CLAIM,
        "rung": rung,
        "name": RUNG_NAMES[rung],
        "question": RUNG_QUESTIONS[rung],
        "outcome": outcome,
        "input_refs": [{"ref": EVIDENCE, "digest": DIGEST}],
        "finding": "entails",
        "evaluated_at": AT if returned else None,
        "evaluator": "evidence scorer",
        "evidence_ref": EVIDENCE if returned else None,
        "kept_with": CLAIM,
        "written_at_sequence": 1,
        "revision": 0,
    }
    fields.update(overrides)
    return EvidenceRungRecord.model_validate(fields)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("replay notes", encoding="utf-8")
    return tmp_path


def test_plan_007_valid_dense_citations_and_resolving_refs_pass(root: Path) -> None:
    report = validate_markdown_artifact(_body(), intent=_brief("docs/x.md"), project_root=root)
    assert report.ok, report.errors


def test_plan_007_a_missing_ref_blocks_promotion(root: Path) -> None:
    report = validate_markdown_artifact(
        _body(), intent=_brief("docs/x.md", "docs/gone.md"), project_root=root
    )
    assert not report.ok
    assert any("docs/gone.md" in e for e in report.errors)


def test_plan_007_a_dense_marker_with_no_reference_row_blocks(root: Path) -> None:
    report = validate_markdown_artifact(
        _body(summary="Replay keeps order [1] and [2]."), intent=_brief(), project_root=root
    )
    assert not report.ok


def test_plan_007_an_entailing_claim_with_retained_evidence_promotes() -> None:
    assert promotion_blockers([_rung(1), _rung(2), _rung(3, "failed"), _rung(4)]) == ()


def test_plan_007_a_non_entailing_claim_is_blocked() -> None:
    blockers = promotion_blockers([_rung(1), _rung(4, "failed")])
    assert blockers == ("rung 4 (entail) has not passed: failed",)


def test_plan_007_a_claim_with_an_unresolved_reference_is_blocked() -> None:
    unresolved = _rung(1, "failed", input_refs=[{"ref": EVIDENCE, "digest": None}])
    assert promotion_blockers([unresolved, _rung(4)])[0].startswith("rung 1 (resolve)")


def test_plan_007_rung_three_alone_never_promotes() -> None:
    assert len(promotion_blockers([_rung(3)])) == 2


def test_plan_007_an_entail_pass_that_kept_no_evidence_is_blocked() -> None:
    blockers = promotion_blockers([_rung(1), _rung(4, evidence_ref=None)])
    assert blockers == ("rung 4 passed without keeping the evidence it entailed the claim with",)


def test_plan_007_an_attested_entail_does_not_certify() -> None:
    blockers = promotion_blockers([_rung(1), _rung(4, basis="attested")])
    assert blockers == ("rung 4 is attested, and an attestation does not certify",)


def test_plan_007_a_later_revision_decides() -> None:
    stale = _rung(4, "failed")
    fresh = _rung(4, revision=1)
    assert promotion_blockers([_rung(1), stale, fresh]) == ()
    assert promotion_blockers([_rung(1), fresh, stale]) == ()


def test_plan_007_no_records_block_on_both_certifying_rungs() -> None:
    assert len(promotion_blockers([])) == 2


def test_plan_007_records_of_two_claims_are_refused() -> None:
    other = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0005"
    with pytest.raises(ValueError, match="per claim"):
        promotion_blockers([_rung(1), _rung(4, claim_ref=other, kept_with=other)])
