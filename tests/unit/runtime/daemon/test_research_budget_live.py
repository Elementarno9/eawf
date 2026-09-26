"""Live ``run_campaign`` tests for the evidence budget, saturation and status edges.

Drives the real daemon ``research.*`` handlers over an on-disk temp state so
each contract is proven on the runtime path, not on a helper alone:

* A campaign created with ``budget_limits`` carries a spent-zero evidence
  budget; ``run_campaign`` charges every round against it and halts with the
  ``evidence_budget`` reason before a round that would exceed a limit.
* ``run_campaign`` reduces saturation through
  :func:`~eawf.kernel.spec.campaign_driver.ledger_saturation_reducer` over the
  carried claim ledger, so the four gates decide when a campaign is dry.
* An illegal Campaign status edge reached through the daemon is refused by
  the transition matrix, and converging a campaign leaves a scope Milestone
  untouched.
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
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedDispatch,
    stage_campaign,
)
from eawf.kernel.spec.saturation import SaturationReport
from eawf.kernel.state.enums import CampaignStatus, StoreKind
from eawf.kernel.state.epoch2 import Milestone, MilestoneStatus
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import IllegalCampaignTransitionError
from eawf.kernel.store.paths import ledger_path, store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import research as research_mod
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    cancel_campaign,
    create_campaign,
    override,
    read_latest_campaign,
    run,
    run_campaign,
    stage_campaign_method,
)

pytestmark = pytest.mark.unit

_CODE = "QR"

_MILESTONE_FIELDS: dict[str, Any] = {
    "uid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
    "key": "MLS-0030",
    "urn": f"eawf://WSP-{_CODE}/PRJ-{_CODE}/REP-{_CODE}/milestone/MLS-0030",
    "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
    "revision": 1,
    "created_at": "2026-09-08T00:00:00Z",
    "updated_at": "2026-09-08T00:00:00Z",
    "primary_track_ref": f"eawf://WSP-{_CODE}/PRJ-{_CODE}/REP-{_CODE}/track/TRK-RUNTIME",
    "title": "Publish an installable wheel",
    "outcome": "An operator installs the published wheel and the CLI answers.",
    "appetite": "M",
    "exclusions": ["platform packaging for Windows"],
    "acceptance_journey": [
        {
            "step_id": "AS-01",
            "actor": "operator",
            "action": "install the published wheel into a clean environment",
            "expected_observation": "the install completes and reports the version",
            "evidence_kinds": ["artifact"],
        }
    ],
    "status": "PLANNED",
}


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
            "domains": ["market-structure", "pricing-models"],
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


def _block() -> ResearchProfileBlock:
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _create_params(campaign_id: str, **budget_limits: float) -> dict[str, Any]:
    block = _block()
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": stage_campaign("options-pricing landscape", block).model_dump(mode="json"),
        "budget_limits": budget_limits,
    }


def _body(domain: str, finding: str) -> dict[str, object]:
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {domain}",
        "question": f"what does {domain} reveal",
        "findings": [finding],
        "recommendation": f"pursue {domain}",
        "evidence_refs": [{"kind": "store_record", "ref": f"src/{domain}.py:1"}],
    }


def _fresh_producer() -> Callable[[StagedDispatch], Mapping[str, object]]:
    """A producer whose every call returns a never-seen finding (never dry)."""
    calls: list[str] = []

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        calls.append(dispatch.domain)
        return _body(dispatch.domain, f"{dispatch.domain} finding {len(calls)}")

    return _produce


def _repeat_producer(dispatch: StagedDispatch) -> Mapping[str, object]:
    """The same finding every round, so round 2 adds no new claim."""
    return _body(dispatch.domain, f"{dispatch.domain} settles on one answer")


def _book_cost(state_path: Path, campaign_id: str, cost_usd: str) -> None:
    """Book one researcher ``dispatch_cost`` event against *campaign_id*."""
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id=f"EV-cost-{os.urandom(6).hex()}",
            kind=StoreKind.EVENT,
            scope_id=campaign_id,
            created_at=datetime.now(UTC),
            summary="dispatch cost",
            payload={"event_type": "dispatch_cost", "cost_usd": cost_usd},
        ),
    )


# --------------------------------------------------------------------------
# Evidence budget -- set at create, spent per round, halts before a breach
# --------------------------------------------------------------------------


def test_create_campaign_opens_budget_with_nothing_spent(tmp_path: Path) -> None:
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-open", rounds=3, usd=2.5))
        latest = read_latest_campaign(state_path, "rc-open")
        assert latest is not None
        budget = latest.evidence_budget
        assert {axis: (pair.unit, pair.limit, pair.spent) for axis, pair in budget.items()} == {
            "rounds": ("rounds", 3.0, 0.0),
            "usd": ("usd", 2.5, 0.0),
        }

    _run(body)


def test_stage_campaign_method_opens_budget(tmp_path: Path) -> None:
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        result = await stage_campaign_method(
            ctx,
            {
                "topic": "options-pricing landscape",
                "config": _block().model_dump(mode="json"),
                "campaign_id": "rc-staged",
                "budget_limits": {"rounds": 1},
            },
        )
        latest = read_latest_campaign(state_path, result["campaign_id"])
        assert latest is not None
        assert latest.evidence_budget["rounds"].limit == 1.0

    _run(body)


def test_create_campaign_rejects_unknown_budget_axis(tmp_path: Path) -> None:
    ctx, _state_path = _build_ctx(tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown evidence budget axis"):
            await create_campaign(ctx, _create_params("rc-bad", tokens=10))

    _run(body)


def test_create_campaign_rejects_negative_budget_limit(tmp_path: Path) -> None:
    ctx, _state_path = _build_ctx(tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            await create_campaign(ctx, _create_params("rc-neg", rounds=-1))

    _run(body)


def test_run_campaign_halts_on_rounds_budget_and_records_spend(tmp_path: Path) -> None:
    """Gate-fire proof: the rounds axis halts a never-dry run before round 3."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-rounds", rounds=2))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-rounds", round_budget=5),
            produce_agent_end=_fresh_producer(),
        )
        assert result["rounds_run"] == 2
        assert result["halt_reason"] == "evidence_budget"
        latest = read_latest_campaign(state_path, "rc-rounds")
        assert latest is not None
        assert latest.evidence_budget["rounds"].spent == 2.0
        # An exhausted budget ends the campaign rather than leaving it ACTIVE.
        assert latest.status is CampaignStatus.CONVERGED

    _run(body)


