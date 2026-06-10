"""Tests: live-spawn executor-report AUTHORITY rules (FLEET-4, P30-I06-W04).

A BINDING-PROOF wave -- it pins, with tests, the authority rules a live
spawn's executor report must obey. Three rules under test, one assertion
group each:

- **Rule 1a (exactly-one row per attempt, authored by the registered
  executor session).** One live spawn lands EXACTLY ONE
  ``executor_report.jsonl`` row at ``base_id=wave_id``, attempt 1, whose
  header ``session_id`` is the executor session the daemon registered for
  the wave (not a cosmetic id).
- **Rule 1b (monotonic attempts on re-dispatch with session reuse).** A
  second live dispatch of the same ``(wave, runtime)`` REUSES the open
  executor session (no duplicate) and appends attempt 2 MONOTONICALLY
  under the same ``(role, base_id)`` series, both rows authored by the
  same session id.
- **Rule 2 (no executor self-verdict).** Driving the verdict author
  resolver with the EXECUTOR session occupying the verdict-author slot
  raises :class:`~eawf.workflow.dispatch.verdict.ExecutorSelfReportError`
  -- the verdict author MUST be a fresh AUDITOR session, never the
  executor's. The standalone
  :func:`~eawf.workflow.dispatch.verdict.assert_not_executor_self_report`
  guard refuses the same self-report fail-fast.

The adapter ``spawn_session`` is ALWAYS a monkeypatched RECORDING STUB
returning a canned :class:`~eawf.runtime.runtimes.adapter.SpawnResult`
whose ``text`` is schema-valid executor JSON -- no real subprocess, no
network, no auth, no cost.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import dispatch
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.verdict import (
    ExecutorSelfReportError,
    _resolve_auditor_session,
    assert_not_executor_self_report,
    produce_wave_verdict,
)
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I06-W04"
_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 10, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 24680
_RUNTIME = "claude-code"


def _executor_report_json() -> str:
    """A schema-valid ``ExecutorReportBody`` JSON string the stub spawn returns."""
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "executor implemented the wave",
            "wave_id": _WAVE_ID,
            "files_changed": ["src/eawf/runtime/daemon/dispatch_runner.py"],
            "tests_run": ["uv run pytest tests/daemon -q"],
            "outcome": "pinned the executor-report authority rules",
        }
    )


def _auditor_report_json() -> str:
    """A schema-valid ``AuditorReportBody`` JSON string for the verdict producer."""
    return json.dumps(
        {
            "role": "auditor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": _WAVE_ID,
            "criteria": [{"criterion": "pin the authority rules", "passed": True}],
            "refutations": [],
        }
    )


class _RecordingStub:
    """A RuntimeAdapter stand-in whose spawn_session never forks a process.

    Returns a canned :class:`SpawnResult` whose ``text`` is schema-valid
    executor JSON, records every prompt + model it was handed, and fires the
    ``on_spawn`` callback with a fixed pid.
    """

    id = _RUNTIME
    cli_binary = "claude"

    def __init__(self) -> None:
        self.spawn_calls = 0
        self.prompts: list[str] = []
        self.models: list[str] = []

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
    ) -> SpawnResult:
        self.spawn_calls += 1
        self.prompts.append(prompt)
        self.models.append(model)
        if on_spawn is not None:
            on_spawn(_STUB_PID)
        return SpawnResult(
            session_id="sess-fleet4-1",
            runtime=_RUNTIME,
            model=model,
            resolved_model="claude-opus-4-8",
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

    def session_log_handle(self, session_id: str) -> str:
        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"


class _RecordingSpawn:
    """Recording stand-in for a live ``spawn_session`` (NEVER a real process).

    Replays one canned auditor-body answer per call. Used by the verdict
    producer path; a self-report rejection fires before any spawn, so this
    is only exercised on the happy path.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        text = self._answers[self.calls]
        self.calls += 1
        return SpawnResult(
            session_id="sess-auditor-1",
            runtime=_RUNTIME,
            model="opus",
            subprocess_pid=7777,
            exit_status=0,
            text=text,
            started_at=_T0,
            ended_at=_T1,
        )


