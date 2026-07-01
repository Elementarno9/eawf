"""Tests for research.run + research.followup + research.snapshot (P30-I18-W03).

Covers the campaign-run RPC surface that drives the W01 round runner over a
persisted staged campaign:

* :func:`~eawf.runtime.daemon.methods.research.run_campaign` drives the bounded
  loop with a stubbed per-dispatch spawner, persists one round record per
  executed round, and respects the round budget + saturation halt.
* :func:`~eawf.runtime.daemon.methods.research.followup` reports the rounds run
  + the next round number.
* :func:`~eawf.runtime.daemon.methods.research.snapshot` folds the persisted
  campaign + rounds into a typed run summary.

The live ``agent.dispatch`` spawn is replaced by a fixture ``agent_end``
producer, mirroring how :mod:`tests.daemon.test_fleet_drive` injects a spawner;
no live campaign is run.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
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
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload, ResearcherReportBody
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods import research as research_mod
from eawf.runtime.daemon.methods.daemon import ping
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    create_campaign,
    followup,
    read_campaign_cost,
    read_campaign_rounds,
    run,
    run_campaign,
    snapshot,
)
from eawf.runtime.runtimes.adapter import SpawnResult

pytestmark = pytest.mark.unit

_LIVE_WAVE_ID = "P30-I20-W06"
_T0 = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 12, 12, 1, tzinfo=UTC)


def _build_ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        state_path=state_path,
    )


def _build_live_ctx(tmp_path: Path) -> tuple[MethodContext, Path]:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(_live_state_payload())
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    ctx = MethodContext(
        started_at="2026-06-12T00:00:00+00:00",
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


def _live_state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-12T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I20",
            "active_wave_ids": [_LIVE_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Live research campaign",
                "status": "active",
                "iter_ids": ["P30-I20"],
                "outcome_ids": [],
                "opened_at": "2026-06-12T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I20": {
                "id": "P30-I20",
                "phase_id": "P30",
                "title": "Research campaign live spawn",
                "status": "active",
                "wave_ids": [_LIVE_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-12T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _LIVE_WAVE_ID: {
                "id": _LIVE_WAVE_ID,
                "iter_id": "P30-I20",
                "title": "Wire live campaign producer",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/research.py"],
                "success_criteria": [],
                "agent_role": "researcher",
                "effort_bucket": "M",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-12T00:00:00Z",
                "claimed_at": "2026-06-12T00:00:00Z",
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


class _ResearchSpawnAdapter:
    id = "claude-code"
    cli_binary = "claude"

    def __init__(self) -> None:
        self.spawn_calls = 0
        self.prompts: list[str] = []

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> SpawnResult:
        self.spawn_calls += 1
        self.prompts.append(prompt)
        on_spawn = kwargs.get("on_spawn")
        if callable(on_spawn):
            on_spawn(43210 + self.spawn_calls)
        domain = "market-structure" if "market-structure" in prompt else "pricing-models"
        # Feed a live stdout line so the researcher chunk-stream wiring (W13)
        # persists an agent.output.chunk, mirroring the wave-spawn path.
        on_chunk = kwargs.get("on_chunk")
        if callable(on_chunk):
            await on_chunk(f"researching {domain}...")
        return SpawnResult(
            session_id=f"research-{self.spawn_calls}",
            runtime="claude-code",
            model=model,
            resolved_model=model,
            subprocess_pid=43210 + self.spawn_calls,
            exit_status=0,
            text=json.dumps(_agent_end_body(domain, findings=[f"{domain}-live-claim"])),
            input_tokens=100,
            output_tokens=25,
            cache_creation_input_tokens=0,
            cache_creation_5m_input_tokens=0,
            cache_creation_1h_input_tokens=0,
            cache_read_input_tokens=0,
            started_at=_T0,
            ended_at=_T1,
        )


def _read_envelopes(path: Path) -> list[Envelope]:
    if not path.exists():
        return []
    return [
        Envelope.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


async def _wait_for_research_run(campaign_id: str, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while research_mod.research_run_in_flight(campaign_id):
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for research run: {campaign_id!r}")
        await asyncio.sleep(0.02)


@pytest.fixture(autouse=True)
def _clear_background_research_runs() -> Any:
    """Ensure a failed background-run test cannot leak the module registry."""
    with research_mod._RESEARCH_RUN_LOCK:
        research_mod._ACTIVE_RESEARCH_RUNS.clear()
    yield
    with research_mod._RESEARCH_RUN_LOCK:
        research_mod._ACTIVE_RESEARCH_RUNS.clear()


def _block() -> ResearchProfileBlock:
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


def _agent_end_body(domain: str, *, findings: list[str]) -> dict[str, object]:
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {domain}",
        "question": f"what does {domain} reveal",
        "findings": findings,
        "recommendation": f"pursue {domain}",
        "evidence_refs": [{"kind": "store_record", "ref": f"src/{domain}.py:1"}],
    }


def _produce_findings(dispatch: StagedDispatch) -> Mapping[str, object]:
    """A stub producer: each dispatch returns one finding line (never dry)."""
    return _agent_end_body(dispatch.domain, findings=[f"{dispatch.domain}-claim"])


def _produce_empty(dispatch: StagedDispatch) -> Mapping[str, object]:
    """A stub producer that returns no findings -> the round saturates."""
    return _agent_end_body(dispatch.domain, findings=[])


# --------------------------------------------------------------------------
# run_campaign -- drives the bounded loop, persists each round
# --------------------------------------------------------------------------


def test_run_campaign_persists_each_round_to_budget(tmp_path: Path) -> None:
    """A never-dry run executes round_budget rounds and persists each one."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-run"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-run", round_budget=3),
            produce_agent_end=_produce_findings,
        )
        assert result["rounds_run"] == 3
        assert result["halt_reason"] == "round_budget"
        assert result["saturated"] is False
        # Two dispatches per round x 3 rounds = 6 claims.
        assert len(result["claim_ids"]) == 6
        rounds = read_campaign_rounds(state_path, "campaign-run")
        assert [r.round_number for r in rounds] == [1, 2, 3]
        assert rounds[0].finding_lines == ["market-structure-claim", "pricing-models-claim"]

    _run(body)


