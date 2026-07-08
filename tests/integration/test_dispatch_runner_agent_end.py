"""Integration: dispatch runner emits a valid ``agent_end`` executor report.

Exercises :func:`eawf.runtime.daemon.dispatch_runner.emit_agent_end_report` and the
report-on-completion path of
:func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` end-to-end against a real
``state.json`` + ``executor_report`` JSONL store on a tmp filesystem.

The load-bearing assertion is the wave's success criterion: the emitted
``agent_end`` row carries a valid verdict AND
:func:`eawf.kernel.validate.invariants.check_agent_report_invariants` returns no
violations for it. The runner emits through the canonical agent-report
writer (:func:`eawf.workflow.agent_report.store.append_agent_report`) so the
persisted envelope is indistinguishable from one written by the
operator-facing ``eawf hook event`` AGENT_END path.
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
    ExecutorReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.invariants import check_agent_report_invariants
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.runtime.daemon.dispatch_runner import (
    DispatchTokens,
    emit_agent_end_report,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.verify.dispatch_close import DispatchCloseBlockedError

pytestmark = pytest.mark.integration

_WAVE_ID = "P27-I03-W10"
_SESSION_ID = "SES-executor"
_AGENT_PRINCIPAL_ID = "u-12345678"
_EXECUTOR_REPORT_KIND = store_kind_for_role(AgentSessionRole.EXECUTOR)


def _state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-23T00:00:00Z",
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
            "phase_id": "P27",
            "iter_id": "P27-I03",
            "active_wave_ids": [],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P27": {
                "id": "P27",
                "scope_id": "EAWF",
                "title": "Observability",
                "status": "active",
                "iter_ids": ["P27-I03"],
                "outcome_ids": [],
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P27-I03": {
                "id": "P27-I03",
                "phase_id": "P27",
                "title": "Build-out",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-23T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P27-I03",
                "title": "Emit typed agent_end reports",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/dispatch_runner.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-05-23T00:00:00Z",
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
                "started_at": "2026-05-23T00:00:00Z",
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
        started_at="2026-05-23T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
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


def test_emit_agent_end_report_passes_invariants(tmp_path: Path) -> None:
    """The emitted ``agent_end`` row validates clean against the invariants."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    report_id = emit_agent_end_report(
        ctx,
        session_id=_SESSION_ID,
        wave_id=_WAVE_ID,
        commit_sha="abcdef1",
        outcome="emitted typed agent_end report on dispatch completion",
        files_changed=["src/eawf/runtime/daemon/dispatch_runner.py"],
        tests_run=["uv run pytest tests/integration/test_dispatch_runner_agent_end.py -q"],
        runtime="claude",
    )

    envelope = _report_envelope(state_path)
    assert envelope.id == report_id
    payload = AgentReportPayload.model_validate(envelope.payload)
    assert isinstance(payload.body, ExecutorReportBody)
    assert payload.header.agent_principal_id == _AGENT_PRINCIPAL_ID
    assert payload.body.verdict is AgentReportVerdict.PASS
    assert payload.body.commit_sha == "abcdef1"

    state = State.model_validate(_state_payload())
    violations = list(check_agent_report_invariants(state, [envelope]))
    assert violations == [], violations


def test_run_dispatch_emits_agent_end_report_on_completion(tmp_path: Path) -> None:
    """``run_dispatch`` emits the report when a ``session_id`` is supplied."""
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
    envelope = _report_envelope(state_path)
    assert envelope.id == result.report_id
    payload = AgentReportPayload.model_validate(envelope.payload)
    assert payload.header.agent_principal_id == _AGENT_PRINCIPAL_ID
    state = State.model_validate(_state_payload())
    assert list(check_agent_report_invariants(state, [envelope])) == []


def test_run_dispatch_fallback_report_verdict_is_pass_with_followups(tmp_path: Path) -> None:
    """A V5 fallback derives a ``pass-with-followups`` report verdict."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    result = run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="codex",
        model="codex-model",
        pricing_version="2026.05.17",
        primary_error=RuntimeErrorClass.RUNTIME_RATE_LIMIT,
        tokens=_tokens(),
        cost_usd=Decimal("0.07"),
        session_id=_SESSION_ID,
        commit_sha="abcdef1",
    )

    assert result.switched is True
    assert result.report_id is not None
    payload = AgentReportPayload.model_validate(_report_envelope(state_path).payload)
    assert payload.body.verdict is AgentReportVerdict.PASS_WITH_FOLLOWUPS


def test_run_dispatch_without_session_id_skips_report(tmp_path: Path) -> None:
    """No ``session_id`` → no report emitted, ``report_id`` stays ``None``."""
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
    )

    assert result.report_id is None
    assert not store_path(state_path, _EXECUTOR_REPORT_KIND).exists()


def test_emit_agent_end_report_requires_state_path() -> None:
    """The runner refuses to emit a report when ``state_path`` is unset."""
    ctx = MethodContext(
        started_at="2026-05-23T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=None,
        event_path=None,
        state_path=None,
    )
    with pytest.raises(RuntimeError, match="state_path not configured"):
        emit_agent_end_report(
            ctx,
            session_id=_SESSION_ID,
            wave_id=_WAVE_ID,
            commit_sha="abcdef1",
            outcome="done",
            runtime="claude",
        )


def test_emit_agent_end_report_unknown_session_raises(tmp_path: Path) -> None:
    """A genuinely unresolvable session (unknown wave) fails fast (W09).

    An unknown session id whose wave IS known is reconstructed from the wave's
    agent_role (W09), so the KeyError only survives when the wave is unknown too
    -- there is then no bookkeeping to rebuild the session from.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    with pytest.raises(KeyError, match="unknown agent session"):
        emit_agent_end_report(
            ctx,
            session_id="SES-missing",
            wave_id="P28-I03-W99",
            commit_sha="abcdef1",
            outcome="done",
            runtime="claude",
        )


def test_emit_agent_end_report_explicit_verdict_override(tmp_path: Path) -> None:
    """An explicit verdict overrides the switched-derived default.

    The W57 post-execution verify gate refuses a ``BLOCKED`` verdict,
    so the call raises :class:`DispatchCloseBlockedError` AFTER the
    report has been persisted. The persisted row still carries the
    operator's explicit verdict + confidence — the gate only stops the
    close path from advancing, it does not unwind the on-disk record.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    with pytest.raises(DispatchCloseBlockedError) as excinfo:
        emit_agent_end_report(
            ctx,
            session_id=_SESSION_ID,
            wave_id=_WAVE_ID,
            commit_sha="abcdef1",
            outcome="blocked on missing fixture",
            runtime="claude",
            verdict=AgentReportVerdict.BLOCKED,
            confidence=Confidence.LOW,
            switched=False,
        )
    assert excinfo.value.wave_id == _WAVE_ID
    assert excinfo.value.result.verdict is AgentReportVerdict.BLOCKED

    payload = AgentReportPayload.model_validate(_report_envelope(state_path).payload)
    assert payload.body.verdict is AgentReportVerdict.BLOCKED
    assert payload.body.confidence is Confidence.LOW
