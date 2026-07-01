"""End-to-end acceptance for the research-campaign capstone (P30-I18-W09).

This is the CR-01 acceptance: it drives a whole research campaign through the
daemon RPC surface that the W01-W08 binding landed -- ``create_campaign`` ->
``run_campaign`` (the W01 round runner over a stubbed per-dispatch spawn) ->
``research.steer`` (the mid-run operator channel) -> ``synthesize_campaign_brief``
+ ``validate_markdown_artifact`` (the W06 EviBound feed) -- and asserts the whole
evidence chain off a TEMP fixture state. It proves, in one run:

1. a campaign executes TWO rounds, each spawning a researcher session per staged
   dispatch (the spawn is a stub returning fixture ``agent_end`` bodies -- this is
   an in-process acceptance, NOT a live researcher spawn);
2. a mid-run operator STEER injected between round 1 and round 2 reflects in round
   2's persisted record (its ``steer_notes``) while round 1 carries none -- a steer
   visibly changes the round it lands before;
3. Claims (the round-end reconcile), an OpenQuestion (the ``add_question`` writer),
   and Round records (``persist_round``) were all written through the canonical
   daemon writers -- the OpenQuestion through the portalock + WAL + event-append
   state writer, the rounds + claim ids through the append-only round store;
4. the run STOPS on a checkpoint (the ON_HALT terminal round) under a bounded
   round budget;
5. a synthesis brief built from the surviving claims is REJECTED by the EviBound
   rung-1 gate when a claim's evidence ref does not resolve, and PROMOTES when
   every ref resolves; and
6. the run evidence (campaign record + rounds + checkpoint) is reconstructible off
   the temp state alone (via ``read_latest_campaign`` / ``read_campaign_rounds`` /
   ``snapshot``).

The live ``agent.dispatch`` spawn is replaced by a recording stub producer +
fixture ``agent_end`` bodies, mirroring the W01 / W03 stub pattern in
:mod:`tests.daemon.test_research_round_runner` and
:mod:`tests.daemon.test_research_run_rpc`; no live campaign is run and no
real subprocess is spawned.
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
from eawf.kernel.spec.operator_input import (
    OperatorInput,
    OperatorInputKind,
    SteerAction,
    SteerPayload,
)
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedDispatch,
    stage_campaign,
)
from eawf.kernel.spec.round_loop import CheckpointPolicy, CheckpointTier
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus, StoreKind, Urgency
from eawf.kernel.state.models import Claim
from eawf.kernel.store.paths import store_path
from eawf.platform.artifacts.validation import validate_markdown_artifact
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    add_question,
    create_campaign,
    persist_operator_input,
    read_campaign_rounds,
    read_latest_campaign,
    run_campaign,
    snapshot,
)
from eawf.surfaces.cli.commands.draft import synthesize_campaign_brief
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Fixture state + context (the canonical add_question writer needs a state.json)
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _state_payload() -> dict[str, object]:
    """Minimal valid State payload with a project + no questions yet."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["market-structure", "pricing-models"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
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
    """Build a daemon context over an on-disk temp state fixture.

    Mirrors :mod:`tests.daemon.test_research_question_writer`: the canonical
    state writer (``add_question``) needs the event path + WAL dir + bus, while
    the append-only campaign / round / operator-input stores only need the
    state path.
    """
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(_state_payload(), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
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


def _block() -> ResearchProfileBlock:
    """A two-domain campaign profile (sorted: market-structure, pricing-models)."""
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _stage_params(campaign_id: str) -> dict[str, Any]:
    block = _block()
    campaign = stage_campaign("options-pricing landscape", block)
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": campaign.model_dump(mode="json"),
    }


def _agent_end_body(domain: str, *, evidence_ref: str, round_number: int = 1) -> dict[str, object]:
    """A valid researcher ``agent_end`` body fixture carrying one finding line.

    The finding line's evidence ref is parameterised so the acceptance can drive
    a campaign whose surviving-claim evidence either resolves (a real temp file)
    or does not (a missing path) -- the two halves of the EviBound assertion. The
    finding carries the round number so each round's finding is DISTINCT (a real
    campaign evolves), exercising the multi-round machinery rather than the W20
    dedup degenerate case.
    """
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {domain}",
        "question": f"what does {domain} reveal",
        "findings": [f"{domain} converges on a stable answer (r{round_number})"],
        "alternatives": [],
        "recommendation": f"pursue {domain}",
        "evidence_refs": [{"kind": "store_record", "ref": evidence_ref}],
    }


