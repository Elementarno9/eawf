"""Tests for the W05 Round entity + progress fold (P30-I18-W05).

Covers the real :class:`~eawf.kernel.state.models.Round` records replacing the
synthetic board round node + the
:class:`~eawf.kernel.spec.operator_input.CampaignProgressState` fold:

* :class:`~eawf.kernel.state.models.Round` is a typed projection of a persisted
  round store record.
* :func:`~eawf.surfaces.tui.modes.research_board.read_round_rows` reads the
  ``research_round`` store into typed Round rows.
* :func:`~eawf.surfaces.tui.modes.research_board.project_campaign_progress`
  folds the ledgers + executed rounds into a CampaignProgressState.
* :func:`~eawf.surfaces.tui.modes.research_board.render_progress` surfaces the
  fold answer in the RUN band + the executed-round count in the ROUND band.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf import __version__
from eawf.kernel.spec.operator_input import CampaignProgressKind
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.models import Round
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    create_campaign,
    run_campaign,
)
from eawf.surfaces.tui.modes.research_board import (
    CampaignRow,
    project_campaign_progress,
    read_round_rows,
    render_progress,
)

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Round model
# --------------------------------------------------------------------------


def test_round_model_defaults() -> None:
    """A Round projection validates with sensible defaults."""
    rnd = Round(campaign_id="campaign-x", round_number=1)
    assert rnd.finding_count == 0
    assert rnd.claim_count == 0
    assert rnd.saturated is False
    assert rnd.checkpoint is False
    assert rnd.steer_notes == []


def test_round_model_rejects_extra_field() -> None:
    """An unknown field is rejected by extra='forbid'."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Round.model_validate({"campaign_id": "c", "round_number": 1, "rogue": True})


def test_round_model_rejects_zero_round_number() -> None:
    """A round number below 1 violates the ge=1 bound."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Round(campaign_id="c", round_number=0)


# --------------------------------------------------------------------------
# read_round_rows -- the synthetic node is replaced by real rounds
# --------------------------------------------------------------------------


def _run_a_campaign(state_path: Path, campaign_id: str, *, budget: int) -> None:
    ctx = MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        state_path=state_path,
    )
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={"market-structure": ResearchDomainConfig(focus="venues")},
    )
    campaign = stage_campaign("options-pricing landscape", block)
    import asyncio

    asyncio.run(
        create_campaign(
            ctx,
            {
                "campaign_id": campaign_id,
                "config": block.model_dump(mode="json"),
                "campaign": campaign.model_dump(mode="json"),
            },
        )
    )

    def _produce(dispatch: object) -> dict[str, object]:
        return {
            "role": "researcher",
            "verdict": "pass",
            "confidence": "medium",
            "summary": "surveyed",
            "question": "what does it reveal",
            "findings": ["a claim"],
            "recommendation": "pursue it",
            "evidence_refs": [{"kind": "store_record", "ref": "src/x.py:1"}],
        }

    run_campaign(
        ctx,
        RunCampaignParams(campaign_id=campaign_id, round_budget=budget),
        produce_agent_end=_produce,  # type: ignore[arg-type]
    )


def test_read_round_rows_reads_persisted_rounds(tmp_path: Path) -> None:
    """A run persists rounds the board reads back as typed Round rows."""
    state_path = tmp_path / "state.json"
    _run_a_campaign(state_path, "campaign-r", budget=3)
    rounds = read_round_rows(state_path)
    assert [r.round_number for r in rounds] == [1, 2, 3]
    assert all(r.campaign_id == "campaign-r" for r in rounds)
    assert rounds[0].finding_count == 1
    assert rounds[0].claim_count == 1


def test_read_round_rows_empty_when_no_store(tmp_path: Path) -> None:
    """A scope that ran no campaign reads zero rounds (the pre-run path)."""
    assert read_round_rows(tmp_path / "state.json") == ()


def test_read_round_rows_none_state_path() -> None:
    """A None state path (user scope) yields no rounds rather than raising."""
    assert read_round_rows(None) == ()


# --------------------------------------------------------------------------
# project_campaign_progress -- the CampaignProgressState fold
# --------------------------------------------------------------------------


def _campaign_row() -> CampaignRow:
    return CampaignRow(
        campaign_id="RC-0001",
        topic="Survey",
        domains=("market-structure", "pricing-models"),
        default_depth="medium",
    )


def test_project_progress_runnable_when_domains_ready() -> None:
    """A staged campaign with no blocking question folds to RUNNABLE."""
    progress = project_campaign_progress((_campaign_row(),), (), (), ())
    assert progress.kind is CampaignProgressKind.RUNNABLE


def test_project_progress_saturated_when_round_converged() -> None:
    """A saturated executed round pins the SATURATED good-stop."""
    rounds = (Round(campaign_id="RC-0001", round_number=1, saturated=True),)
    progress = project_campaign_progress((_campaign_row(),), (), (), rounds)
    assert progress.kind is CampaignProgressKind.SATURATED
    assert progress.round_index == 1


def test_project_progress_blocked_await_user_on_blocking_question() -> None:
    """A blocking open question forces BLOCKED_AWAIT_USER (D-2)."""
    from eawf.kernel.state.models import OpenQuestion

    question = OpenQuestion(
        id="OQ-1",
        scope_id="RC-0001",
        title="is the feed authoritative",
        status="blocked",  # type: ignore[arg-type]
        blocking=True,
        created_at=_now(),
    )
    progress = project_campaign_progress((_campaign_row(),), (), (question,), ())
    assert progress.kind is CampaignProgressKind.BLOCKED_AWAIT_USER


# --------------------------------------------------------------------------
# render_progress -- RUN band reflects the fold, ROUND counts the runs
# --------------------------------------------------------------------------


def test_render_progress_run_band_reflects_progress_and_round_count() -> None:
    """The RUN band reads the fold kind; the ROUND band counts executed rounds."""
    rounds = (
        Round(campaign_id="RC-0001", round_number=1),
        Round(campaign_id="RC-0001", round_number=2, saturated=True),
    )
    body = render_progress((_campaign_row(),), (), (), checkpoints=0, rounds=rounds)
    # A saturated terminal round folds the RUN band to saturated.
    assert "RUN" in body
    assert "saturated" in body
    assert "2 run" in body
    assert "not yet wired" not in body