def test_run_campaign_halts_on_usd_budget_from_booked_cost(tmp_path: Path) -> None:
    """Each round's booked researcher spend is charged; the next round is projected."""
    ctx, state_path = _build_ctx(tmp_path)
    fresh = _fresh_producer()

    def _costly(dispatch: StagedDispatch) -> Mapping[str, object]:
        _book_cost(state_path, "rc-usd", "0.5")
        return fresh(dispatch)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-usd", usd=2.5))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-usd", round_budget=5),
            produce_agent_end=_costly,
        )
        # Two dispatches at 0.5 each cost 1.0 a round: after round 2 the 0.5
        # remainder cannot afford a third round projected at 1.0.
        assert result["rounds_run"] == 2
        assert result["halt_reason"] == "evidence_budget"
        latest = read_latest_campaign(state_path, "rc-usd")
        assert latest is not None
        assert latest.evidence_budget["usd"].spent == pytest.approx(2.0)

    _run(body)


def test_run_campaign_without_budget_runs_to_round_budget(tmp_path: Path) -> None:
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-free"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-free", round_budget=2),
            produce_agent_end=_fresh_producer(),
        )
        assert result["halt_reason"] == "round_budget"
        latest = read_latest_campaign(state_path, "rc-free")
        assert latest is not None and latest.evidence_budget == {}

    _run(body)


def test_run_campaign_refuses_budget_exhausted_before_first_round(tmp_path: Path) -> None:
    """The loop always runs one round, so an exhausted budget is refused up front."""
    ctx, _state_path = _build_ctx(tmp_path)
    spawned: list[str] = []

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        spawned.append(dispatch.domain)
        return _repeat_producer(dispatch)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-empty", rounds=0))
        with pytest.raises(ValueError, match="evidence budget exhausted"):
            run_campaign(
                ctx,
                RunCampaignParams(campaign_id="rc-empty", round_budget=3),
                produce_agent_end=_produce,
            )
        with pytest.raises(ValueError, match="evidence budget exhausted"):
            await run(ctx, {"campaign_id": "rc-empty"})
        assert spawned == []

    _run(body)