#: The mid-run steer note the operator pushes between round 1 and round 2.
_STEER_NOTE = "narrow onto the short-tenor curve"


def _inject_steer(state_path: Path, campaign_id: str) -> None:
    """Append a between-rounds operator STEER through the canonical blackboard writer.

    The in-process analogue of an operator typing a steer at the board's
    between-rounds pause: it builds the same typed ``steer`` :class:`OperatorInput`
    the ``research.steer`` RPC builds and lands it through the daemon-owned
    append-only operator-input store via :func:`persist_operator_input` -- the
    same canonical writer the RPC calls internally. The synchronous helper is
    used (rather than awaiting the async RPC) because the round-runner spawn seam
    fires inside ``run_campaign``'s synchronous loop, itself nested in the test's
    running event loop, where ``asyncio.run`` cannot re-enter.
    """
    op_input = OperatorInput(
        campaign_id=campaign_id,
        kind=OperatorInputKind.STEER,
        scope="campaign",
        note=_STEER_NOTE,
        urgency=Urgency.NORMAL,  # NORMAL -> queued (between-rounds), not blocking
        payload=SteerPayload(action=SteerAction.NARROW),
        at=datetime.now(UTC),
    )
    persist_operator_input(state_path, op_input)


class _SteeringSpawner:
    """A recording stub spawner that injects a mid-run steer between rounds.

    Records each spawned dispatch and returns its fixture ``agent_end`` body.
    After round 1's dispatches complete (detected when the FIRST dispatch of
    round 2 is requested), it appends a ``steer`` operator input so the next
    round's channel fold sees it. Round 1's record carries no steer note (the
    steer did not exist yet when round 1 reconciled); round 2's record carries it.
    """

    def __init__(self, state_path: Path, *, campaign_id: str, evidence_ref: str) -> None:
        self._state_path = state_path
        self._campaign_id = campaign_id
        self._evidence_ref = evidence_ref
        self._dispatches_per_round = 2  # market-structure + pricing-models
        self.spawned: list[str] = []
        self.steer_injected_at_call: int | None = None

    def __call__(self, dispatch: StagedDispatch) -> Mapping[str, object]:
        self.spawned.append(dispatch.domain)
        # The first dispatch of round 2 is the (dispatches_per_round + 1)-th
        # overall spawn; round 1 has already reconciled by then, so a steer
        # injected now lands on round 2's record, not round 1's.
        if len(self.spawned) == self._dispatches_per_round + 1:
            self.steer_injected_at_call = len(self.spawned)
            _inject_steer(self._state_path, self._campaign_id)
        round_number = (len(self.spawned) - 1) // self._dispatches_per_round + 1
        return _agent_end_body(
            dispatch.domain, evidence_ref=self._evidence_ref, round_number=round_number
        )


# --------------------------------------------------------------------------
# CR-01 -- the end-to-end capstone acceptance
# --------------------------------------------------------------------------


