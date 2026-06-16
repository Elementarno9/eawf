"""Tests: a live spawn binds the agent's REAL output to the executor report.

Exercises FLEET-1 (P30-I06-W01): the live-spawn path of
:func:`eawf.runtime.daemon.methods.agent.dispatch` (``spawn=True``) now drives
:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema` over the spawned
executor's OWN ``text`` to populate a validated
:class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody`, replacing the
synthetic ``"dispatch served by ..."`` placeholder the runner used to mint.

The adapter ``spawn_session`` is ALWAYS a monkeypatched RECORDING STUB returning
canned :class:`~eawf.runtime.runtimes.adapter.SpawnResult` rows -- no real
subprocess, no network, no cost. The stub's ``text`` is the spawned agent's
modeled answer.

The two load-bearing assertions, one per wave success criterion:

- criterion 1: a stub that returns valid ``ExecutorReportBody`` JSON yields an
  ``executor_report.jsonl`` row whose ``outcome`` / ``files_changed`` /
  ``verdict`` equal the stub's emitted body, and are NOT the synthetic
  ``"dispatch served by ..."`` string;
- criterion 2: a stub whose ``text`` fails ``ExecutorReportBody`` validation
  re-asks with a correction notice that NAMES the validation failure.

P30-I20-W41 layers the SAFETY-NET half on top: when the bounded re-ask loop
exhausts its ceiling (a model that answers in prose), the dispatch no longer
lets :class:`LLMAssistError` escape -- it SYNTHESIZES a typed
:class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody` from the
accepted spawn's exit status (PASS on exit 0, FAIL otherwise), at MEDIUM
confidence with a parse-failure follow-up, so :func:`run_dispatch` always runs
and the dispatch-cost + EU accrual still fire. The error-path tests below assert
that synthesized row instead of a propagated exception.
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
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    StoreKind,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import dispatch
from eawf.runtime.runtimes.adapter import SpawnResult

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I06-W01"
_T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 10, 12, 0, 5, tzinfo=UTC)
_STUB_PID = 13579

# The agent's modeled body -- the words the persisted report must carry.
_REAL_OUTCOME = "rebuilt the report binding so the row carries the agent words"
_REAL_FILES = [
    "src/eawf/runtime/daemon/methods/agent.py",
    "src/eawf/workflow/dispatch/llm_assist.py",
]
_SYNTHETIC_PREFIX = "dispatch served by"


def _valid_executor_json() -> str:
    """A schema-valid ``ExecutorReportBody`` JSON string carrying the agent words."""
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "executor finished the wave",
            "wave_id": _WAVE_ID,
            "files_changed": _REAL_FILES,
            "tests_run": ["uv run pytest tests/daemon -q"],
            "outcome": _REAL_OUTCOME,
        }
    )


class _RecordingStub:
    """A RuntimeAdapter stand-in that returns a fixed ``text`` and records prompts.

    ``spawn_session`` returns a canned :class:`SpawnResult` whose ``text`` is the
    constructor-supplied modeled answer (valid or invalid executor JSON), records
    every prompt it was handed (so a test can inspect the re-ask correction
    notice), and fires the ``on_spawn`` callback with a fixed pid.
    """

    id = "claude-code"
    cli_binary = "claude"

    def __init__(self, *, text: str, exit_status: int = 0) -> None:
        self._text = text
        self._exit_status = exit_status
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
            session_id="sess-bind-1",
            runtime="claude-code",
            model=model,
            resolved_model="claude-opus-4-8",
            subprocess_pid=_STUB_PID,
            exit_status=self._exit_status,
            text=self._text,
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


def _state_payload() -> dict[str, Any]:
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
            "track_id": None,
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
                "title": "Bind spawned executor output to the report",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/agent.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "persist the agent own report body",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "persist the agent own report body",
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
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path) -> Path:
    from eawf.kernel.state.models import State

    state = State.model_validate(_state_payload())
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


def _dispatch_cost_event_count(event_path: Path) -> int:
    """Count ``dispatch_cost`` events in the live event log.

    A nonzero count proves :func:`run_dispatch` reached its cost-emit step --
    the synthesized-fallback path must still accrue cost rather than strand the
    wave on an escaped :class:`LLMAssistError`.
    """
    if not event_path.exists():
        return 0
    return sum(
        1
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and '"event_type":"dispatch_cost"' in line.replace(" ", "")
    )


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Criterion 1: the persisted row carries the agent's OWN body, not the stub.
# --------------------------------------------------------------------------- #


def test_spawn_persists_real_executor_body_not_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid stub body lands verbatim: outcome / files_changed / verdict match.

    The keystone fidelity assertion -- the persisted ``executor_report.jsonl``
    row carries the spawned agent's OWN outcome / files_changed / verdict (parsed
    via the schema-assist path), NOT the synthetic ``"dispatch served by ..."``
    string the runner used to mint.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _RecordingStub(text=_valid_executor_json())
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    body = payload.body
    assert payload.header.role is AgentSessionRole.EXECUTOR
    # The agent's OWN words landed.
    assert body.outcome == _REAL_OUTCOME
    assert body.files_changed == _REAL_FILES
    assert body.verdict.value == "pass"
    # And it is NOT the synthetic placeholder the runner used to mint.
    assert not body.outcome.startswith(_SYNTHETIC_PREFIX)
    assert not body.summary.startswith(_SYNTHETIC_PREFIX)
    # Exactly one spawn ran: the assist loop reused the accepted spawn.
    assert adapter.spawn_calls == 1


# --------------------------------------------------------------------------- #
# Criterion 2: an invalid body re-asks naming the failure, then synthesizes.
# --------------------------------------------------------------------------- #


def test_spawn_invalid_body_reasks_then_synthesizes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid output re-asks naming the failure, then SYNTHESIZES a typed row.

    A stub whose ``text`` is not a valid ``ExecutorReportBody`` (missing the
    executor-required ``wave_id`` / ``outcome``) drives the bounded re-ask loop:
    the re-ask prompt names the validation failure. On ceiling-exhaustion the
    dispatch no longer lets :class:`LLMAssistError` escape -- it synthesizes a
    typed ``ExecutorReportBody`` from the accepted spawn's exit status so
    :func:`run_dispatch` still runs (cost + EU accrue) and the wave is not
    stranded. The synthesized row carries MEDIUM confidence (the report was not
    authored) and one parse-failure follow-up so the degrade is auditable.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    # Valid JSON, wrong shape: an executor body without wave_id / outcome.
    invalid = json.dumps({"role": "executor", "verdict": "pass", "confidence": "high"})
    adapter = _RecordingStub(text=invalid)
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    # No exception escapes: the dispatch synthesizes a body and completes.
    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # The re-ask prompts (every spawn after the first) named the validation
    # failure before the loop exhausted its ceiling.
    assert adapter.spawn_calls >= 2
    reask_prompts = adapter.prompts[1:]
    assert reask_prompts
    for reask in reask_prompts:
        assert "Output correction required" in reask
        assert "schema_mismatch" in reask

    # Exactly one synthesized report row landed.
    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    payload = AgentReportPayload.model_validate(rows[0].payload)
    body = payload.body
    assert payload.header.role is AgentSessionRole.EXECUTOR
    # MEDIUM confidence flags the synthesized (not authored) report.
    assert body.confidence is Confidence.MEDIUM
    # The accepted spawn exited 0 -> PASS per the exit-status rule.
    assert body.verdict is AgentReportVerdict.PASS
    # The synthesized prose names the parse failure (attempt count + reason).
    assert "synthesized executor report" in body.outcome
    assert "schema_mismatch" in body.outcome
    # One auditable parse-failure follow-up is present.
    assert len(body.followups) == 1
    assert "synthesized" in body.followups[0].title
    # No commit landed on the synthesized path.
    assert body.commit_sha is None
    # The keystone: cost accrued (run_dispatch ran) rather than the wave hanging
    # on an escaped LLMAssistError.
    assert _dispatch_cost_event_count(event_path) == 1


def test_spawn_invalid_json_reasks_then_synthesizes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-JSON output re-asks naming ``invalid_json`` then synthesizes a row.

    The error-path companion: a stub whose ``text`` is free prose (not JSON at
    all) is classified ``invalid_json``; the re-ask names that classification and
    on exhaustion the dispatch synthesizes a typed row naming ``invalid_json``
    rather than raising.
    """
    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _RecordingStub(text="the executor answer in free prose")
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    reask_prompts = adapter.prompts[1:]
    assert reask_prompts
    assert all("invalid_json" in reask for reask in reask_prompts)

    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    body = AgentReportPayload.model_validate(rows[0].payload).body
    assert body.confidence is Confidence.MEDIUM
    assert body.verdict is AgentReportVerdict.PASS
    # The synthesized prose names the invalid_json classification.
    assert "invalid_json" in body.outcome
    assert len(body.followups) == 1


