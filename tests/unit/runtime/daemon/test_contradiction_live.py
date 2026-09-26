"""Tests for the live-claim contradiction path: refutation, seeding, sealing.

Covers the live-claim contradiction guarantees:

* :func:`~eawf.runtime.daemon.methods.research.reconcile_round_claims` folds a
  researcher's ``refuted_claim_ids`` into a REFUTED status on the named LIVE
  claim -- previously no code path ever produced that status. The same
  reconcile also SEALS an ``AUTO_RESOLVED`` question once a round boundary has
  passed it by (PLAN-036's override window is exactly one round wide).
* ``run_campaign`` halts with a ``contradiction`` when a round refutes a live
  claim, leaving the campaign ACTIVE. How that halt is scoped across campaigns
  and across runs is covered in ``test_contradiction_scope.py``.
* :func:`~eawf.runtime.daemon.methods.research.build_bound_round_runner` is
  wired into ``run_campaign`` as its production round-runner binding, not left
  as an untested, uncalled seam.
* :func:`~eawf.runtime.daemon.methods.research._researcher_prompt` actually
  tells a researcher when to populate ``refuted_claim_ids`` and offers the
  scope's live claim ids to name -- the field existed on the wire but no
  producer ever told a live researcher to fill it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.campaign_driver import RoundFindings
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedDispatch,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    CampaignStatus,
    ClaimStatus,
    Confidence,
    OpenQuestionStatus,
    StoreKind,
)
from eawf.kernel.state.models import Claim, OpenQuestion, State
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import research as research_mod
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    _researcher_prompt,
    create_campaign,
    read_latest_campaign,
    reconcile_round_claims,
    run_campaign,
)

pytestmark = pytest.mark.unit

_CODE = "QR"


# --------------------------------------------------------------------------
# reconcile_round_claims -- unit level: REFUTED marking + SEALED production
# --------------------------------------------------------------------------


def _report_body(
    *, findings: list[str] | None = None, refuted_claim_ids: list[str] | None = None
) -> ResearcherReportBody:
    return ResearcherReportBody(
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.MEDIUM,
        summary="reviewed the venue landscape",
        question="does the venue claim hold",
        findings=findings or [],
        recommendation="proceed",
        refuted_claim_ids=refuted_claim_ids or [],
    )


def test_reconcile_round_claims_marks_named_claim_refuted() -> None:
    """A finding naming a live claim id flips it REFUTED -- the only such path."""
    live_claim = Claim(
        id="CLM-r1-market-structure-0",
        scope_id="research",
        title="dark pools dominate block volume",
        status=ClaimStatus.OPEN,
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    state = State.model_construct(
        claims={live_claim.id: live_claim}, open_questions={}, project=None
    )
    findings = RoundFindings(
        round_number=2,
        domains=("market-structure",),
        bodies=(_report_body(refuted_claim_ids=[live_claim.id]),),
    )

    written = reconcile_round_claims(
        state, findings, scope_id=None, now=datetime(2026, 9, 25, tzinfo=UTC)
    )

    assert written == [live_claim.id]
    assert state.claims is not None
    assert state.claims[live_claim.id].status is ClaimStatus.REFUTED


def test_reconcile_round_claims_ignores_refutation_of_unknown_or_dead_claim() -> None:
    """A stale / foreign / already-REFUTED reference is skipped, not raised."""
    dead_claim = Claim(
        id="CLM-r1-market-structure-0",
        scope_id="research",
        title="already refuted claim",
        status=ClaimStatus.REFUTED,
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    state = State.model_construct(
        claims={dead_claim.id: dead_claim}, open_questions={}, project=None
    )
    findings = RoundFindings(
        round_number=2,
        domains=("market-structure",),
        bodies=(_report_body(refuted_claim_ids=[dead_claim.id, "CLM-does-not-exist"]),),
    )

    written = reconcile_round_claims(
        state, findings, scope_id=None, now=datetime(2026, 9, 25, tzinfo=UTC)
    )

    assert written == []
    assert state.claims is not None
    assert state.claims[dead_claim.id].status is ClaimStatus.REFUTED


def test_reconcile_round_claims_seals_auto_resolved_question_from_earlier_round() -> None:
    """PLAN-036's override window is one round wide: the next reconcile seals it."""
    question = OpenQuestion(
        id="OQ-1",
        scope_id="research",
        title="which venue dominates",
        status=OpenQuestionStatus.AUTO_RESOLVED,
        answered_by_claim_id="CLM-r1-market-structure-0",
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
        resolved_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    state = State.model_construct(claims={}, open_questions={"OQ-1": question}, project=None)
    findings = RoundFindings(
        round_number=2,
        domains=("market-structure",),
        bodies=(_report_body(),),
    )

    reconcile_round_claims(state, findings, scope_id=None, now=datetime(2026, 9, 25, tzinfo=UTC))

    assert state.open_questions is not None
    assert state.open_questions["OQ-1"].status is OpenQuestionStatus.SEALED


def test_reconcile_round_claims_does_not_seal_the_question_it_just_auto_resolved() -> None:
    """A pairing THIS round's own elimination step makes stays AUTO_RESOLVED."""
    question = OpenQuestion(
        id="OQ-1",
        scope_id="research",
        title="which venue dominates",
        status=OpenQuestionStatus.OPEN,
        created_at=datetime(2026, 9, 25, tzinfo=UTC),
    )
    state = State.model_construct(claims={}, open_questions={"OQ-1": question}, project=None)
    findings = RoundFindings(
        round_number=1,
        domains=("market-structure",),
        bodies=(_report_body(findings=["dark pools dominate block volume"]),),
    )

    reconcile_round_claims(state, findings, scope_id=None, now=datetime(2026, 9, 25, tzinfo=UTC))

    assert state.open_questions is not None
    assert state.open_questions["OQ-1"].status is OpenQuestionStatus.AUTO_RESOLVED


# --------------------------------------------------------------------------
# _researcher_prompt -- the refutation instruction + the live-claim offer
# --------------------------------------------------------------------------


def _dispatch(domain: str = "market-structure") -> StagedDispatch:
    return StagedDispatch(
        domain=domain,
        agent_role="researcher",
        depth=ResearchDepth.MEDIUM,
        prompt="Survey the options-pricing landscape",
    )


def test_researcher_prompt_offers_live_claims_and_names_the_instruction() -> None:
    """A researcher is told plainly WHEN to name refuted_claim_ids, with real ids to name."""
    live = (
        Claim(
            id="CLM-r1-market-structure-0",
            scope_id="research",
            title="dark pools dominate block volume",
            status=ClaimStatus.OPEN,
            created_at=datetime(2026, 9, 24, tzinfo=UTC),
        ),
    )

    prompt = _researcher_prompt(_dispatch(), live_claims=live)

    assert "CLM-r1-market-structure-0" in prompt
    assert "dark pools dominate block volume" in prompt
    assert "refuted_claim_ids" in prompt
    assert "directly contradicts" in prompt


def test_researcher_prompt_omits_the_ledger_note_when_empty() -> None:
    """No live claims yet (a first round) -- no ledger note, but the field still ships."""
    prompt = _researcher_prompt(_dispatch(), live_claims=())

    assert "Live claims already on this scope's ledger" not in prompt
    assert "refuted_claim_ids" in prompt  # still advertised in the JSON schema


# --------------------------------------------------------------------------
# run_campaign -- live: contradiction halt, carried-ledger seeding, wiring
# --------------------------------------------------------------------------


def _state_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": f"urn:eawf:v1:state:{_CODE}",
        "updated_at": datetime(2026, 9, 25, tzinfo=UTC).isoformat(),
        "project": {
            "code": _CODE,
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["market-structure"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": f"urn:eawf:v1:repo:{_CODE}",
        },
        "current": {"project_code": _CODE},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _build_ctx(tmp_path: Path) -> tuple[MethodContext, Path]:
    """A daemon context over real on-disk state, so claims fold into state."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(_state_payload()))
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    ctx = MethodContext(
        started_at="2026-09-25T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def _one_domain_block() -> ResearchProfileBlock:
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={"market-structure": ResearchDomainConfig(focus="venues + flow")},
    )


def _create_params(campaign_id: str) -> dict[str, Any]:
    block = _one_domain_block()
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": stage_campaign("options-pricing landscape", block).model_dump(mode="json"),
        "budget_limits": {},
    }


def _pass_body(finding: str | None, *, refuted: list[str] | None = None) -> dict[str, object]:
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": "surveyed the venue landscape",
        "question": "what do venues reveal",
        "findings": [finding] if finding else [],
        "recommendation": "proceed",
        "refuted_claim_ids": refuted or [],
    }


def _create_then_refute_producer() -> Callable[[StagedDispatch], Mapping[str, object]]:
    """Round 1 creates a claim; round 2 refutes it by its deterministic id."""
    calls: list[str] = []

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        calls.append(dispatch.domain)
        if len(calls) == 1:
            return _pass_body("dark pools dominate block volume")
        return _pass_body(None, refuted=["CLM-r1-market-structure-0"])

    return _produce


def _noop_producer(dispatch: StagedDispatch) -> Mapping[str, object]:
    """Never creates or refutes anything -- a quiet, non-novel round."""
    return _pass_body(None)


def test_run_campaign_marks_refuted_claim_and_halts_with_contradiction(tmp_path: Path) -> None:
    """Gate-fire proof: a live refutation halts the run and leaves it ACTIVE."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-contra"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-contra", round_budget=3),
            produce_agent_end=_create_then_refute_producer(),
        )
        assert result["rounds_run"] == 2
        assert result["halt_reason"] == "contradiction"
        assert result["contradiction_offenders"] == ["CLM-r1-market-structure-0"]
        latest = read_latest_campaign(state_path, "rc-contra")
        assert latest is not None and latest.status is CampaignStatus.ACTIVE

    _run(body)


def test_run_campaign_wires_through_build_bound_round_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof: run_campaign's round runner IS build_bound_round_runner.

    ``build_bound_round_runner`` composed the live spawner + the campaign-
    driver round runner but had no production caller -- ``run_campaign``
    duplicated its two-line body inline instead of calling it, so the seam
    was unit-tested in isolation but never actually driven end to end.
    """
    ctx, _state_path = _build_ctx(tmp_path)
    calls: list[Any] = []
    real = research_mod.build_bound_round_runner

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(research_mod, "build_bound_round_runner", _spy)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-wired"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-wired", round_budget=1),
            produce_agent_end=_noop_producer,
        )
        assert result["rounds_run"] == 1

    _run(body)
    assert len(calls) == 1