def test_campaign_acceptance_two_rounds_steer_claims_checkpoint_and_evibound(
    tmp_path: Path,
) -> None:
    """The full capstone: 2 rounds + steer + claims/questions/rounds + checkpoint + EviBound.

    Drives the whole campaign control plane in one in-process run against a temp
    fixture state and asserts every leg of CR-01. The researcher spawn is a stub
    returning fixture ``agent_end`` bodies -- no live researcher subprocess.
    """
    ctx, state_path = _build_ctx(tmp_path)
    campaign_id = "campaign-accept"
    # The evidence ref each round's claim carries -- a real file under the temp
    # project root, so the synthesised brief's refs RESOLVE for the promote leg.
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "survey.md").write_text("survey notes", encoding="utf-8")
    resolving_ref = "evidence/survey.md"

    async def body() -> None:
        # (1) Seed a toy OpenQuestion through the canonical state writer, then
        # stage + persist the campaign through the canonical campaign writer.
        oq = await add_question(
            ctx,
            {"title": "which curve model fits the short tenor", "question_id": "OQ-tenor"},
        )
        assert oq["status"] == "open"
        await create_campaign(ctx, _stage_params(campaign_id))

        # (2) Run a TWO-round campaign. The stub spawner injects a mid-run steer
        # between round 1 and round 2. The round budget bounds the loop at 2; the
        # ON_HALT checkpoint policy records the terminal round as the checkpoint.
        spawner = _SteeringSpawner(state_path, campaign_id=campaign_id, evidence_ref=resolving_ref)
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id=campaign_id, round_budget=2),
            produce_agent_end=spawner,
            checkpoint_policy=CheckpointPolicy(tier=CheckpointTier.ON_HALT),
        )

        # -- 2 rounds with spawned (stubbed) researchers, one session per domain.
        assert result["rounds_run"] == 2
        assert spawner.spawned == [
            "market-structure",
            "pricing-models",  # round 1
            "market-structure",
            "pricing-models",  # round 2
        ]
        # The steer was injected at the start of round 2 (the 3rd overall spawn).
        assert spawner.steer_injected_at_call == 3

        # -- STOPS on a checkpoint under the bounded budget (ON_HALT terminal round).
        assert result["halt_reason"] == "round_budget"
        assert result["saturated"] is False
        assert result["checkpoints"] >= 1

        # -- Claims were written: 1 finding/domain x 2 domains x 2 rounds = 4 claim ids.
        assert len(result["claim_ids"]) == 4

        # (3) Round records were written through the canonical (append-only) writer,
        # and the mid-run steer reflects in round 2's record but not round 1's.
        rounds = read_campaign_rounds(state_path, campaign_id)
        assert [r.round_number for r in rounds] == [1, 2]
        assert rounds[0].steer_notes == []
        assert any(_STEER_NOTE in note for note in rounds[1].steer_notes), rounds[1].steer_notes
        # The per-round findings + claim ids are persisted on the round record.
        assert rounds[0].finding_lines == [
            "market-structure converges on a stable answer (r1)",
            "pricing-models converges on a stable answer (r1)",
        ]
        assert rounds[1].claim_ids == [
            "CLM-r2-market-structure-0",
            "CLM-r2-pricing-models-0",
        ]
        # The terminal round coincides with a recorded checkpoint (ON_HALT).
        assert rounds[1].checkpoint is True

        # -- The seeded OpenQuestion is reconstructible off the canonical state
        # and W17 resolved it: the run's answering claim marked it ANSWERED and
        # linked both sides (no phantom OPEN question after a productive run).
        state = load_state(state_path)
        assert state.open_questions is not None
        assert "OQ-tenor" in state.open_questions
        resolved_q = state.open_questions["OQ-tenor"]
        assert resolved_q.status is OpenQuestionStatus.ANSWERED
        assert resolved_q.answered_by_claim_id is not None
        assert state.claims[resolved_q.answered_by_claim_id].answers_question_id == "OQ-tenor"

        # -- The live run FOLDED its reconciled claims into the canonical
        # state.claims (not a throwaway shadow): every round-record claim id
        # resolves to a real Claim row on the persisted state (W05/W06 binding).
        assert state.claims is not None
        assert len(state.claims) == 4
        for round_record in rounds:
            for claim_id in round_record.claim_ids:
                assert claim_id in state.claims, claim_id
                assert state.claims[claim_id].evidence_refs == [resolving_ref]

        # (5) The run evidence (campaign record + rounds + checkpoint) is
        # reconstructible off the temp state alone -- the snapshot RPC folds it.
        snap = await snapshot(ctx, {"campaign_id": campaign_id})
        assert snap["campaign_id"] == campaign_id
        # W16: a completed run flips the campaign to its terminal CONVERGED state
        # (it no longer lingers ACTIVE forever).
        assert snap["status"] == "converged"
        assert snap["rounds_run"] == 2
        assert snap["total_findings"] == 4
        assert snap["total_claims"] == 4
        assert snap["checkpoints"] >= 1
        # The campaign record itself reads back as ACTIVE off the store.
        persisted = read_latest_campaign(state_path, campaign_id)
        assert persisted is not None
        assert persisted.campaign.topic == "options-pricing landscape"

        # (4) Synthesise a brief from the surviving claims and promote it through
        # the EviBound rung-1 gate. Reconstruct the survivor Claim rows from the
        # persisted round findings (the run reconciles claims into the round
        # record's claim ids; the surviving-claim evidence is the body's refs).
        survivors = _survivor_claims(rounds, evidence_ref=resolving_ref)

        # -- A fully-referenced synthesis PROMOTES (every ref resolves).
        ok_brief = synthesize_campaign_brief("options-pricing landscape", survivors)
        assert ok_brief.evidence_refs == [resolving_ref]
        ok_report = validate_markdown_artifact(
            _synthesis_body(), intent=ok_brief, project_root=tmp_path
        )
        assert ok_report.ok, ok_report.errors

        # -- An unresolved-refs synthesis is REJECTED by the EviBound gate.
        bad_survivors = _survivor_claims(rounds, evidence_ref="evidence/missing.md")
        bad_brief = synthesize_campaign_brief("options-pricing landscape", bad_survivors)
        bad_report = validate_markdown_artifact(
            _synthesis_body(), intent=bad_brief, project_root=tmp_path
        )
        assert not bad_report.ok
        assert any("evidence/missing.md" in err and "rung-1" in err for err in bad_report.errors), (
            bad_report.errors
        )

    _run(body)


