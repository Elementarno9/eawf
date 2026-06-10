"""Unit tests for the ``eawf phase retro`` closure-digest builder.

Covers the join (``wave_id == base_id``), the failed-wave flag (``WaveStatus``
or report verdict), the honest-empty no-reports path, and the unknown-phase
``ValueError``. Mirrors ``tests/unit/test_lifecycle_phase_prepare_close.py``
(state construction) and ``tests/unit/test_agent_report_rollup.py`` (report
envelope construction).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.workflow.agent_report.rollup import (
    PhaseRetroDigest,
    phase_retro_digest,
    render_phase_retro_markdown,
)
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from tests.conftest import make_intent

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": NOW.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _phase_with_wave(*, wave_status: WaveStatus = WaveStatus.CLOSED) -> State:
    """Build a state with P03 / P03-I01 / P03-I01-W01 in *wave_status*."""
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    open_iter(state, iter_id="P03-I01", phase_id="P03", title="i")
    plan_wave(
        state,
        wave_id="P03-I01-W01",
        iter_id="P03-I01",
        title="first wave",
        file_scopes=["x"],
        effort_bucket="M",
        intent=make_intent(),
    )
    wave = state.waves["P03-I01-W01"]
    wave.status = wave_status
    wave.outcome = "did the thing"
    if wave_status is WaveStatus.CLOSED:
        wave.closed_at = NOW
    return state


def _write_report(
    state_path: Path,
    *,
    base_id: str,
    verdict: AgentReportVerdict,
    attempt: int = 1,
) -> None:
    """Append one executor report envelope keyed by ``base_id`` to the store."""
    body = ExecutorReportBody(
        role="executor",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="attempt completed",
        wave_id=base_id,
        outcome="done",
    )
    report_id = f"AR-executor-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="codex",
        generated_at=NOW,
        summary=body.summary,
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(AgentSessionRole.EXECUTOR),
        scope_id=base_id,
        created_at=NOW + timedelta(minutes=attempt),
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(AgentSessionRole.EXECUTOR))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


def test_phase_retro_digest_unknown_phase_raises(tmp_path: Path) -> None:
    state = _empty_state()
    with pytest.raises(ValueError, match="unknown phase: 'P99'"):
        phase_retro_digest(state, tmp_path / "state.json", "P99")


def test_phase_retro_digest_no_reports_renders_honest_empty(tmp_path: Path) -> None:
    state = _phase_with_wave()
    state_path = tmp_path / "state.json"

    digest = phase_retro_digest(state, state_path, "P03")

    assert isinstance(digest, PhaseRetroDigest)
    assert digest.wave_count == 1
    assert digest.closed_count == 1
    assert digest.failed_count == 0
    assert digest.reportless_count == 1
    row = digest.waves[0]
    assert row.wave_id == "P03-I01-W01"
    assert row.has_report is False
    assert row.report_verdict is None
    assert row.report_count == 0
    assert row.failed is False
    assert row.outcome == "did the thing"


def test_phase_retro_digest_join_surfaces_report_verdict(tmp_path: Path) -> None:
    state = _phase_with_wave()
    state_path = tmp_path / "state.json"
    _write_report(state_path, base_id="P03-I01-W01", verdict=AgentReportVerdict.PASS)

    digest = phase_retro_digest(state, state_path, "P03")

    assert digest.reportless_count == 0
    row = digest.waves[0]
    assert row.has_report is True
    assert row.report_verdict == AgentReportVerdict.PASS.value
    assert row.report_id == "AR-executor-P03-I01-W01-01"
    assert row.report_count == 1
    assert row.failed is False


def test_phase_retro_digest_picks_latest_report_attempt(tmp_path: Path) -> None:
    state = _phase_with_wave()
    state_path = tmp_path / "state.json"
    _write_report(state_path, base_id="P03-I01-W01", verdict=AgentReportVerdict.BLOCKED, attempt=1)
    _write_report(state_path, base_id="P03-I01-W01", verdict=AgentReportVerdict.PASS, attempt=2)

    digest = phase_retro_digest(state, state_path, "P03")

    row = digest.waves[0]
    assert row.report_count == 2
    # Latest by (created_at, id) is attempt 2 (PASS) -> not failed.
    assert row.report_verdict == AgentReportVerdict.PASS.value
    assert row.failed is False


def test_phase_retro_digest_flags_failed_wave_by_status(tmp_path: Path) -> None:
    state = _phase_with_wave(wave_status=WaveStatus.FAILED)
    state_path = tmp_path / "state.json"

    digest = phase_retro_digest(state, state_path, "P03")

    assert digest.failed_count == 1
    assert digest.closed_count == 0
    row = digest.waves[0]
    assert row.status == WaveStatus.FAILED.value
    assert row.failed is True


def test_phase_retro_digest_flags_failed_wave_by_report_verdict(tmp_path: Path) -> None:
    # Wave is CLOSED, but its report verdict is FAIL -> still flagged failed.
    state = _phase_with_wave(wave_status=WaveStatus.CLOSED)
    state_path = tmp_path / "state.json"
    _write_report(state_path, base_id="P03-I01-W01", verdict=AgentReportVerdict.FAIL)

    digest = phase_retro_digest(state, state_path, "P03")

    assert digest.failed_count == 1
    row = digest.waves[0]
    assert row.status == WaveStatus.CLOSED.value
    assert row.report_verdict == AgentReportVerdict.FAIL.value
    assert row.failed is True


def test_render_phase_retro_markdown_honest_empty(tmp_path: Path) -> None:
    state = _phase_with_wave()
    digest = phase_retro_digest(state, tmp_path / "state.json", "P03")

    md = render_phase_retro_markdown(digest)

    assert "## Phase retro: P03" in md
    assert "1 wave(s): 1 closed, 0 failed, 1 reportless" in md
    assert "| P03-I01-W01 | closed | no report | no | did the thing |" in md


def test_render_phase_retro_markdown_with_report(tmp_path: Path) -> None:
    state = _phase_with_wave()
    state_path = tmp_path / "state.json"
    _write_report(state_path, base_id="P03-I01-W01", verdict=AgentReportVerdict.PASS)
    digest = phase_retro_digest(state, state_path, "P03")

    md = render_phase_retro_markdown(digest)

    assert "| P03-I01-W01 | closed | pass | no |" in md


def test_render_phase_retro_markdown_no_waves(tmp_path: Path) -> None:
    state = _empty_state()
    open_phase(state, phase_id="P03", title="t")
    digest = phase_retro_digest(state, tmp_path / "state.json", "P03")

    md = render_phase_retro_markdown(digest)

    assert "(no waves)" in md
    assert digest.wave_count == 0