def _state_payload(*, extra_sessions: dict[str, Any] | None = None) -> dict[str, Any]:
    """A minimal valid State with the full phase -> iter -> wave chain.

    The wave starts CLAIMED so the runner's head transition flips it to
    IN_PROGRESS, and ``agent_sessions`` starts empty (unless *extra_sessions*
    seeds one) so the live path registers the executor session itself.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-10T00:00:00Z",
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
            "subproject_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I06",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "v0.6",
                "status": "active",
                "iter_ids": ["P30-I06"],
                "outcome_ids": [],
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I06": {
                "id": "P30-I06",
                "phase_id": "P30",
                "title": "Fleet binding",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I06",
                "title": "Pin the executor-report authority rules",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/dispatch_runner.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "one executor report row per wave attempt",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "one executor report row per wave attempt",
                    }
                ],
                "agent_role": "executor",
                "effort_bucket": "L",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-10T00:00:00Z",
                "claimed_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
                "runtime_preference": ["claude-code"],
            }
        },
        "artifacts": {},
        "agent_sessions": extra_sessions or {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, **kwargs: Any) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    state = State.model_validate(_state_payload(**kwargs))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path, *, event_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter: _RecordingStub) -> None:
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.agent.select_adapter",
        lambda runtime_id: adapter,
    )


def _executor_report_rows(state_path: Path) -> list[Envelope]:
    path = store_path(state_path, StoreKind.EXECUTOR_REPORT)
    rows: list[Envelope] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Envelope.model_validate_json(line))
    return rows


def _executor_session(scope_id: str) -> dict[str, Any]:
    """An ACTIVE executor session record occupying *scope_id* on the runtime."""
    return {
        "SES-EXEC": {
            "id": "SES-EXEC",
            "role": "executor",
            "runtime": _RUNTIME,
            "scope_id": scope_id,
            "status": "active",
            "claimed_wave_ids": [],
            "worktree_ids": [],
            "artifact_ids": [],
            "started_at": "2026-06-10T00:00:00Z",
            "ended_at": None,
            "summary": None,
        }
    }


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Rule 1a: one live spawn -> exactly one executor report row at base_id=wave_id
# authored by the registered executor session.
# --------------------------------------------------------------------------- #


def test_live_spawn_yields_exactly_one_executor_report_authored_by_executor_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One spawn -> one row at base_id=wave_id, attempt 1, authored by the executor.

    Pins the authority rule: the persisted ``executor_report.jsonl`` carries
    EXACTLY ONE row whose ``base_id`` is the wave id and whose header
    ``session_id`` is the executor session the daemon registered for the wave
    -- not a placeholder id, and not a duplicate.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _RecordingStub()
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    result: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # Exactly one executor session was registered for the wave.
    state = load_state(state_path)
    sessions = [s for s in state.agent_sessions.values() if s.role is AgentSessionRole.EXECUTOR]
    assert len(sessions) == 1
    executor = sessions[0]
    assert executor.scope_id == _WAVE_ID
    assert executor.status is AgentSessionStatus.ACTIVE
    assert result["session_id"] == executor.id

    # Exactly one executor report row, at base_id=wave_id, attempt 1, authored
    # by the registered executor session.
    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    assert payload.header.role is AgentSessionRole.EXECUTOR
    assert payload.header.base_id == _WAVE_ID
    assert payload.header.attempt == 1
    assert payload.header.session_id == executor.id


# --------------------------------------------------------------------------- #
# Rule 1b: re-dispatch reuses the session + appends attempt N+1 monotonically.
# --------------------------------------------------------------------------- #


def test_redispatch_reuses_session_and_appends_monotonic_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second dispatch of the same (wave, runtime) reuses the session, appends attempt 2.

    Pins the authority rule: re-dispatch does NOT register a duplicate
    executor session; ``start_session`` raises ``SessionConflict`` for the
    open ``(wave, runtime)`` slot and the live path reuses the session. The
    second report row is attempt 2, MONOTONICALLY after attempt 1, both rows
    in one ``(role, base_id)`` series authored by the same session.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _RecordingStub())
    ctx = _ctx(state_path, event_path=event_path)

    first: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    second: dict[str, Any] = _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # The session was reused -- still exactly one executor session.
    state = load_state(state_path)
    sessions = [s for s in state.agent_sessions.values() if s.role is AgentSessionRole.EXECUTOR]
    assert len(sessions) == 1
    assert first["session_id"] == second["session_id"]

    # Two rows under one (role, base_id) series; attempts are 1 then 2,
    # monotonic, both authored by the reused session.
    rows = _executor_report_rows(state_path)
    assert len(rows) == 2
    payloads = [AgentReportPayload.model_validate(row.payload) for row in rows]
    assert [p.header.attempt for p in payloads] == [1, 2]
    assert all(p.header.base_id == _WAVE_ID for p in payloads)
    assert {p.header.session_id for p in payloads} == {first["session_id"]}


def test_third_dispatch_attempt_stays_monotonic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third dispatch appends attempt 3 -- attempts never reset or collide.

    Boundary case past the first re-dispatch: the monotonic counter keeps
    climbing (1, 2, 3) so no attempt is ever reused or overwritten.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_adapter(monkeypatch, _RecordingStub())
    ctx = _ctx(state_path, event_path=event_path)

    for _ in range(3):
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    rows = _executor_report_rows(state_path)
    payloads = [AgentReportPayload.model_validate(row.payload) for row in rows]
    assert [p.header.attempt for p in payloads] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Rule 2: the verdict author MUST be a fresh AUDITOR session; an executor
# occupying the verdict-author slot is refused fail-fast.
# --------------------------------------------------------------------------- #


def test_produce_wave_verdict_rejects_executor_as_verdict_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executor occupying the verdict-author slot raises ExecutorSelfReportError.

    The verdict producer resolves the author slot at the verdict-qualified
    ``"{wave_id}::audit"`` scope. When an ACTIVE EXECUTOR session occupies
    that slot, re-using it would make the executor its own verdict author --
    a self-report. ``produce_wave_verdict`` refuses fail-fast with
    :class:`ExecutorSelfReportError` and spawns NOTHING, proving the verdict
    author must be a fresh AUDITOR session.
    """
    # Seed an executor session occupying the verdict-author slot.
    executor = _executor_session(scope_id=f"{_WAVE_ID}::audit")
    state_path = _write_state(tmp_path, extra_sessions=executor)
    events_path = tmp_path / ".ea" / "store" / "event.jsonl"
    state = load_state(state_path)
    wave = state.waves[_WAVE_ID]
    spawn = _RecordingSpawn([_auditor_report_json()])

    with pytest.raises(ExecutorSelfReportError, match="must be a fresh auditor session"):
        _run(
            produce_wave_verdict(
                state=state,
                state_path=state_path,
                events_path=events_path,
                wave=wave,
                spawn=spawn,
                runtime=_RUNTIME,
                repo_root=tmp_path,
            )
        )
    # The self-report rejection fired before any spawn ran.
    assert spawn.calls == 0


