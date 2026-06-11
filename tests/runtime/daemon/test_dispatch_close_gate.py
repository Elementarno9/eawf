"""Integration: dispatch runner consults the W57 post-execution verify gate.

Exercises :func:`eawf.runtime.daemon.dispatch_runner.emit_agent_end_report`
end-to-end against a real ``state.json`` + ``executor_report`` JSONL
store on a tmp filesystem. The load-bearing assertion: a
``FAIL`` / ``BLOCKED`` executor body still lands on disk (the report
is a durable record) but the runner raises
:class:`~eawf.workflow.verify.dispatch_close.DispatchCloseBlockedError`
so the close path stops at the first verifiable failure rather than
silently accepting an unverified attempt.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportPayload,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.dispatch_runner import (
    DispatchTokens,
    emit_agent_end_report,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

pytestmark = pytest.mark.integration

_WAVE_ID = "P28-I03-W57"
_OTHER_WAVE_ID = "P28-I03-W56"
_SESSION_ID = "SES-executor-w57"
_AGENT_PRINCIPAL_ID = "u-12345678"
_EXECUTOR_REPORT_KIND = store_kind_for_role(AgentSessionRole.EXECUTOR)


def _state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-28T00:00:00Z",
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
            "phase_id": "P28",
            "iter_id": "P28-I03",
            "active_wave_ids": [],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P28": {
                "id": "P28",
                "scope_id": "EAWF",
                "title": "v0.4 verify spine",
                "status": "active",
                "iter_ids": ["P28-I03"],
                "outcome_ids": [],
                "opened_at": "2026-05-28T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P28-I03": {
                "id": "P28-I03",
                "phase_id": "P28",
                "title": "Build-out",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-28T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P28-I03",
                "title": "Verify gate + codex + iter prefix",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/workflow/verify/dispatch_close.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-05-28T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            _SESSION_ID: {
                "id": _SESSION_ID,
                "role": "executor",
                "runtime": "claude",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-05-28T00:00:00Z",
                "ended_at": None,
                "summary": None,
                "agent_principal_id": _AGENT_PRINCIPAL_ID,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    state = State.model_validate(_state_payload())
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    """Daemon context wired to a tmp ``state.json`` + sibling event log."""
    return MethodContext(
        started_at="2026-05-28T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.4.0",
        bus=None,
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def _tokens() -> DispatchTokens:
    return DispatchTokens(
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=8000,
        cache_read_input_tokens=64000,
    )


def _report_envelope(state_path: Path) -> Envelope:
    """Load the single executor-report envelope written under *state_path*."""
    path = store_path(state_path, _EXECUTOR_REPORT_KIND)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return Envelope.model_validate_json(lines[0])


# ---- Pass path -------------------------------------------------------------


def test_emit_agent_end_report_passes_gate_on_clean_verdict(tmp_path: Path) -> None:
    """A clean PASS verdict returns the report id with no raise."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    report_id = emit_agent_end_report(
        ctx,
        session_id=_SESSION_ID,
        wave_id=_WAVE_ID,
        commit_sha="abcdef1",
        outcome="implemented W57 verify gate",
        files_changed=["src/eawf/workflow/verify/dispatch_close.py"],
        tests_run=["uv run pytest tests/workflow/verify -q"],
        runtime="claude",
    )
    envelope = _report_envelope(state_path)
    assert envelope.id == report_id
    payload = AgentReportPayload.model_validate(envelope.payload)
    assert payload.body.verdict is AgentReportVerdict.PASS


def test_run_dispatch_passes_gate_end_to_end(tmp_path: Path) -> None:
    """A clean ``run_dispatch`` flow completes; the gate does not interfere."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    result = run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=_tokens(),
        cost_usd=Decimal("0.05"),
        session_id=_SESSION_ID,
        commit_sha="abcdef1",
        outcome="done",
    )
    assert result.report_id is not None


# ---- Fail path -------------------------------------------------------------


def test_emit_agent_end_report_blocks_on_fail_verdict(tmp_path: Path) -> None:
    """A FAIL verdict raises :class:`DispatchCloseBlockedError` AFTER persist."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    with pytest.raises(DispatchCloseBlockedError) as excinfo:
        emit_agent_end_report(
            ctx,
            session_id=_SESSION_ID,
            wave_id=_WAVE_ID,
            commit_sha="abcdef1",
            outcome="ran into a fixture mismatch",
            runtime="claude",
            verdict=AgentReportVerdict.FAIL,
            confidence=Confidence.HIGH,
        )
    assert excinfo.value.wave_id == _WAVE_ID
    assert excinfo.value.result.verdict is AgentReportVerdict.FAIL
    # The report row is still on disk — the gate stops the close path, not the persist path.
    payload = AgentReportPayload.model_validate(_report_envelope(state_path).payload)
    assert payload.body.verdict is AgentReportVerdict.FAIL


def test_emit_agent_end_report_blocks_on_blocked_verdict(tmp_path: Path) -> None:
    """A BLOCKED verdict raises through the gate."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    with pytest.raises(DispatchCloseBlockedError):
        emit_agent_end_report(
            ctx,
            session_id=_SESSION_ID,
            wave_id=_WAVE_ID,
            commit_sha="abcdef1",
            outcome="upstream API unavailable",
            runtime="claude",
            verdict=AgentReportVerdict.BLOCKED,
            confidence=Confidence.LOW,
        )


def test_run_dispatch_raises_when_explicit_verdict_fails_gate(tmp_path: Path) -> None:
    """``run_dispatch`` propagates :class:`DispatchCloseBlockedError` from the gate."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    with pytest.raises(DispatchCloseBlockedError):
        run_dispatch(
            ctx,
            wave_id=_WAVE_ID,
            primary_runtime="claude",
            fallback_runtime="codex",
            model="claude-opus-4-7",
            pricing_version="2026.05.17",
            primary_error=None,
            tokens=_tokens(),
            cost_usd=Decimal("0.05"),
            session_id=_SESSION_ID,
            commit_sha="abcdef1",
            outcome="failed mid-flight",
            verdict=AgentReportVerdict.FAIL,
        )
    # The event store still carries the C09 events emitted before the gate
    # tripped — the gate stops the close path AFTER cost/accrue events
    # have lawfully landed on disk.
    event_path = state_path.parent / "store" / "event.jsonl"
    assert event_path.exists()
