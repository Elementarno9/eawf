"""Tests for the W06 EviBound feed + pruning wire.

Covers the two halves of the binding:

* :func:`~eawf.surfaces.cli.commands.draft.synthesize_campaign_brief` folds a
  campaign's surviving claims into an :class:`IntentBrief` whose ``evidence_refs``
  the EviBound rung-1 gate scores at the operator promote path -- a synthesis
  whose refs do not resolve is rejected, a fully-referenced one promotes.
* :func:`~eawf.kernel.spec.pruning.prune_round_carryover` is the L1 reducer the
  bounded round loop calls between rounds: dead rows drop, live claims carry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.spec.pruning import DropReason, PruneLevel, prune_round_carryover
from eawf.kernel.spec.round_loop import RoundOutcome, run_round_loop
from eawf.kernel.spec.saturation import SaturationReport
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion
from eawf.platform.artifacts.validation import validate_markdown_artifact
from eawf.surfaces.cli.commands.draft import synthesize_campaign_brief

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _claim(claim_id: str, *, status: ClaimStatus, evidence: list[str]) -> Claim:
    return Claim(
        id=claim_id,
        scope_id="QR",
        title=f"claim {claim_id}",
        status=status,
        evidence_refs=evidence,
        created_at=_now(),
    )


def _artifact_body() -> str:
    return "\n".join(
        [
            "# Plan",
            "",
            "## Summary",
            "",
            "Synthesised.",
            "",
            "## References",
            "",
            "(none)",
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


# --------------------------------------------------------------------------
# synthesize_campaign_brief -- the EviBound feed
# --------------------------------------------------------------------------


def test_synthesis_aggregates_surviving_claim_evidence(tmp_path: Path) -> None:
    """The brief aggregates only the live claims' evidence refs (deduped)."""
    claims = [
        _claim("CL-1", status=ClaimStatus.SUPPORTED, evidence=["docs/a.md", "docs/b.md"]),
        _claim("CL-2", status=ClaimStatus.OPEN, evidence=["docs/b.md"]),  # dup b
        _claim("CL-3", status=ClaimStatus.REFUTED, evidence=["docs/dead.md"]),  # pruned
    ]
    brief = synthesize_campaign_brief("options-pricing", claims)
    # Only the two live claims contribute evidence, deduped + order-preserved.
    assert brief.evidence_refs == ["docs/a.md", "docs/b.md"]
    assert "docs/dead.md" not in brief.evidence_refs
    assert brief.planned_steps == ["claim CL-1", "claim CL-2"]


def test_synthesis_promotes_when_evidence_resolves(tmp_path: Path) -> None:
    """A synthesis whose refs all resolve passes the EviBound gate."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("hi", encoding="utf-8")
    claims = [_claim("CL-1", status=ClaimStatus.SUPPORTED, evidence=["docs/a.md"])]
    brief = synthesize_campaign_brief("topic", claims)
    report = validate_markdown_artifact(_artifact_body(), intent=brief, project_root=tmp_path)
    assert report.ok, report.errors


def test_synthesis_rejected_when_evidence_does_not_resolve(tmp_path: Path) -> None:
    """A synthesis whose ref does not resolve is rejected by the EviBound gate."""
    claims = [_claim("CL-1", status=ClaimStatus.SUPPORTED, evidence=["docs/missing.md"])]
    brief = synthesize_campaign_brief("topic", claims)
    report = validate_markdown_artifact(_artifact_body(), intent=brief, project_root=tmp_path)
    assert not report.ok
    assert any("docs/missing.md" in err and "rung-1" in err for err in report.errors)


def test_synthesis_empty_survivor_set_yields_evidence_less_brief(tmp_path: Path) -> None:
    """A campaign with only dead claims synthesises an evidence-less brief."""
    claims = [_claim("CL-1", status=ClaimStatus.SUPERSEDED, evidence=["docs/old.md"])]
    brief = synthesize_campaign_brief("topic", claims)
    assert brief.evidence_refs == []
    assert brief.planned_steps == []


# --------------------------------------------------------------------------
# prune_round_carryover -- the L1 reducer wired between rounds
# --------------------------------------------------------------------------


def test_carryover_drops_dead_rows_keeps_live(tmp_path: Path) -> None:
    """The L1 carryover drops SUPERSEDED rows and keeps the live claims."""
    claims = [
        _claim("CL-live", status=ClaimStatus.SUPPORTED, evidence=["docs/a.md"]),
        _claim("CL-dead", status=ClaimStatus.SUPERSEDED, evidence=["docs/old.md"]),
    ]
    result = prune_round_carryover(claims, [], now=_now())
    assert result.level is PruneLevel.L1
    assert result.kept == ("CL-live",)
    assert result.dropped_for(DropReason.SUPERSEDED) == ("CL-dead",)


def test_carryover_drops_answers_to_dropped_question() -> None:
    """A claim answering a DROPPED question is pruned as dead context."""
    question = OpenQuestion(
        id="OQ-x",
        scope_id="QR",
        title="a dropped question",
        status=OpenQuestionStatus.DROPPED,
        created_at=_now(),
    )
    claim = Claim(
        id="CL-ans",
        scope_id="QR",
        title="answers the dropped one",
        status=ClaimStatus.OPEN,
        evidence_refs=["docs/a.md"],
        answers_question_id="OQ-x",
        created_at=_now(),
    )
    result = prune_round_carryover([claim], [question], now=_now())
    assert result.kept == ()
    assert result.dropped_for(DropReason.ANSWERS_DROPPED_QUESTION) == ("CL-ans",)


def test_round_loop_calls_carryover_between_rounds() -> None:
    """The bounded round loop calls the L1 carryover between rounds.

    Drives :func:`run_round_loop` with a runner that appends a claim each round
    (one of them dead) and prunes the accumulated ledger via
    :func:`prune_round_carryover` -- proving the reducer is the between-rounds
    wire the loop drives. The surviving (live) claims are what each later round
    carries forward.
    """
    ledger: list[Claim] = []
    carryover_calls: list[tuple[str, ...]] = []

    def _runner(round_number: int) -> RoundOutcome:
        ledger.append(
            _claim(f"CL-live-{round_number}", status=ClaimStatus.SUPPORTED, evidence=["docs/a.md"])
        )
        ledger.append(_claim(f"CL-dead-{round_number}", status=ClaimStatus.SUPERSEDED, evidence=[]))
        # The loop prunes the carried ledger between rounds (L1).
        pruned = prune_round_carryover(ledger, [], now=_now())
        carryover_calls.append(pruned.kept)
        # Not dry until the budget is spent.
        report = SaturationReport(
            saturated=False, gates=(), live_claim_count=len(pruned.kept), empty_ledger=False
        )
        return RoundOutcome(saturation=report)

    result = run_round_loop(_runner, round_budget=2)
    assert result.rounds_run == 2
    # The carryover ran each round, keeping only the live claims.
    assert carryover_calls[0] == ("CL-live-1",)
    assert carryover_calls[1] == ("CL-live-1", "CL-live-2")