def test_campaign_acceptance_saturation_halt_records_checkpoint(tmp_path: Path) -> None:
    """A dry round halts the run early on saturation -- still a recorded checkpoint.

    The companion stop-condition to the budget halt above: when a round returns no
    findings the saturation reducer declares the campaign dry and the loop halts
    on saturation (round 1), recording the ON_HALT checkpoint on the terminal
    round. Proves the run stops on a checkpoint via the saturation gate too.
    """
    ctx, state_path = _build_ctx(tmp_path)
    campaign_id = "campaign-dry"

    def _produce_empty(dispatch: StagedDispatch) -> Mapping[str, object]:
        body = _agent_end_body(dispatch.domain, evidence_ref="evidence/x.md")
        body["findings"] = []  # no findings -> the round saturates
        return body

    async def body() -> None:
        await create_campaign(ctx, _stage_params(campaign_id))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id=campaign_id, round_budget=5),
            produce_agent_end=_produce_empty,
            checkpoint_policy=CheckpointPolicy(tier=CheckpointTier.ON_HALT),
        )
        assert result["rounds_run"] == 1
        assert result["halt_reason"] == "saturated"
        assert result["saturated"] is True
        assert result["checkpoints"] >= 1
        rounds = read_campaign_rounds(state_path, campaign_id)
        assert len(rounds) == 1
        assert rounds[0].saturated is True
        assert rounds[0].checkpoint is True

    _run(body)


# --------------------------------------------------------------------------
# Helpers: reconstruct survivor claims + a synthesis-artifact chassis body
# --------------------------------------------------------------------------


def _survivor_claims(rounds: Any, *, evidence_ref: str) -> list[Claim]:
    """Rebuild the surviving (OPEN) Claim rows from the persisted round records.

    ``run_campaign`` reconciles each round's findings into an in-memory claim
    shadow and persists the claim ids + finding lines on the round record (the
    state-resident claim fold is a downstream wave). For the synthesis leg the
    acceptance rebuilds the survivor Claim rows from those persisted findings,
    binding each to the round's evidence ref -- the same ``evidence_refs`` the
    round-end reconcile would carry -- so the EviBound gate scores a real ref.
    """
    claims: list[Claim] = []
    for record in rounds:
        for claim_id, line in zip(record.claim_ids, record.finding_lines, strict=True):
            claims.append(
                Claim(
                    id=claim_id,
                    scope_id="QR",
                    title=line if len(line) <= 72 else f"{line[:69]}...",
                    status=ClaimStatus.OPEN,
                    evidence_refs=[evidence_ref],
                    created_at=_now(),
                )
            )
    return claims


def _synthesis_body() -> str:
    """A minimal chassis-clean synthesis artifact body (no scrub/citation faults).

    The chassis itself validates; the only promotion gate the acceptance exercises
    is the EviBound rung-1 check over the supplied brief's ``evidence_refs``.
    """
    return "\n".join(
        [
            "# Campaign synthesis",
            "",
            "## Summary",
            "",
            "Synthesised the campaign findings.",
            "",
            "## References",
            "",
            "(none)",
            "",
            "## Provenance",
            "",
            "- kind: research",
            "",
            "## Scrub",
            "",
            "- status: clean",
            "",
        ]
    )
