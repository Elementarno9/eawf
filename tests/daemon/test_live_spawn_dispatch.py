"""Tests: live wave-executor spawn + AgentSession registration un-idling the report.

Exercises the opt-in live-spawn path of
:func:`eawf.runtime.daemon.methods.agent.dispatch` (``spawn=True``): the daemon
registers an executor :class:`~eawf.kernel.state.models.AgentSession`, renders
the prompt, resolves the runtime adapter, ``await``s its ``spawn_session``
(behind the safety floor), prices the spawn, and drives
:func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` with the registered
session id so the previously-idle ``emit_agent_end_report`` path fires.

The adapter ``spawn_session`` is ALWAYS a monkeypatched stub returning a
canned :class:`~eawf.runtime.runtimes.adapter.SpawnResult` -- no real ``claude``
subprocess, no network, no auth, no cost. The stub fires the ``on_spawn``
callback with a fixed pid so the captured-pid assertion is observable.

The load-bearing assertions, one per wave success criterion:

- an EXECUTOR ``AgentSession`` is registered in ``state.agent_sessions``
  after a live dispatch (ACTIVE, scope = wave_id, role executor);
- ``run_dispatch`` received the registered session id and the
  ``agent_end`` emit fired -- a role-specific executor report envelope
  lands in the ``executor_report`` store;
- the returned plan carries the real captured pid (non-zero) and the
  priced cost flowed into the ``dispatch_cost`` event;
- back-compat: ``spawn=False`` (plan-only + hand-fed-outcome) is unchanged;
- error path: ``spawn=True`` without ``state_path`` fails fast typed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import AgentSession
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload, store_kind_for_role
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.pricing import lookup_pricing
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.dispatch_runner import AGENT_OUTPUT_EVENT_TYPE
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.agent import (
    LiveSpawnError,
    _claim_live_session,
    _persist_live_session_attempt,
    dispatch,
    kill,
)
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.runtimes.cancel import CancelResult
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P29-I04-W01"
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 54321


def _executor_report_json(*, wave_id: str = _WAVE_ID, verdict: str = "pass") -> str:
    """A valid ``ExecutorReportBody`` JSON string the stub spawn returns.

    The live-spawn report path now binds the spawned agent's OWN text to a
    validated :class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody`
    via the schema-assist re-ask loop, so the stub must emit schema-valid
    JSON (not free prose). The ``wave_id`` matches the dispatched wave so the
    post-execution verify gate passes.
    """
    return json.dumps(
        {
            "role": "executor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "executor implemented the wave",
            "wave_id": wave_id,
            "files_changed": ["src/eawf/runtime/daemon/methods/agent.py"],
            "tests_run": ["uv run pytest tests/daemon -q"],
            "commit_sha": "abc1234",
            "outcome": "bound the spawned executor output to the report body",
        }
    )


def _role_report_json(role: AgentSessionRole) -> str:
    """Return one valid authored report body for a specialist *role*."""
    common: dict[str, Any] = {
        "role": role.value,
        "verdict": "pass",
        "confidence": "high",
        "summary": f"{role.value} completed the assigned wave",
        "evidence_refs": [
            {
                "kind": "artifact",
                "ref": "tests/daemon/test_live_spawn_dispatch.py:1",
                "note": "CR-01",
            }
        ],
        "followups": [],
    }
    role_fields: dict[AgentSessionRole, dict[str, Any]] = {
        AgentSessionRole.RESEARCHER: {
            "question": "What evidence resolves this wave?",
            "findings": ["the dispatch report path is role-correct"],
            "alternatives": [],
            "recommendation": "retain the role-matched report binding",
        },
        AgentSessionRole.PLANNER: {
            "objective": "plan the dispatched wave",
            "waves": [],
            "risks": [],
        },
        AgentSessionRole.AUDITOR: {
            "target_id": _WAVE_ID,
            "criteria": [
                {
                    "criterion": "CR-01",
                    "passed": True,
                    "evidence_refs": common["evidence_refs"],
                }
            ],
            "refutations": [],
        },
        AgentSessionRole.REVIEWER: {
            "target_id": _WAVE_ID,
            "findings": [],
            "coverage_refs": common["evidence_refs"],
        },
        AgentSessionRole.POLISHER: {
            "scope_id": _WAVE_ID,
            "changes": [],
            "deferred_items": [],
        },
        AgentSessionRole.OPERATOR: {
            "phase_id": "P29",
            "completed_wave_ids": [_WAVE_ID],
            "decisions": [],
            "next_actions": [],
        },
        AgentSessionRole.DOMAIN_SPECIALIST: {
            "domain": "workflow",
            "assessment": "the role-specific dispatch contract is satisfied",
            "recommendations": [],
        },
    }
    common.update(role_fields[role])
    return json.dumps(common)


def _authored_report_json(role: AgentSessionRole) -> str:
    """Return a valid report body for any live-dispatch role."""
    if role is AgentSessionRole.EXECUTOR:
        return _executor_report_json()
    return _role_report_json(role)


# --------------------------------------------------------------------------- #
# Fixtures: a stub adapter spawn + a valid on-disk state with the wave chain.
# --------------------------------------------------------------------------- #


class _StubAdapter:
    """A RuntimeAdapter stand-in whose spawn_session never forks a process.

    Returns a canned :class:`SpawnResult` with a representative token spread
    so the priced cost is non-zero, and fires the ``on_spawn`` callback with
    a fixed pid so the captured-pid path is observable. Records the prompt +
    model it was handed so a test can assert the rendered prompt flowed in.
    """

    id = "claude-code"
    cli_binary = "claude"

    def __init__(
        self,
        *,
        report_text: str | None = None,
        report_texts: Sequence[str] | None = None,
    ) -> None:
        self.spawn_calls = 0
        self.prompts: list[str] = []
        self.models: list[str] = []
        self.report_texts = list(report_texts or [report_text or _executor_report_json()])

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        timeout: float | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> SpawnResult:
        self.spawn_calls += 1
        self.prompts.append(prompt)
        self.models.append(model)
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        text = self.report_texts[min(self.spawn_calls - 1, len(self.report_texts) - 1)]
        if on_chunk is not None:
            # Mirror the real adapter's live-streaming seam: fan each stdout line
            # to the chunk callback as it "arrives" so the W45 chunk producer is
            # exercised end to end (the lines carry their newline like the adapter).
            for line in text.splitlines(keepends=True):
                await on_chunk(line)
        return SpawnResult(
            session_id="sess-live-abc123",
            runtime="claude-code",
            model=model,
            resolved_model="claude-opus-4-8",
            subprocess_pid=_STUB_PID,
            exit_status=0,
            text=text,
            input_tokens=100,
            output_tokens=42,
            cache_creation_input_tokens=80,
            cache_creation_5m_input_tokens=50,
            cache_creation_1h_input_tokens=30,
            cache_read_input_tokens=200,
            started_at=_T0,
            ended_at=_T1,
        )

    def session_log_handle(self, session_id: str) -> str:
        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"


def _state_payload(
    *,
    agent_role: str = "executor",
    effort_bucket: str | None = "L",
    token_budget: int | None = None,
    wave_status: str = "pending",
) -> dict[str, Any]:
    """A minimal valid State with the full phase -> iter -> wave chain.

    The chain is required because the live path renders the dispatch
    envelope, which walks wave -> iter -> phase -> scope. The wave starts
    PENDING so live dispatch creates and binds its session atomically before
    the runner's head transition flips it to IN_PROGRESS. ``agent_sessions``
    starts empty so the live path registers the executor session itself.
    ``wave_status`` overrides the wave's status (e.g.
    ``"closed"`` to model close-on-behalf having already closed the wave).
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-01T00:00:00Z",
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
            "phase_id": "P29",
            "iter_id": "P29-I04",
            "active_wave_ids": ([_WAVE_ID] if wave_status in {"claimed", "in_progress"} else []),
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "EAWF",
                "title": "v0.5",
                "status": "active",
                "iter_ids": ["P29-I04"],
                "outcome_ids": [],
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I04": {
                "id": "P29-I04",
                "phase_id": "P29",
                "title": "Spawn spine",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P29-I04",
                "title": "Live wave-executor spawn",
                "status": wave_status,
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/agent.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "spawn for real behind the floor",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "spawn for real behind the floor",
                    }
                ],
                "agent_role": agent_role,
                "effort_bucket": effort_bucket,
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": token_budget,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "claimed_at": (
                    "2026-06-01T00:00:00Z" if wave_status in {"claimed", "in_progress"} else None
                ),
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, **kwargs: Any) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    from eawf.kernel.state.models import State

    state = State.model_validate(_state_payload(**kwargs))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path | None, *, event_path: Path | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-06-01T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _StubAdapter) -> None:
    """Make the live path resolve to *adapter* instead of the real claude one."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.agent.select_adapter",
        lambda runtime_id: adapter,
    )


def _read_envelopes(path: Path) -> list[Envelope]:
    rows: list[Envelope] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Envelope.model_validate_json(line))
    return rows


def _executor_report_rows(state_path: Path) -> list[Envelope]:
    return _read_envelopes(store_path(state_path, StoreKind.EXECUTOR_REPORT))


def _dispatch_cost_payloads(event_path: Path) -> list[dict[str, Any]]:
    return [
        env.payload
        for env in _read_envelopes(event_path)
        if env.payload.get("event_type") == "dispatch_cost"
    ]


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Criterion (a) + (b): live spawn registers an EXECUTOR session, threads the
# session id into run_dispatch, and the agent_end report emit fires.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_registers_executor_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live dispatch registers an ACTIVE EXECUTOR session scoped to the wave."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    state = load_state(state_path)
    # Exactly one session was registered; it is the executor lane for the wave.
    assert len(state.agent_sessions) == 1
    session = next(iter(state.agent_sessions.values()))
    assert session.role is AgentSessionRole.EXECUTOR
    assert session.scope_id == _WAVE_ID
    assert session.status is AgentSessionStatus.ACTIVE
    assert session.claimed_wave_ids == [_WAVE_ID]
    # The plan's session id is the registered AgentSession id (not a cosmetic UUID).
    assert result["session_id"] == session.id
    assert session.id in state.current.active_session_ids
    assert state.waves[_WAVE_ID].claim_session_id == session.id
    # The adapter spawn was driven exactly once.
    assert adapter.spawn_calls == 1
    events = _read_envelopes(event_path)
    assert [event.payload["event_type"] for event in events[:2]] == [
        "session.start",
        "wave.claim",
    ]
    assert events[1].payload["actor"] == session.id


def test_dispatch_claim_failure_leaves_no_session_wave_or_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection after staging a session rolls the whole live claim back."""
    state_path = _write_state(tmp_path, effort_bucket=None)
    before = state_path.read_bytes()
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(DaemonValidationError, match="has no effort_bucket"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert state_path.read_bytes() == before
    assert not event_path.exists()
    assert adapter.spawn_calls == 0


def test_dispatch_unknown_runtime_rejects_before_session_claim_or_event(tmp_path: Path) -> None:
    """Adapter preflight rejects a bogus runtime with no durable orphan."""
    state_path = _write_state(tmp_path)
    before = state_path.read_bytes()
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(ValueError, match="unknown runtime"):
        _run(
            dispatch(
                ctx,
                {"wave_id": _WAVE_ID, "runtime": "bogus-runtime", "spawn": True},
            )
        )

    assert state_path.read_bytes() == before
    assert not event_path.exists()


def test_dispatch_model_resolution_rejects_before_session_claim_or_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model preflight failure leaves state/events byte-identical and never spawns."""
    state_path = _write_state(tmp_path)
    before = state_path.read_bytes()
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)

    def _fail_model(*args: Any, **kwargs: Any) -> str:
        raise ValueError("model resolution failed")

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.agent._resolve_spawn_model",
        _fail_model,
    )
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(ValueError, match="model resolution failed"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert state_path.read_bytes() == before
    assert not event_path.exists()
    assert adapter.spawn_calls == 0


def test_live_claim_transaction_uses_specialist_wave_role(tmp_path: Path) -> None:
    """Canonical transaction creates AUDITOR, never unconditional EXECUTOR."""
    state_path = _write_state(tmp_path, agent_role="auditor")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    ctx = _ctx(state_path, event_path=event_path)

    binding = _claim_live_session(
        ctx,
        wave_id=_WAVE_ID,
        runtime="claude-code",
        out_of_order=False,
    )

    state = load_state(state_path)
    session = state.agent_sessions[binding.session_id]
    assert binding.role is AgentSessionRole.AUDITOR
    assert session.role is AgentSessionRole.AUDITOR
    assert session.claimed_wave_ids == [_WAVE_ID]
    assert state.waves[_WAVE_ID].claim_session_id == session.id


@pytest.mark.parametrize(
    "role",
    [
        AgentSessionRole.RESEARCHER,
        AgentSessionRole.PLANNER,
        AgentSessionRole.AUDITOR,
        AgentSessionRole.REVIEWER,
        AgentSessionRole.POLISHER,
        AgentSessionRole.OPERATOR,
        AgentSessionRole.DOMAIN_SPECIALIST,
    ],
)
def test_dispatch_spawn_emits_role_correct_specialist_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: AgentSessionRole,
) -> None:
    """Full specialist dispatch binds and emits under its session role."""
    state_path = _write_state(tmp_path, agent_role=role.value)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter(report_text=_role_report_json(role))
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    state = load_state(state_path)
    session = state.agent_sessions[result["session_id"]]
    assert session.role is role
    assert state.waves[_WAVE_ID].claim_session_id == session.id

    report_path = store_path(state_path, store_kind_for_role(role))
    rows = _read_envelopes(report_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    assert payload.header.role is role
    assert payload.header.session_id == session.id
    assert payload.body.role == role.value
    assert payload.body.verdict.value == "pass"
    assert _executor_report_rows(state_path) == []

    assert adapter.spawn_calls == 1
    assert "## Report output" in adapter.prompts[0]
    assert role.value in adapter.prompts[0]
    assert f"typed {role.value} report" in adapter.prompts[0]


@pytest.mark.parametrize(
    ("role", "invalid_field", "invalid_value", "violation_code"),
    [
        (
            AgentSessionRole.EXECUTOR,
            "commit_sha",
            None,
            "INV.AGENT_REPORT.EXECUTOR_COMMIT_MISSING",
        ),
        (
            AgentSessionRole.AUDITOR,
            "criteria",
            [],
            "INV.AGENT_REPORT.AUDITOR_CRITERIA_MISSING",
        ),
        (
            AgentSessionRole.REVIEWER,
            "coverage_refs",
            [],
            "INV.AGENT_REPORT.REVIEWER_COVERAGE_MISSING",
        ),
        (
            AgentSessionRole.OPERATOR,
            "phase_id",
            _WAVE_ID,
            "INV.AGENT_REPORT.OPERATOR_PHASE_MISSING",
        ),
    ],
)
def test_dispatch_reasks_canonical_invariant_failure_before_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: AgentSessionRole,
    invalid_field: str,
    invalid_value: object,
    violation_code: str,
) -> None:
    """Schema-valid but invariant-invalid role bodies are never persisted."""
    state_path = _write_state(tmp_path, agent_role=role.value)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    invalid = json.loads(_authored_report_json(role))
    invalid[invalid_field] = invalid_value
    adapter = _StubAdapter(
        report_texts=[
            json.dumps(invalid),
            _authored_report_json(role),
        ]
    )
    _patch_adapter(monkeypatch, adapter)

    _run(dispatch(_ctx(state_path, event_path=event_path), {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.spawn_calls == 2
    assert "Output correction required" in adapter.prompts[1]
    assert violation_code in adapter.prompts[1]
    rows = _read_envelopes(store_path(state_path, store_kind_for_role(role)))
    assert len(rows) == 1
    body = AgentReportPayload.model_validate(rows[0].payload).body
    assert body.role == role.value
    assert body.report_source.value == "authored"


def test_dispatch_operator_override_uses_operator_prompt_validator_and_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator session over an auditor wave stays operator end to end."""
    state_path = _write_state(tmp_path, agent_role="auditor", wave_status="claimed")
    state = load_state(state_path)
    session_id = "SES-operator-override"
    state.agent_sessions[session_id] = AgentSession(
        id=session_id,
        role=AgentSessionRole.OPERATOR,
        runtime="claude-code",
        scope_id=_WAVE_ID,
        status=AgentSessionStatus.ACTIVE,
        claimed_wave_ids=[_WAVE_ID],
        started_at=_T0,
    )
    state.current.active_session_ids.append(session_id)
    state.waves[_WAVE_ID].claim_session_id = session_id
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter(report_text=_role_report_json(AgentSessionRole.OPERATOR))
    _patch_adapter(monkeypatch, adapter)

    result: dict[str, Any] = _run(
        dispatch(_ctx(state_path, event_path=event_path), {"wave_id": _WAVE_ID, "spawn": True})
    )

    assert result["session_id"] == session_id
    assert adapter.spawn_calls == 1
    prompt = adapter.prompts[0]
    assert "- agent_role: operator" in prompt
    assert "- role: operator" in prompt
    assert "typed operator report" in prompt
    assert "parent phase `P29`" in prompt
    assert "typed auditor report" not in prompt

    report_path = store_path(state_path, StoreKind.OPERATOR_REPORT)
    rows = _read_envelopes(report_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    assert payload.header.role is AgentSessionRole.OPERATOR
    assert payload.body.role == "operator"
    assert payload.body.phase_id == "P29"
    assert payload.body.report_source.value == "authored"
    assert not store_path(state_path, StoreKind.AUDITOR_REPORT).exists()


def test_dispatch_claimed_wave_without_bound_session_rejects_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical CLAIMED row missing its binding cannot gain a new session."""
    state_path = _write_state(tmp_path, wave_status="claimed")
    before = state_path.read_bytes()
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(DaemonValidationError, match="claim_session_not_found"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert state_path.read_bytes() == before
    assert not event_path.exists()
    assert adapter.spawn_calls == 0


def test_dispatch_claimed_wave_with_stale_bound_session_rejects_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live redispatch refuses a stale session already bound to the wave."""
    state_path = _write_state(tmp_path, wave_status="claimed")
    state = load_state(state_path)
    session_id = "SES-stale-bound"
    state.agent_sessions[session_id] = AgentSession(
        id=session_id,
        role=AgentSessionRole.EXECUTOR,
        runtime="claude-code",
        scope_id=_WAVE_ID,
        status=AgentSessionStatus.STALE,
        claimed_wave_ids=[_WAVE_ID],
        started_at=_T0,
        ended_at=_T1,
    )
    state.waves[_WAVE_ID].claim_session_id = session_id
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    before = state_path.read_bytes()
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(DaemonValidationError, match="claim_session_not_active"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert state_path.read_bytes() == before
    assert not event_path.exists()
    assert adapter.spawn_calls == 0


def test_dispatch_spawn_emits_executor_report_unidling_run_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threaded session id un-idles emit_agent_end_report: a report row lands.

    This is the keystone assertion -- before W01 ``run_dispatch`` was never
    handed a ``session_id``, so ``emit_agent_end_report`` never fired. The
    live path registers a session and threads its id, so a role-specific
    executor report envelope lands in the executor_report store.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    # The report is the executor body for the dispatched wave, scoped to it.
    assert payload.header.role is AgentSessionRole.EXECUTOR
    assert payload.header.scope_id == _WAVE_ID
    assert payload.body.role == "executor"
    assert payload.body.wave_id == _WAVE_ID
    # A clean (no-fallback) dispatch reports a pass verdict.
    assert payload.body.verdict.value == "pass"


def test_dispatch_spawn_report_session_id_matches_registered_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted report's header session id is the registered session id."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    payload = AgentReportPayload.model_validate(_executor_report_rows(state_path)[0].payload)
    assert payload.header.session_id == result["session_id"]


# --------------------------------------------------------------------------- #
# Criterion (c): real captured pid + priced cost flow through.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_plan_carries_captured_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The returned plan carries the real (non-zero) pid captured via on_spawn."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert result["pid"] == _STUB_PID
    assert result["pid"] != 0


def test_dispatch_spawn_persists_session_attempt_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live dispatch persists the captured pid on Wave.sessions."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    wave = load_state(state_path).waves[_WAVE_ID]
    attempt = wave.sessions[result["attempt"]]
    assert attempt.subprocess_pid == _STUB_PID
    assert attempt.session_id == "sess-live-abc123"
    assert attempt.session_id != result["session_id"]
    assert result["session_attempt"]["subprocess_pid"] == _STUB_PID
    assert result["session_attempt"]["session_id"] == "sess-live-abc123"
    assert wave.dispatch_history[-1].attempt == result["attempt"]


def test_dispatch_then_kill_uses_persisted_session_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-fleet live dispatch can be killed through its SessionAttempt pid."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)
    sent: list[tuple[int, bool]] = []

    def _fake_cancel(pgid: int, *, hard: bool = False) -> CancelResult:
        sent.append((pgid, hard))
        return CancelResult(pgid=pgid, signal_sent=9 if hard else 15, delivered=True)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    monkeypatch.setattr("eawf.runtime.daemon.methods.fleet.cancel_process_group", _fake_cancel)

    result: dict[str, Any] = _run(kill(ctx, {"wave_id": _WAVE_ID, "attempt": 1, "signal": "kill"}))

    assert result == {"killed": True, "signal": "kill", "reason": None}
    assert sent == [(_STUB_PID, True)]


def test_dispatch_spawn_priced_cost_flows_into_dispatch_cost_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn is priced and the real cost lands on the dispatch_cost event.

    The stub returns a non-zero token spread; the metering writer prices it
    against ``claude-opus-4-8``, and that exact Decimal cost rides on the
    emitted ``dispatch_cost`` payload rather than a ``$0`` placeholder.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    pricing = lookup_pricing("claude-opus-4-8")
    assert pricing is not None
    expected = (
        100 * pricing.input_per_token
        + 42 * pricing.output_per_token
        + 50 * pricing.cache_write_5m_per_token
        + 30 * pricing.cache_write_1h_per_token
        + 200 * pricing.cache_read_per_token
    )
    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert Decimal(costs[0]["cost_usd"]) == expected
    assert Decimal(costs[0]["cost_usd"]) > Decimal("0")
    assert costs[0]["runtime"] == "claude"
    assert costs[0]["wave_id"] == _WAVE_ID


def test_dispatch_spawn_threads_pgid_and_config_enforce_to_budget_interlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard over-budget live spawn reaches the interlock with the live pgid."""
    state_path = _write_state(tmp_path, token_budget=1)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    config_path = tmp_path / ".ea" / "config.yaml"
    config_path.write_text(
        "schema_version: '1.0'\nflow:\n  budget:\n    enforce: hard\n",
        encoding="utf-8",
    )
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)
    calls: list[dict[str, Any]] = []

    def _fake_enforce_token_cap(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(terminated=True)

    monkeypatch.setattr(
        "eawf.runtime.daemon.dispatch_runner.enforce_token_cap",
        _fake_enforce_token_cap,
    )

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert len(calls) == 1
    assert calls[0]["pgid"] == _STUB_PID
    assert calls[0]["enforce"] == "hard"
    assert calls[0]["base_budget"] == 1
    assert calls[0]["consumed"] > calls[0]["base_budget"]


def test_dispatch_spawn_event_ids_reflect_runner_emissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's event_ids are the runner's emitted ids (non-empty on live)."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # No fallback in the live W01 path -> two events: the C09 dispatch_cost AND
    # the W08 agent.output fan (the live spawn now threads its captured answer
    # text into run_dispatch(output_text=...), so the stdout producer fires).
    assert len(result["event_ids"]) == 2
    envelopes = _read_envelopes(event_path)
    cost_envelope_ids = [
        env.id for env in envelopes if env.payload.get("event_type") == "dispatch_cost"
    ]
    output_envelope_ids = [
        env.id for env in envelopes if env.payload.get("event_type") == AGENT_OUTPUT_EVENT_TYPE
    ]
    assert len(cost_envelope_ids) == 1
    assert len(output_envelope_ids) == 1
    # The plan's event_ids ARE the runner's emitted ids: the dispatch_cost then
    # the agent.output (emitted in that order).
    assert list(result["event_ids"]) == cost_envelope_ids + output_envelope_ids


def test_dispatch_spawn_emits_ordered_chunk_events_and_terminal_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W45: a live spawn that streams stdout persists ordered agent.output.chunk rows.

    The stub adapter fans its captured text to the ``on_chunk`` callback line by
    line (the real adapter's live-streaming seam), so the W45 batching producer
    appends one or more ``agent.output.chunk`` envelopes keyed on the wave's
    scope_id with a monotonic ``seq`` -- AND the terminal ``agent.output`` event
    still fires (the chunks are additive, the end-of-spawn tail stays).
    """
    from eawf.runtime.daemon.dispatch_runner import AGENT_OUTPUT_CHUNK_EVENT_TYPE

    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    envelopes = _read_envelopes(event_path)
    chunk_envelopes = [
        env for env in envelopes if env.payload.get("event_type") == AGENT_OUTPUT_CHUNK_EVENT_TYPE
    ]
    chunk_payloads = [env.payload for env in chunk_envelopes]
    # At least one chunk landed, every chunk is scoped to the dispatched wave, and
    # the seq is monotonic + contiguous from 0 (the order is reconstructible).
    assert chunk_payloads
    assert all(env.scope_id == _WAVE_ID for env in chunk_envelopes)
    assert all(p["wave_id"] == _WAVE_ID for p in chunk_payloads)
    seqs = [p["seq"] for p in chunk_payloads]
    assert seqs == list(range(len(seqs)))
    # The terminal agent.output event still fires (chunks are additive).
    terminal = [
        env for env in envelopes if env.payload.get("event_type") == AGENT_OUTPUT_EVENT_TYPE
    ]
    assert len(terminal) == 1


def test_dispatch_spawn_model_override_prices_against_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit model override is the model the spawn is driven with."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True, "model": "claude-haiku-4-5"}))

    # The override is the model handed to spawn_session.
    assert adapter.models == ["claude-haiku-4-5"]


def test_dispatch_spawn_resolves_model_from_routing_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no override, the model resolves from the wave's role + effort.

    An executor / L wave routes to the Opus tier per the default routing
    table, so the spawn is driven with that model.
    """
    state_path = _write_state(tmp_path, agent_role="executor", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.models == ["claude-opus-4-8"]


def _write_state_with_runtime(tmp_path: Path, *, runtime: str, effort_bucket: str) -> Path:
    """Serialise a state whose only wave pins ``runtime_preference=[runtime]``."""
    from eawf.kernel.state.models import State

    payload = _state_payload(agent_role="executor", effort_bucket=effort_bucket)
    payload["waves"][_WAVE_ID]["runtime_preference"] = [runtime]
    state = State.model_validate(payload)
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


class _VendorStubAdapter(_StubAdapter):
    """A stub adapter bound to a specific runtime id (codex / opencode).

    Inherits the claude stub's spawn machinery but stamps its own ``id`` +
    ``runtime`` so the live path resolves it as the foreign vendor and the
    resulting :class:`SpawnResult` carries that runtime. ``resolved_model`` is
    left ``None`` so the metering writer prices against the requested model the
    routing resolved (the assertion target).
    """

    def __init__(self, runtime_id: str) -> None:
        super().__init__()
        self.id = runtime_id

    async def spawn_session(self, prompt: str, *, model: str, **kwargs: Any) -> SpawnResult:  # type: ignore[override]
        self.spawn_calls += 1
        self.prompts.append(prompt)
        self.models.append(model)
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id=f"sess-{self.id}",
            runtime=self.id,
            model=model,
            resolved_model=None,
            subprocess_pid=_STUB_PID,
            exit_status=0,
            text=_executor_report_json(),
            input_tokens=100,
            output_tokens=42,
            cache_creation_input_tokens=80,
            cache_creation_5m_input_tokens=50,
            cache_creation_1h_input_tokens=30,
            cache_read_input_tokens=200,
            started_at=_T0,
            ended_at=_T1,
        )


@pytest.mark.parametrize(
    ("runtime", "effort_bucket", "expected_model"),
    [
        ("codex", "L", "gpt-5.5"),
        ("codex", "XS", "gpt-5.3-codex-spark"),
        ("opencode", "L", "anthropic/claude-opus-4-8"),
        ("opencode", "XS", "anthropic/claude-haiku-4-5"),
    ],
)
def test_dispatch_spawn_resolves_per_vendor_model_for_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    effort_bucket: str,
    expected_model: str,
) -> None:
    """A codex / opencode spawn is driven with ITS OWN vendor model id.

    The cross-vendor reachability fix: when the wave pins a non-claude runtime,
    the live path resolves that runtime's own per-tier model (a bare OpenAI id
    for codex, a ``provider/model`` id for opencode) rather than a claude id the
    foreign CLI would reject.
    """
    state_path = _write_state_with_runtime(tmp_path, runtime=runtime, effort_bucket=effort_bucket)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _VendorStubAdapter(runtime)
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert adapter.models == [expected_model]


def test_dispatch_spawn_codex_priced_cost_is_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex spawn prices its own model to a real non-zero dispatch_cost.

    Proves the pricing lane: the codex per-tier model resolves to a pricing row
    so the emitted ``dispatch_cost`` carries a real cost, not the $0 the
    unpriced fallback would have produced before W15.
    """
    state_path = _write_state_with_runtime(tmp_path, runtime="codex", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _VendorStubAdapter("codex"))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    pricing = lookup_pricing("gpt-5.5")
    assert pricing is not None
    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert Decimal(costs[0]["cost_usd"]) > Decimal("0")
    assert costs[0]["runtime"] == "codex"
    assert costs[0]["model"] == "gpt-5.5"


def test_dispatch_spawn_flips_wave_to_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's head transition flips the CLAIMED wave to IN_PROGRESS."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    wave = load_state(state_path).waves[_WAVE_ID]
    assert wave.status.value == "in_progress"


# --------------------------------------------------------------------------- #
# SessionConflict reuse: a second live dispatch reuses the open session.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_reuses_active_session_on_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second live dispatch reuses the already-ACTIVE executor session.

    ``start_session`` raises ``SessionConflict`` for an existing ACTIVE
    ``(scope, runtime)`` pair; the live path catches it and reuses the open
    session rather than failing, so no duplicate session is registered.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    first: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    second: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    state = load_state(state_path)
    # Still exactly one session; the second dispatch reused the first.
    assert len(state.agent_sessions) == 1
    assert first["session_id"] == second["session_id"]
    # Two attempts of the same wave append two executor report rows.
    assert len(_executor_report_rows(state_path)) == 2


# --------------------------------------------------------------------------- #
# Criterion (d): back-compat -- spawn=False is byte-unchanged.
# --------------------------------------------------------------------------- #


def test_dispatch_plan_only_path_unchanged_when_spawn_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn=False stays plan-only: no session, no spawn, pid 0, no events."""
    state_path = _write_state(tmp_path)
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=None)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "session_policy": "fresh"}))

    assert result["pid"] == 0
    assert result["event_ids"] == []
    # No spawn ran and no session was registered.
    assert adapter.spawn_calls == 0
    assert load_state(state_path).agent_sessions == {}
    assert _executor_report_rows(state_path) == []


def test_dispatch_default_spawn_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting ``spawn`` entirely defaults to the plan-only path (no spawn)."""
    state_path = _write_state(tmp_path)
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=None)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID}))

    assert result["pid"] == 0
    assert adapter.spawn_calls == 0


# --------------------------------------------------------------------------- #
# Error paths.
# --------------------------------------------------------------------------- #


def test_dispatch_spawn_without_state_path_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn=True without a state_path fails fast with a typed LiveSpawnError."""
    _patch_adapter(monkeypatch, _StubAdapter())
    # No state_path; an event_path alone is not enough for the live path.
    ctx = _ctx(None, event_path=tmp_path / "store" / "event.jsonl")

    with pytest.raises(LiveSpawnError, match="requires state_path"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))


def test_dispatch_spawn_without_event_path_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn=True without an event_path fails fast with a typed LiveSpawnError."""
    state_path = _write_state(tmp_path)
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=None)

    with pytest.raises(LiveSpawnError, match="requires state_path"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    # No spawn was attempted -- the guard fires first.
    assert adapter.spawn_calls == 0


def test_dispatch_spawn_and_outcome_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both spawn=True and an outcome is rejected before any spawn."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _StubAdapter()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    outcome = {
        "model": "claude-opus-4-8",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": "0.01",
    }
    with pytest.raises(ValueError, match="mutually exclusive"):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True, "outcome": outcome}))
    assert adapter.spawn_calls == 0


def test_dispatch_spawn_rejects_unknown_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live dispatch for a wave absent from state fails fast."""
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _StubAdapter())
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(ValueError, match="unknown wave"):
        _run(dispatch(ctx, {"wave_id": "P29-I04-W99", "spawn": True}))


# --------------------------------------------------------------------------- #
# R3: a dispatch that completes AFTER close must not persist a phantom attempt.
# When close-on-behalf has already moved the wave to a terminal status, the
# live-spawn persist re-reads ``wave.status`` under the lock and DROPS the
# attempt -- no SessionAttempt row, no dispatch_history entry, no cost / tokens,
# no state.json mutation -- returning ``None`` so the caller short-circuits.
# --------------------------------------------------------------------------- #


def _terminal_spawn_result(*, runtime: str) -> SpawnResult:
    """A canned :class:`SpawnResult` for the terminal-drop persist test."""
    return SpawnResult(
        session_id="sess-late-xyz789",
        runtime=runtime,
        model="model-under-test",
        resolved_model="model-under-test",
        subprocess_pid=_STUB_PID,
        exit_status=0,
        text=_executor_report_json(),
        input_tokens=100,
        output_tokens=42,
        cache_creation_input_tokens=80,
        cache_creation_5m_input_tokens=50,
        cache_creation_1h_input_tokens=30,
        cache_read_input_tokens=200,
        started_at=_T0,
        ended_at=_T1,
    )


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_persist_live_session_attempt_drops_when_wave_terminal(
    tmp_path: Path, runtime: str
) -> None:
    """A persist that lands after close-on-behalf drops: no attempt, no cost.

    Models the R3 race: ``_spawn_and_dispatch`` releases the state lock after
    the claim and runs the spawn UNLOCKED, so close-on-behalf can close the
    wave before the in-flight dispatch reaches its persist step. The persist
    re-reads ``wave.status`` under the lock; on a terminal status it writes no
    :class:`~eawf.kernel.state.models.SessionAttempt`, appends no
    ``dispatch_history`` row, accrues no cost / tokens, and leaves
    ``state.json`` untouched, returning ``None``.
    """
    state_path = _write_state(tmp_path, wave_status="closed")
    before = state_path.read_text(encoding="utf-8")
    ctx = _ctx(state_path)

    result = _persist_live_session_attempt(
        ctx,
        wave_id=_WAVE_ID,
        requested_runtime=runtime,
        serving_runtime=runtime,
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:sess-late-xyz789",
        spawn_result=_terminal_spawn_result(runtime=runtime),
        pid=_STUB_PID,
    )

    # 1. The persist is dropped -- the caller gets the terminal sentinel.
    assert result is None
    # 2. state.json is byte-for-byte unchanged (no attempt / history / accrual).
    assert state_path.read_text(encoding="utf-8") == before
    # 3. No SessionAttempt landed and no cost / tokens accrued on the wave.
    wave = load_state(state_path).waves[_WAVE_ID]
    assert wave.sessions == {}
    assert wave.dispatch_history == []
    assert wave.tokens_consumed == 0
    assert wave.runtime_baseline is None
    assert wave.runtime_latest is None


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_persist_live_session_attempt_persists_when_wave_active(
    tmp_path: Path, runtime: str
) -> None:
    """Back-compat: a non-terminal wave still persists the attempt + priced cost.

    Guards the terminal recheck against over-reach -- when the wave is still
    CLAIMED (not raced by close-on-behalf) the persist writes attempt 1, the
    session-attempt row carries the spawn's priced cost, and the headless
    runtime snapshot is credited onto the wave.
    """
    state_path = _write_state(tmp_path, wave_status="claimed")
    ctx = _ctx(state_path)

    result = _persist_live_session_attempt(
        ctx,
        wave_id=_WAVE_ID,
        requested_runtime=runtime,
        serving_runtime=runtime,
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:sess-late-xyz789",
        spawn_result=_terminal_spawn_result(runtime=runtime),
        pid=_STUB_PID,
    )

    assert result is not None
    attempt, _annotation, session_attempt = result
    assert attempt == 1
    wave = load_state(state_path).waves[_WAVE_ID]
    assert set(wave.sessions) == {1}
    assert wave.sessions[1].session_id == "sess-late-xyz789"
    # The persisted attempt's cost matches the returned session-attempt row.
    assert wave.sessions[1].cost_usd == pytest.approx(float(session_attempt.cost_usd))
    # A headless runtime fires no runtime.capture RPC, so the persist credits
    # its priced snapshot onto the wave (proving the non-terminal path ran).
    assert wave.runtime_baseline is not None
    assert wave.runtime_latest is not None
