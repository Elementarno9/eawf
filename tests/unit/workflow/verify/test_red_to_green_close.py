"""Wave close runs the red-to-green check over an executor report's test runs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentReportVerdict, Confidence
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportPayload,
    ExecutorReportBody,
    ExecutorTestRun,
    PlannerReportBody,
)
from eawf.runtime.daemon.dispatch_runner import emit_agent_end_report
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.agents.specs.models import SubagentSpec
from eawf.workflow.verify.dispatch_close import (
    DispatchCloseBlockedError,
    verify_close_readiness,
)
from eawf.workflow.verify.red_to_green import red_to_green_close_findings

_WAVE_ID = "P01-I01-W01"
_SESSION_ID = "SES-executor-w01"
_UNIT_TEST = "tests/unit/test_x.py::test_x"
_REPRO_TEST = "tests/unit/test_x.py::test_x_repro_42"


def _run(test_id: str, outcome: str, revision: str = "abcdef1") -> dict[str, str]:
    return {"test_id": test_id, "outcome": outcome, "revision": revision}


def _report(*runs: dict[str, str], **overrides: Any) -> ExecutorReportBody:
    payload: dict[str, Any] = {
        "role": "executor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "done",
        "wave_id": _WAVE_ID,
        "outcome": "done",
        "commit_sha": "abcdef1",
        "test_runs": list(runs),
    }
    payload.update(overrides)
    return ExecutorReportBody.model_validate(payload)


# ---- ExecutorTestRun schema ------------------------------------------------


def test_executor_report_body_parses_without_test_runs() -> None:
    body = _report()
    body_without_field = ExecutorReportBody.model_validate(
        {k: v for k, v in body.model_dump(mode="json").items() if k != "test_runs"}
    )
    assert body_without_field.test_runs == []


def test_executor_test_run_rejects_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate(_run(_UNIT_TEST, "yellow"))


def test_executor_test_run_rejects_empty_test_id() -> None:
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate(_run("", "red"))


def test_executor_test_run_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate({**_run(_UNIT_TEST, "red"), "duration": 1})


def test_executor_test_run_accepts_max_length_test_id() -> None:
    assert ExecutorTestRun.model_validate(_run("t" * 500, "red")).test_id == "t" * 500
    with pytest.raises(ValidationError):
        ExecutorTestRun.model_validate(_run("t" * 501, "red"))


# ---- red_to_green_close_findings ------------------------------------------


def test_red_to_green_close_findings_empty_runs_raise_nothing() -> None:
    assert red_to_green_close_findings(_report()) == ()


def test_red_to_green_close_findings_red_then_green_passes() -> None:
    report = _report(_run(_UNIT_TEST, "red", "worktree"), _run(_UNIT_TEST, "green"))
    assert red_to_green_close_findings(report) == ()


def test_red_to_green_close_findings_single_green_is_advisory() -> None:
    (finding,) = red_to_green_close_findings(_report(_run(_UNIT_TEST, "green")))
    assert not finding.blocking
    assert finding.finding.test_id == _UNIT_TEST
    assert "has no earlier red run" in finding.render()
    assert finding.render().endswith("(advisory)")


def test_red_to_green_close_findings_repro_without_red_blocks() -> None:
    (finding,) = red_to_green_close_findings(_report(_run(_REPRO_TEST, "green")))
    assert finding.blocking
    assert finding.render().endswith("(blocks close)")


def test_red_to_green_close_findings_groups_runs_per_test() -> None:
    report = _report(
        _run(_UNIT_TEST, "red", "worktree"),
        _run(_REPRO_TEST, "green"),
        _run(_UNIT_TEST, "green"),
    )
    findings = red_to_green_close_findings(report)
    assert [f.finding.test_id for f in findings] == [_REPRO_TEST]


def test_red_to_green_close_findings_skips_tests_above_wave_tier() -> None:
    report = _report(
        _run("tests/golden/test_render.py::test_render", "green"),
        _run("tests/e2e/test_flow.py::test_flow", "green"),
    )
    assert red_to_green_close_findings(report) == ()


def test_red_to_green_close_findings_checks_tests_outside_kind_dirs() -> None:
    (finding,) = red_to_green_close_findings(_report(_run("tests/lint/test_a.py::test_a", "green")))
    assert finding.finding.test_id == "tests/lint/test_a.py::test_a"


def test_red_to_green_close_findings_flags_final_red_run() -> None:
    report = _report(
        _run(_UNIT_TEST, "red", "worktree"),
        _run(_UNIT_TEST, "green"),
        _run(_UNIT_TEST, "red"),
    )
    (finding,) = red_to_green_close_findings(report)
    assert "is red" in finding.finding.reason


def test_red_to_green_close_findings_rejects_non_executor_body() -> None:
    planner = PlannerReportBody(
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="plan",
        objective="plan",
    )
    with pytest.raises(TypeError, match="ExecutorReportBody"):
        red_to_green_close_findings(planner)  # type: ignore[arg-type]


# ---- verify_close_readiness wiring ----------------------------------------


def test_verify_close_readiness_flags_green_without_red_as_advisory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="eawf.workflow.verify.dispatch_close"):
        result = verify_close_readiness(_WAVE_ID, _report(_run(_UNIT_TEST, "green")))
    assert result.passed
    assert len(result.advisories) == 1
    assert _UNIT_TEST in result.advisories[0]
    assert "advisory=" in caplog.text


def test_verify_close_readiness_blocks_repro_without_red() -> None:
    result = verify_close_readiness(_WAVE_ID, _report(_run(_REPRO_TEST, "green")))
    assert not result.passed
    assert any(_REPRO_TEST in reason for reason in result.reasons)
    assert result.advisories == ()


def test_verify_close_readiness_clean_pair_has_no_advisory() -> None:
    report = _report(_run(_REPRO_TEST, "red", "worktree"), _run(_REPRO_TEST, "green"))
    result = verify_close_readiness(_WAVE_ID, report)
    assert result.passed
    assert result.advisories == ()


# ---- dispatch close path ---------------------------------------------------


def _write_state(tmp_path: Path) -> Path:
    stamp = "2026-09-25T00:00:00Z"
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": stamp,
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
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": [],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P01": {
                "id": "P01",
                "scope_id": "EAWF",
                "title": "Phase",
                "status": "active",
                "iter_ids": ["P01-I01"],
                "outcome_ids": [],
                "opened_at": stamp,
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P01-I01": {
                "id": "P01-I01",
                "phase_id": "P01",
                "title": "Iter",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": stamp,
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P01-I01",
                "title": "Fix a defect",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/x.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "S",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": stamp,
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
                "started_at": stamp,
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    return path


def _emit(state_path: Path, report: ExecutorReportBody) -> str:
    ctx = MethodContext(
        started_at="2026-09-25T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0",
        bus=None,
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )
    return emit_agent_end_report(
        ctx,
        session_id=_SESSION_ID,
        wave_id=_WAVE_ID,
        commit_sha="abcdef1",
        outcome="done",
        runtime="claude",
        report_body=report,
    )


def test_emit_agent_end_report_blocks_repro_test_that_never_ran_red(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path)
    with pytest.raises(DispatchCloseBlockedError, match="red-to-green"):
        _emit(state_path, _report(_run(_REPRO_TEST, "green")))


def test_emit_agent_end_report_persists_test_runs_and_closes_on_advisory(
    tmp_path: Path,
) -> None:
    state_path = _write_state(tmp_path)
    assert _emit(state_path, _report(_run(_UNIT_TEST, "green")))
    store = state_path.parent / "store" / "executor_report.jsonl"
    line = store.read_text(encoding="utf-8").splitlines()[-1]
    payload = AgentReportPayload.model_validate(Envelope.model_validate_json(line).payload)
    assert isinstance(payload.body, ExecutorReportBody)
    assert [run.outcome for run in payload.body.test_runs] == ["green"]


# ---- executor prompt contract ----------------------------------------------


def test_render_headless_executor_asks_for_test_runs() -> None:
    spec = SubagentSpec.model_validate(
        {
            "wave_id": _WAVE_ID,
            "iter_id": "P01-I01",
            "title": "Solo wave",
            "scope_id": "QR",
            "agent_role": "executor",
            "file_scopes": ["src/"],
        }
    )
    out = spec.render(headless=True)
    assert '"test_runs": [{"test_id"' in out
    assert "never ran red first" in out