def test_spawn_synthesized_verdict_is_fail_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a nonzero accepted-spawn exit synthesizes a FAIL verdict.

    The verdict mirrors the accepted spawn's exit status: ``exit_status == 0``
    is PASS (the two tests above), and a nonzero exit is FAIL. A FAIL body still
    flows into :func:`run_dispatch` -- cost + EU accrue and the report row is
    persisted -- BEFORE the post-execution verify gate blocks the close on the
    FAIL verdict (raising :class:`DispatchCloseBlockedError`). The block is the
    correct downstream behavior: a synthesized FAIL must not silently close the
    wave, but it must not strand it either (cost already accrued, row recorded).
    """
    from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    # Prose output (never validates) + a nonzero accepted-spawn exit status.
    adapter = _RecordingStub(text="prose, never valid json", exit_status=1)
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    # The synthesized FAIL is persisted, then the verify gate blocks the close.
    with pytest.raises(DispatchCloseBlockedError) as exc_info:
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    assert "verdict=fail" in str(exc_info.value)

    # The report row landed (cost + EU accrued) BEFORE the close was blocked.
    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    body = AgentReportPayload.model_validate(rows[0].payload).body
    assert body.verdict is AgentReportVerdict.FAIL
    assert body.confidence is Confidence.MEDIUM
    assert "exit_status=1" in body.outcome
    assert len(body.followups) == 1