def test_run_campaign_flips_to_terminal_converged(tmp_path: Path) -> None:
    """A completed run flips the campaign ACTIVE -> CONVERGED, final round kept.

    W16: the campaign must reach a terminal status (never linger ACTIVE forever)
    on both the hard-cap and saturation halts, and the last round the loop ran
    is persisted (not dropped).
    """
    from eawf.kernel.state.enums import CampaignStatus
    from eawf.runtime.daemon.methods.research import read_latest_campaign

    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        # Hard round-cap halt.
        await create_campaign(ctx, _stage_params("campaign-cap"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-cap", round_budget=2),
            produce_agent_end=_produce_findings,
        )
        assert result["rounds_run"] == 2
        cap = read_latest_campaign(state_path, "campaign-cap")
        assert cap is not None and cap.status is CampaignStatus.CONVERGED
        # The final (2nd) round is persisted, not dropped.
        assert [r.round_number for r in read_campaign_rounds(state_path, "campaign-cap")] == [1, 2]

        # Saturation halt also converges.
        await create_campaign(ctx, _stage_params("campaign-sat"))
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-sat", round_budget=5),
            produce_agent_end=_produce_empty,
        )
        sat = read_latest_campaign(state_path, "campaign-sat")
        assert sat is not None and sat.status is CampaignStatus.CONVERGED

    _run(body)


