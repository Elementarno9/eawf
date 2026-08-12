"""Unit tests for ``eawf hook dispatch --event-type agent_end``.

The wave deliverable: ``eawf hook dispatch --event-type agent_end`` seeds a
first verdict cohort manually — an ``agent_end`` hook event is translated into
a seeded verdict row in the per-role store that the self-eval + jury surfaces
read. This is the interim / manual producer before the live per-wave verdict
producer lands.

Pins:

- ``--event-type agent_end`` with a valid executor body seeds one verdict row
  into ``executor_report.jsonl``; the row's ``body.verdict`` is the seeded
  verdict and ``compute_self_eval`` sees a cohort of size one.
- A malformed agent_end body (schema mismatch) is rejected with exit 1
  (``InvalidInput``) and writes nothing.
- An unknown session id is rejected with exit 1 (``NotFound``).
- A non-``agent_end`` event type is rejected with exit 1 (``InvalidInput``) —
  the dispatch surface is the verdict seeder, not the general hook dispatcher.
- An empty store before the seed yields an ``insufficient_data`` self-eval
  surface; the seed adds exactly one verdict row (empty-cohort seed boundary).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.observability.eval.self_eval import SelfEvalStatus, compute_self_eval
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.agent_report.rollup import iter_agent_reports

runner = CliRunner()

_SESSION_ID = "SES-001"
_SCOPE_ID = "P18-I01-W04"


def _workspace_with_session(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:QR",
                "updated_at": "2026-05-14T00:00:00Z",
                "project": {
                    "code": "QR",
                    "slug": "qr",
                    "title": "QR",
                    "domains": [],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:QR",
                },
                "current": {
                    "project_code": "QR",
                    "track_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [_SESSION_ID],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {
                    _SESSION_ID: {
                        "id": _SESSION_ID,
                        "role": "executor",
                        "runtime": "generic",
                        "scope_id": _SCOPE_ID,
                        "status": "active",
                        "claimed_wave_ids": [],
                        "worktree_ids": [],
                        "artifact_ids": [],
                        "started_at": "2026-05-14T00:00:00Z",
                        "ended_at": None,
                        "summary": None,
                    }
                },
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _agent_end_payload(verdict: str = "pass") -> dict[str, object]:
    return {
        "session_id": _SESSION_ID,
        "base_id": _SCOPE_ID,
        "body": {
            "role": "executor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "seeded interim verdict",
            "wave_id": _SCOPE_ID,
            "outcome": "seed",
        },
    }


def test_dispatch_agent_end_seeds_verdict_row(tmp_path: Path) -> None:
    """A valid agent_end event seeds one verdict row in the executor store."""
    workspace = _workspace_with_session(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "dispatch", "--event-type", "agent_end"],
        input=json.dumps(_agent_end_payload()),
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["report_id"] == "AR-executor-P18-I01-W04-01"
    assert env.body["store_kind"] == "executor_report"
    assert env.footer.persisted_store_records

    store_file = workspace / ".ea" / "store" / "executor_report.jsonl"
    lines = [ln for ln in store_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    payload = AgentReportPayload.model_validate(Envelope.model_validate_json(lines[0]).payload)
    assert payload.body.verdict is AgentReportVerdict.PASS
    assert payload.header.session_id == _SESSION_ID


def test_dispatch_agent_end_seeds_cohort_self_eval_reads(tmp_path: Path) -> None:
    """The seeded verdict is the cohort the self-eval surface reads."""
    workspace = _workspace_with_session(tmp_path)
    state_path = workspace / ".ea" / "state.json"

    # Empty cohort before the seed: self-eval refuses to score.
    before = compute_self_eval(state_path)
    assert before.status is SelfEvalStatus.INSUFFICIENT_DATA
    assert before.cohort_size == 0

    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "dispatch", "--event-type", "agent_end"],
        input=json.dumps(_agent_end_payload()),
    )
    assert result.exit_code == 0, result.stdout

    rows = iter_agent_reports(state_path)
    assert [row.payload.body.verdict for row in rows] == [AgentReportVerdict.PASS]

    after = compute_self_eval(state_path)
    assert after.cohort_size == 1
    assert after.verdict_breakdown == {"pass": 1}


def test_dispatch_agent_end_malformed_body_rejected(tmp_path: Path) -> None:
    """A schema-mismatched agent_end body is rejected and writes nothing."""
    workspace = _workspace_with_session(tmp_path)
    payload = {
        "session_id": _SESSION_ID,
        "base_id": _SCOPE_ID,
        # Executor body missing required wave_id / outcome.
        "body": {"role": "executor", "verdict": "pass", "confidence": "high", "summary": "x"},
    }
    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "dispatch", "--event-type", "agent_end"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 1, result.stdout
    assert not (workspace / ".ea" / "store" / "executor_report.jsonl").exists()


def test_dispatch_agent_end_unknown_session_rejected(tmp_path: Path) -> None:
    """An agent_end event for a missing session is rejected (NotFound)."""
    workspace = _workspace_with_session(tmp_path)
    payload = _agent_end_payload()
    payload["session_id"] = "SES-missing"
    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "dispatch", "--event-type", "agent_end"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 1, result.stdout
    assert not (workspace / ".ea" / "store" / "executor_report.jsonl").exists()


def test_dispatch_non_agent_end_event_rejected(tmp_path: Path) -> None:
    """The dispatch surface seeds verdicts from agent_end only."""
    workspace = _workspace_with_session(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "dispatch", "--event-type", "pre_commit"],
        input="",
    )
    assert result.exit_code == 1, result.stdout


def test_dispatch_unknown_event_type_rejected(tmp_path: Path) -> None:
    """An unrecognised event type is rejected (InvalidInput)."""
    workspace = _workspace_with_session(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "dispatch", "--event-type", "not_a_real_event"],
        input="",
    )
    assert result.exit_code == 1, result.stdout


def test_dispatch_help_shows_event_type_option() -> None:
    """The dispatch subcommand surfaces the --event-type option."""
    # Pin the render width so the option name is never clipped / wrapped.
    # Typer/Click + Rich read ``COLUMNS`` for the help layout; on a CI runner
    # the inherited terminal width is environment-dependent and can be narrow
    # enough that ``--event-type`` wraps mid-token and the substring assertion
    # fails. A wide, fixed width renders the full option text deterministically.
    result = runner.invoke(
        app,
        ["hook", "dispatch", "--help"],
        env={"COLUMNS": "200", "TERM": "dumb"},
    )
    assert result.exit_code == 0
    assert "--event-type" in result.stdout