def test_resolve_auditor_session_rejects_executor_occupant(tmp_path: Path) -> None:
    """The author resolver refuses an executor occupying the audit slot, typed.

    Directly exercises :func:`_resolve_auditor_session` -- the shared author
    resolver both the verdict producer and the spec-jury gate call. An
    executor in the ``"{wave_id}::audit"`` slot is rejected as a self-report
    rather than re-raised as an opaque ``SessionConflict``.
    """
    executor = _executor_session(scope_id=f"{_WAVE_ID}::audit")
    state_path = _write_state(tmp_path, extra_sessions=executor)
    events_path = tmp_path / ".ea" / "store" / "event.jsonl"
    state = load_state(state_path)
    wave = state.waves[_WAVE_ID]

    with pytest.raises(ExecutorSelfReportError, match="must be a fresh auditor session"):
        _resolve_auditor_session(
            state=state,
            events_path=events_path,
            wave=wave,
            runtime=_RUNTIME,
            now=None,
        )


def test_assert_not_executor_self_report_rejects_executor_author(tmp_path: Path) -> None:
    """The standalone self-report guard refuses an executor author fail-fast.

    Pins the second authority surface a daemon close path uses before
    accepting an externally-supplied verdict author: an executor session id
    cannot author its own wave's verdict.
    """
    executor = _executor_session(scope_id=_WAVE_ID)
    state_path = _write_state(tmp_path, extra_sessions=executor)
    state = load_state(state_path)

    with pytest.raises(ExecutorSelfReportError, match="cannot author its own"):
        assert_not_executor_self_report(state, wave_id=_WAVE_ID, author_session_id="SES-EXEC")


def test_produce_wave_verdict_with_bare_scope_executor_still_registers_fresh_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executor on the BARE wave scope does not block a fresh auditor verdict.

    The realistic close-time scenario: the executor session for the wave is
    ACTIVE on the bare ``wave_id`` scope, NOT the verdict-qualified
    ``"{wave_id}::audit"`` slot. The verdict producer coexists with it -- it
    registers a FRESH AUDITOR (never the executor) and the verdict lands. This
    guards against the self-report fix over-firing on the legitimate path.
    """
    executor = _executor_session(scope_id=_WAVE_ID)
    state_path = _write_state(tmp_path, extra_sessions=executor)
    events_path = tmp_path / ".ea" / "store" / "event.jsonl"
    state = load_state(state_path)
    wave = state.waves[_WAVE_ID]

    result = _run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn([_auditor_report_json()]),
            runtime=_RUNTIME,
            repo_root=tmp_path,
        )
    )

    assert result.auditor_session_id != "SES-EXEC"
    author = state.agent_sessions[result.auditor_session_id]
    assert author.role is AgentSessionRole.AUDITOR
    assert author.scope_id == f"{_WAVE_ID}::audit"
