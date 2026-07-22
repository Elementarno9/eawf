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
:class:`~eawf.kernel.store.kinds.agent_report.ExecutorReportBody` so
:func:`run_dispatch` always runs and the dispatch-cost + EU accrual still fire.

P30-I26-W07 hardens the synth verdict (truth defect T8): a synthesized body was
NEVER authored by the agent, and ``exit_status == 0`` means the process exited,
not that the work passed -- so the synth path must not mint a green
:attr:`~eawf.kernel.state.enums.AgentReportVerdict.PASS`. It now ALWAYS mints
:attr:`~eawf.kernel.state.enums.AgentReportVerdict.BLOCKED` (mirroring the
researcher synth path) at LOW confidence, carries a
:attr:`~eawf.kernel.state.enums.ReportSource.SYNTHESIZED` provenance marker, and
a parse-failure follow-up. BLOCKED is not close-ready, so the close gate blocks
the wave (``DispatchCloseBlockedError``) AFTER the row + cost land -- the wave is
never closed green on output no agent authored, but it is not stranded either.
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
    ReportSource,
    StoreKind,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload, ExecutorReportBody
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
            "commit_sha": "abc1234",
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
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
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
            "active_wave_ids": [],
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
                "status": "pending",
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
                "claimed_at": None,
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
    # The authored (bound) path relies on the default provenance marker: a body
    # the agent's own output validated into is AUTHORED, never synthesized.
    assert body.report_source is ReportSource.AUTHORED
    # And it is NOT the synthetic placeholder the runner used to mint.
    assert not body.outcome.startswith(_SYNTHETIC_PREFIX)
    assert not body.summary.startswith(_SYNTHETIC_PREFIX)
    # Exactly one spawn ran: the assist loop reused the accepted spawn.
    assert adapter.spawn_calls == 1


# --------------------------------------------------------------------------- #
# Criterion 2: an invalid body re-asks naming the failure, then synthesizes.
# --------------------------------------------------------------------------- #


def test_spawn_invalid_body_synthesizes_blocked_not_pass_on_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truth defect T8: an exit-0 synth mints BLOCKED (never PASS) + synthesized.

    A stub whose ``text`` is not a valid ``ExecutorReportBody`` (missing the
    executor-required ``wave_id`` / ``outcome``) drives the bounded re-ask loop:
    the re-ask prompt names the validation failure. On ceiling-exhaustion the
    dispatch synthesizes a typed row rather than letting :class:`LLMAssistError`
    escape -- so :func:`run_dispatch` still runs (cost + EU accrue) and the wave
    is not stranded. The keystone assertion: the accepted spawn exited 0, but a
    synthesized body was NEVER authored, so the verdict is BLOCKED (a green PASS
    would close the wave on output no agent stood behind). BLOCKED is not
    close-ready, so the close gate blocks the wave AFTER the row + cost land.
    """
    from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    # Valid JSON, wrong shape: an executor body without wave_id / outcome.
    invalid = json.dumps({"role": "executor", "verdict": "pass", "confidence": "high"})
    adapter = _RecordingStub(text=invalid)
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    # The synthesized BLOCKED row is persisted, then the verify gate blocks the
    # close -- the wave is never closed green on the exit-0 signal.
    with pytest.raises(DispatchCloseBlockedError) as exc_info:
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    assert "verdict=blocked" in str(exc_info.value)

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
    # The keystone: exit 0 does NOT mint a green PASS -- a synthesized body is
    # BLOCKED (never verified as passing), at LOW confidence.
    assert body.verdict is AgentReportVerdict.BLOCKED
    assert body.verdict is not AgentReportVerdict.PASS
    assert body.confidence is Confidence.LOW
    # The provenance marker is honest: this body was synthesized, not authored.
    assert body.report_source is ReportSource.SYNTHESIZED
    # The synthesized prose names the parse failure (attempt count + reason).
    assert "synthesized executor report" in body.outcome
    assert "schema_mismatch" in body.outcome
    # One auditable parse-failure follow-up is present.
    assert len(body.followups) == 1
    assert "synthesized" in body.followups[0].title
    # Synthesized rows retain the explicit sentinel required by the canonical
    # executor-report invariant; the BLOCKED verdict prevents a green close.
    assert body.commit_sha == "0000000"
    # The keystone: cost accrued (run_dispatch ran) rather than the wave hanging
    # on an escaped LLMAssistError.
    assert _dispatch_cost_event_count(event_path) == 1


def test_spawn_invalid_json_synthesizes_blocked_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-JSON output re-asks naming ``invalid_json`` then synthesizes BLOCKED.

    The error-path companion: a stub whose ``text`` is free prose (not JSON at
    all) is classified ``invalid_json``; the re-ask names that classification and
    on exhaustion the dispatch synthesizes a typed BLOCKED / synthesized row
    naming ``invalid_json``. BLOCKED is not close-ready, so the close gate blocks
    the wave after the row lands.
    """
    from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    adapter = _RecordingStub(text="the executor answer in free prose")
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    with pytest.raises(DispatchCloseBlockedError) as exc_info:
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    assert "verdict=blocked" in str(exc_info.value)

    reask_prompts = adapter.prompts[1:]
    assert reask_prompts
    assert all("invalid_json" in reask for reask in reask_prompts)

    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    body = AgentReportPayload.model_validate(rows[0].payload).body
    assert body.verdict is AgentReportVerdict.BLOCKED
    assert body.confidence is Confidence.LOW
    assert body.report_source is ReportSource.SYNTHESIZED
    # The synthesized prose names the invalid_json classification.
    assert "invalid_json" in body.outcome
    assert len(body.followups) == 1