def test_run_campaign_halts_on_saturation(tmp_path: Path) -> None:
    """A round returning no findings saturates and halts the loop early."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-dry"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-dry", round_budget=5),
            produce_agent_end=_produce_empty,
        )
        assert result["rounds_run"] == 1
        assert result["halt_reason"] == "saturated"
        assert result["saturated"] is True
        rounds = read_campaign_rounds(state_path, "campaign-dry")
        assert len(rounds) == 1
        assert rounds[0].saturated is True

    _run(body)


def test_research_run_rpc_uses_live_producer_without_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RPC entrypoint wires the live producer and records smoke evidence."""
    ctx, state_path = _build_live_ctx(tmp_path)
    adapter = _ResearchSpawnAdapter()
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.research.select_adapter",
        lambda _runtime: adapter,
    )

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-live"))
        result = await run(ctx, {"campaign_id": "campaign-live", "round_budget": 1})
        assert result["campaign_id"] == "campaign-live"
        assert result["run_state"] == "running"
        assert result["backgrounded"] is True
        assert result["handle_id"].startswith("research-run-")
        await _wait_for_research_run("campaign-live")
        rounds = read_campaign_rounds(state_path, "campaign-live")
        assert len(rounds) == 1
        assert rounds[0].claim_ids == ["CLM-r1-market-structure-0", "CLM-r1-pricing-models-0"]

    _run(body)

    assert adapter.spawn_calls == 2
    schema_hint = "Return only a JSON object matching this agent_end schema"
    assert all(schema_hint in p for p in adapter.prompts)

    reports = _read_envelopes(store_path(state_path, StoreKind.RESEARCHER_REPORT))
    assert len(reports) == 2
    payloads = [AgentReportPayload.model_validate(row.payload) for row in reports]
    assert all(payload.header.role is AgentSessionRole.RESEARCHER for payload in payloads)
    assert all(isinstance(payload.body, ResearcherReportBody) for payload in payloads)

    events = _read_envelopes(store_path(state_path, StoreKind.EVENT))
    event_types = [row.payload.get("event_type") for row in events]
    assert "research.run.round" in event_types
    assert "dispatch_cost" in event_types
    # W13: a Feed lifecycle marker per researcher spawn + finish, and the
    # researcher stdout streams live as agent.output.chunk rows.
    assert "research.researcher.spawn" in event_types
    assert "research.researcher.finish" in event_types
    assert "agent.output.chunk" in event_types
    # W15: even with an active execution wave in scope, researcher spend books
    # to the CAMPAIGN cost centre (queryable), not the wave -- every
    # dispatch_cost row scopes to the campaign, and the active wave's counters
    # stay executor-only (untouched by research).
    cost_rows = [row for row in events if row.payload.get("event_type") == "dispatch_cost"]
    assert cost_rows
    assert all(row.scope_id == "campaign-live" for row in cost_rows)
    assert read_campaign_cost(state_path, "campaign-live") > 0
    final_state = State.model_validate(orjson.loads(state_path.read_bytes()))
    active_wave = final_state.waves[_LIVE_WAVE_ID]
    assert active_wave.tokens_consumed == 0  # research never inflated the wave
    # W17: every researcher session reached CLOSED and none leaked as a phantom
    # ACTIVE session in current.active_session_ids.
    researcher_sessions = [
        s for s in final_state.agent_sessions.values() if s.role is AgentSessionRole.RESEARCHER
    ]
    assert researcher_sessions
    assert all(s.status is AgentSessionStatus.CLOSED for s in researcher_sessions)
    assert all(s.ended_at is not None for s in researcher_sessions)
    assert not any(
        final_state.agent_sessions[sid].role is AgentSessionRole.RESEARCHER
        for sid in final_state.current.active_session_ids
        if sid in final_state.agent_sessions
    )


def test_research_run_runs_with_no_active_wave_scoped_to_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """research.run succeeds with NO active execution wave, scoped to the campaign.

    W14: a campaign is project-scoped, so the live run must not require (or
    pollute) an execution wave. With the active-wave list + iter waves cleared,
    the run still executes and the researcher dispatch cost / output anchor to
    the campaign id, never a wave.
    """
    ctx, state_path = _build_live_ctx(tmp_path)
    # Strip every execution wave so no active wave is resolvable.
    state = State.model_validate(orjson.loads(state_path.read_bytes()))
    state.current.active_wave_ids = []
    state.waves = {}
    for it in state.iters.values():
        it.wave_ids = []
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    adapter = _ResearchSpawnAdapter()
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.research.select_adapter",
        lambda _runtime: adapter,
    )

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-nowave"))
        result = await run(ctx, {"campaign_id": "campaign-nowave", "round_budget": 1})
        assert result["run_state"] == "running"
        await _wait_for_research_run("campaign-nowave")
        rounds = read_campaign_rounds(state_path, "campaign-nowave")
        assert len(rounds) == 1  # the run executed despite no active wave

    _run(body)

    # The dispatch cost anchors to the campaign, never a wave id.
    events = _read_envelopes(store_path(state_path, StoreKind.EVENT))
    cost_rows = [row for row in events if row.payload.get("event_type") == "dispatch_cost"]
    assert cost_rows
    assert all("campaign-nowave" in row.scope_id for row in cost_rows)