def test_run_campaign_pause_halts_between_rounds_and_stays_active(tmp_path: Path) -> None:
    """A blocking operator input yields the run to the operator, not a convergence."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-pause"))
        await override(ctx, {"verdict": "pin model A", "campaign_id": "rc-pause"})
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-pause", round_budget=4),
            produce_agent_end=_fresh_producer(),
        )
        assert result["rounds_run"] == 1
        assert result["halt_reason"] == "paused"
        latest = read_latest_campaign(state_path, "rc-pause")
        assert latest is not None and latest.status is CampaignStatus.ACTIVE

    _run(body)


# --------------------------------------------------------------------------
# Saturation -- the four-gate reducer over the carried claim ledger
# --------------------------------------------------------------------------


def test_run_campaign_reduces_saturation_through_ledger_reducer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof: saturation comes from the ledger reducer's four gates."""
    ctx, state_path = _build_ctx(tmp_path)
    reports: list[SaturationReport] = []
    real_reducer = research_mod.ledger_saturation_reducer

    def _spy(*args: Any, **kwargs: Any) -> Any:
        reduce = real_reducer(*args, **kwargs)

        def _recording(findings: Any) -> SaturationReport:
            report = reduce(findings)
            reports.append(report)
            return report

        return _recording

    monkeypatch.setattr(research_mod, "ledger_saturation_reducer", _spy)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-sat"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-sat", round_budget=5),
            produce_agent_end=_repeat_producer,
        )
        assert result["halt_reason"] == "saturated"
        assert result["rounds_run"] == 2
        # Round 1 logged two new claims, so novelty held the campaign open.
        assert list(reports[0].blocking_gates()) == ["novelty_decay"]
        assert reports[0].live_claim_count == 2
        assert reports[1].saturated is True
        assert [g.name for g in reports[1].gates] == [
            "no_open_question",
            "novelty_decay",
            "no_contradiction",
            "integration_closed",
        ]
        latest = read_latest_campaign(state_path, "rc-sat")
        assert latest is not None and latest.status is CampaignStatus.CONVERGED

    _run(body)


def test_run_campaign_open_question_holds_saturation_open(tmp_path: Path) -> None:
    """A blocking researcher question keeps gate (a) open, so the run never dries."""
    ctx, _state_path = _build_ctx(tmp_path)

    def _blocked(dispatch: StagedDispatch) -> Mapping[str, object]:
        body = _repeat_producer(dispatch)
        return {**body, "verdict": "blocked", "question": f"clarify {dispatch.domain}"}

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-open-q"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-open-q", round_budget=3),
            produce_agent_end=_blocked,
        )
        assert result["halt_reason"] == "round_budget"
        assert result["rounds_run"] == 3

    _run(body)


# --------------------------------------------------------------------------
# Campaign status edges -- the transition matrix gates the daemon writes
# --------------------------------------------------------------------------


def test_cancel_campaign_refuses_an_illegal_transition(tmp_path: Path) -> None:
    """Gate-fire proof: cancelling a converged campaign is refused by the matrix."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-done"))
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-done", round_budget=1),
            produce_agent_end=_repeat_producer,
        )
        with pytest.raises(IllegalCampaignTransitionError, match="'converged' -> 'cancelled'"):
            await cancel_campaign(ctx, {"campaign_id": "rc-done"})
        latest = read_latest_campaign(state_path, "rc-done")
        assert latest is not None and latest.status is CampaignStatus.CONVERGED

    _run(body)


def test_run_campaign_mid_run_cancel_is_not_converged(tmp_path: Path) -> None:
    """The run's convergence edge is refused for a campaign cancelled mid-run."""
    ctx, state_path = _build_ctx(tmp_path)

    def _cancel_during_round(dispatch: StagedDispatch) -> Mapping[str, object]:
        latest = read_latest_campaign(state_path, "rc-cancel")
        assert latest is not None
        if latest.status is CampaignStatus.ACTIVE:
            asyncio.run(cancel_campaign(ctx, {"campaign_id": "rc-cancel"}))
        return _repeat_producer(dispatch)

    # Driven outside an event loop so the in-round cancel can run its own.
    asyncio.run(create_campaign(ctx, _create_params("rc-cancel")))
    result = run_campaign(
        ctx,
        RunCampaignParams(campaign_id="rc-cancel", round_budget=1),
        produce_agent_end=_cancel_during_round,
    )
    assert result["halt_reason"] == "round_budget"
    latest = read_latest_campaign(state_path, "rc-cancel")
    assert latest is not None and latest.status is CampaignStatus.CANCELLED


def test_run_campaign_convergence_leaves_scope_milestone_unchanged(tmp_path: Path) -> None:
    """Converging a Campaign through the daemon never writes a scope Milestone."""
    ctx, state_path = _build_ctx(tmp_path)
    milestone = Milestone.model_validate(_MILESTONE_FIELDS)
    milestone_ledger = ledger_path(state_path, Epoch2Collection.MILESTONE)
    milestone_ledger.parent.mkdir(parents=True, exist_ok=True)
    milestone_ledger.write_text(f"{milestone.model_dump_json()}\n", encoding="utf-8")
    before = milestone_ledger.read_bytes()

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-milestone"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-milestone", round_budget=5),
            produce_agent_end=_repeat_producer,
        )
        assert result["halt_reason"] == "saturated"
        latest = read_latest_campaign(state_path, "rc-milestone")
        assert latest is not None and latest.status is CampaignStatus.CONVERGED
        assert milestone_ledger.read_bytes() == before
        reread = Milestone.model_validate_json(before.decode("utf-8").strip())
        assert reread.status is MilestoneStatus.PLANNED
        assert Epoch2Collection.MILESTONE.value not in orjson.loads(state_path.read_bytes())

    _run(body)