def test_spawn_synthesized_verdict_is_blocked_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a nonzero accepted-spawn exit ALSO synthesizes BLOCKED.

    The verdict no longer mirrors the accepted spawn's exit status: whether the
    spawn exited 0 or nonzero, a synthesized body is BLOCKED because it was never
    authored. A BLOCKED body still flows into :func:`run_dispatch` -- cost + EU
    accrue and the report row is persisted -- BEFORE the post-execution verify
    gate blocks the close on the BLOCKED verdict (raising
    :class:`DispatchCloseBlockedError`). The block is the correct downstream
    behavior: a synthesized report must not silently close the wave, but it must
    not strand it either (cost already accrued, row recorded).
    """
    from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

    state_path = _write_state(tmp_path)
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    # Prose output (never validates) + a nonzero accepted-spawn exit status.
    adapter = _RecordingStub(text="prose, never valid json", exit_status=1)
    _patch_adapter(monkeypatch, adapter)
    ctx = _ctx(state_path, event_path=event_path)

    # The synthesized BLOCKED is persisted, then the verify gate blocks the close.
    with pytest.raises(DispatchCloseBlockedError) as exc_info:
        _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))
    assert "verdict=blocked" in str(exc_info.value)

    # The report row landed (cost + EU accrued) BEFORE the close was blocked.
    rows = _executor_report_rows(state_path)
    assert len(rows) == 1
    body = AgentReportPayload.model_validate(rows[0].payload).body
    assert body.verdict is AgentReportVerdict.BLOCKED
    assert body.confidence is Confidence.LOW
    assert body.report_source is ReportSource.SYNTHESIZED
    assert "exit_status=1" in body.outcome
    assert len(body.followups) == 1


def test_redact_report_body_rewrites_local_paths_keeps_ids() -> None:
    """A headless report's absolute-path prose is redacted before persist.

    The live codex e2e showed the agent citing the repo root in its
    ``outcome``, which the report-store scrub rejects (``absolute_posix_path``)
    -- hard-failing an otherwise-successful wave. The headless dispatch path
    redacts the body's string fields so the scrub passes and the wave closes,
    while ids + repo-relative paths survive.
    """
    from eawf.platform.scrub.scan import scan_text
    from eawf.runtime.daemon.methods.agent import _redact_report_body

    body = ExecutorReportBody(
        verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
        confidence=Confidence.MEDIUM,
        summary="created greeting.txt",
        wave_id="P01-I01-W01",
        outcome="criteria met at /tmp/eawf-smoke-f8Ec/greeting.txt",
        files_changed=["greeting.txt"],
        tests_run=[],
    )
    redacted = _redact_report_body(body)

    # The absolute path is gone (no scrub finding survives), but the wave id and
    # the repo-relative file path are untouched.
    assert "/tmp/eawf-smoke-f8Ec" not in redacted.outcome
    assert not scan_text(redacted.outcome)
    assert redacted.wave_id == "P01-I01-W01"
    assert redacted.files_changed == ["greeting.txt"]
    assert redacted.verdict is AgentReportVerdict.PASS_WITH_FOLLOWUPS