def test_research_run_ping_and_steer_answer_while_background_run_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """research.run returns a handle while ping + steer answer mid-run."""
    ctx, state_path = _build_live_ctx(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def _gated_producer(dispatch: StagedDispatch) -> Mapping[str, object]:
        calls.append(dispatch.domain)
        started.set()
        release.wait(timeout=5.0)
        return _agent_end_body(dispatch.domain, findings=[f"{dispatch.domain}-background-claim"])

    monkeypatch.setattr(
        research_mod,
        "_live_agent_end_producer",
        lambda _ctx, runtime, *, campaign_id: _gated_producer,
    )

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-bg"))
        handle = await run(ctx, {"campaign_id": "campaign-bg", "round_budget": 1})
        assert handle["campaign_id"] == "campaign-bg"
        assert handle["run_state"] == "running"
        assert handle["backgrounded"] is True
        assert await asyncio.to_thread(started.wait, 1.0)
        assert research_mod.research_run_in_flight("campaign-bg") is True

        pong = await asyncio.wait_for(ping(ctx, {}), timeout=1.0)
        steer_result = await asyncio.wait_for(
            research_mod.steer(
                ctx,
                {"text": "narrow to exchange microstructure", "campaign_id": "campaign-bg"},
            ),
            timeout=1.0,
        )

        assert pong["pid"] == ctx.pid
        assert steer_result["kind"] == "steer"
        release.set()
        await _wait_for_research_run("campaign-bg")

    try:
        _run(body)
    finally:
        release.set()

    assert calls == ["market-structure", "pricing-models"]
    rounds = read_campaign_rounds(state_path, "campaign-bg")
    assert len(rounds) == 1
    assert rounds[0].finding_lines == [
        "market-structure-background-claim",
        "pricing-models-background-claim",
    ]
    assert any("narrow to exchange microstructure" in note for note in rounds[0].steer_notes)


def test_run_campaign_rejects_unknown_campaign(tmp_path: Path) -> None:
    """Running an unstaged campaign id is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)
    with pytest.raises(ValueError, match="unknown campaign"):
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-ghost"),
            produce_agent_end=_produce_findings,
        )


def test_run_campaign_raises_without_state_path() -> None:
    """run_campaign raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="state_path not configured"):
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="x"),
            produce_agent_end=_produce_findings,
        )


# --------------------------------------------------------------------------
# research.followup -- reports rounds run + next round
# --------------------------------------------------------------------------


def test_followup_reports_next_round(tmp_path: Path) -> None:
    """Follow-up names the round a follow-up run would start at."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-fu"))
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-fu", round_budget=2),
            produce_agent_end=_produce_findings,
        )
        result = await followup(ctx, {"campaign_id": "campaign-fu", "note": "dig deeper"})
        assert result["rounds_run"] == 2
        assert result["next_round"] == 3
        assert result["note"] == "dig deeper"

    _run(body)


def test_followup_rejects_unknown_campaign(tmp_path: Path) -> None:
    """Follow-up on an unknown campaign is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown campaign"):
            await followup(ctx, {"campaign_id": "campaign-missing"})

    _run(body)


# --------------------------------------------------------------------------
# research.snapshot -- typed run summary
# --------------------------------------------------------------------------


def test_snapshot_summarises_run(tmp_path: Path) -> None:
    """Snapshot folds the campaign + rounds into a typed run summary."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-snap"))
        run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-snap", round_budget=2),
            produce_agent_end=_produce_findings,
        )
        result = await snapshot(ctx, {"campaign_id": "campaign-snap"})
        assert result["campaign_id"] == "campaign-snap"
        # W16: the completed run flipped the campaign to its terminal state.
        assert result["status"] == "converged"
        assert result["topic"] == "options-pricing landscape"
        assert result["rounds_run"] == 2
        assert result["total_findings"] == 4  # 2 dispatches x 2 rounds
        assert result["total_claims"] == 4
        assert result["checkpoints"] >= 1  # ON_HALT records the terminal round

    _run(body)


def test_snapshot_pre_run_reports_zero_rounds(tmp_path: Path) -> None:
    """A staged-but-not-run campaign snapshots zero rounds."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-staged"))
        result = await snapshot(ctx, {"campaign_id": "campaign-staged"})
        assert result["rounds_run"] == 0
        assert result["saturated"] is False
        assert result["total_findings"] == 0

    _run(body)


def test_snapshot_rejects_unknown_campaign(tmp_path: Path) -> None:
    """Snapshot on an unknown campaign is rejected."""
    state_path = tmp_path / "state.json"
    ctx = _build_ctx(state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown campaign"):
            await snapshot(ctx, {"campaign_id": "campaign-nope"})

    _run(body)
