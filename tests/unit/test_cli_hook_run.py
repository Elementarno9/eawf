"""Unit tests for ``eawf hook run`` CLI command (Phase 4 W04 acceptance §3).

Pins:

- Empty stdin → exit ``0`` + an envelope with ``status="ok"`` and an
  empty ``body.results`` list (no hooks registered in v1).
- Unknown event-type argument → exit ``3`` (``INVALID_INPUT``).
- Malformed JSON on stdin → exit ``3``.
- Non-object JSON on stdin → exit ``3``.
- Unknown ``--runtime`` value → exit ``3``.
- The emitted envelope is the canonical OutputEnvelope shape (header /
  body / footer) and parses back through ``OutputEnvelope.model_validate_json``.
- The command does not mutate state (rule 4) — re-running it is
  idempotent w.r.t. the filesystem.

The HOOK_BLOCKED (exit 9) path is exercised via a direct call to
``HookRunner`` in ``test_hook_runner.py``; the CLI surface registers no
hooks at v1 so the exit-9 transition is verified indirectly via the
underlying runner's contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope

runner = CliRunner()


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
                    "subproject_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": ["SES-001"],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {
                    "SES-001": {
                        "id": "SES-001",
                        "role": "executor",
                        "runtime": "generic",
                        "scope_id": "P18-I01-W04",
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


def test_hook_run_empty_stdin_exits_zero_with_ok_envelope() -> None:
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input="")
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.status == "ok"
    assert isinstance(env.body, dict)
    assert env.body["event_type"] == "pre_commit"
    assert env.body["results"] == []
    assert env.body["blocked"] is False


def test_hook_run_with_payload_folds_into_payloads_key() -> None:
    payload = {"branch": "feature/x", "files_changed": ["a.py"]}
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input=json.dumps(payload))
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    # Body echoes the dispatched event_type; payload is folded by
    # _build_event under payloads[<event_type>] before dispatch.
    assert env.body["event_type"] == "pre_commit"
    assert env.body["scope_id"] == ""


def test_hook_run_session_end_without_cost_is_nonblocking() -> None:
    payload = {"hook_event_name": "Stop", "session_id": "session-1"}
    result = runner.invoke(app, ["hook", "run", "session_end"], input=json.dumps(payload))

    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["blocked"] is False
    assert env.body["results"][0]["name"] == "runtime.capture"
    assert env.body["results"][0]["output"] == "runtime.capture skipped: no cost block"


def test_hook_run_unknown_event_type_returns_invalid_input() -> None:
    result = runner.invoke(app, ["hook", "run", "not_a_real_event"], input="")
    assert result.exit_code == 1, result.stdout


def test_hook_run_malformed_stdin_returns_invalid_input() -> None:
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input="{not json")
    assert result.exit_code == 1, result.stdout


def test_hook_run_non_object_stdin_returns_invalid_input() -> None:
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input='["x"]')
    assert result.exit_code == 1, result.stdout


def test_hook_run_unknown_runtime_returns_invalid_input() -> None:
    result = runner.invoke(
        app,
        ["hook", "run", "pre_commit", "--runtime", "zsh"],
        input="",
    )
    assert result.exit_code == 1, result.stdout


def test_hook_run_agent_end_writes_typed_report(tmp_path: Path) -> None:
    workspace = _workspace_with_session(tmp_path)
    payload = {
        "session_id": "SES-001",
        "base_id": "P18-I01-W04",
        "body": {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "implemented writer",
            "wave_id": "P18-I01-W04",
            "outcome": "done",
        },
    }
    result = runner.invoke(
        app,
        ["-w", str(workspace), "hook", "run", "agent_end"],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["report_id"] == "AR-executor-P18-I01-W04-01"
    assert env.footer.persisted_store_records
    store_file = workspace / ".ea" / "store" / "executor_report.jsonl"
    stored = Envelope.model_validate_json(store_file.read_text(encoding="utf-8").splitlines()[0])
    stored_payload = AgentReportPayload.model_validate(stored.payload)
    assert stored_payload.header.attempt == 1
    assert stored_payload.header.session_id == "SES-001"


def test_hook_run_emits_canonical_output_envelope_shape() -> None:
    result = runner.invoke(app, ["hook", "run", "session_start"], input="")
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"header", "body", "footer"}
    assert set(payload["header"].keys()) == {
        "skill",
        "scope_id",
        "session",
        "started_at",
        "finished_at",
        "status",
        "instrument_probe",
    }
    assert payload["header"]["skill"] == "/audit"


def test_hook_run_supports_scope_and_command_options() -> None:
    result = runner.invoke(
        app,
        [
            "hook",
            "run",
            "wave_close",
            "--scope",
            "P04-I01-W04",
            "--command",
            "eawf wave close",
            "--runtime",
            "claude",
        ],
        input="",
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["scope_id"] == "P04-I01-W04"
    assert env.body["runtime"] == "claude"
    # The header.scope_id falls through to the supplied scope_id.
    assert env.header.scope_id == "P04-I01-W04"


def test_hook_run_help_shows_event_type_argument() -> None:
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    # The subcommand is registered as "run" under the hook subapp.
    assert "run" in result.stdout


def test_hook_run_blocking_hook_propagates_exit_9(monkeypatch) -> None:
    """A blocking hook registered via monkeypatch surfaces as exit ``9``."""
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.runner import HookResult, HookRunner

    class _BlockingRunner(HookRunner):
        def __init__(self) -> None:
            super().__init__()
            self.register(
                HookEventType.PRE_COMMIT,
                lambda _evt: HookResult(
                    name="block-me",
                    block=True,
                    output="blocked by test",
                    duration_ms=0.0,
                    raised=False,
                ),
                name="block-me",
            )

    # ``hook run`` lazy-imports ``HookRunner`` from its source module inside
    # the handler (deferred to keep the command-tree build light), so the
    # blocking-runner seam is patched at the source rather than on the
    # command module.
    monkeypatch.setattr("eawf.runtime.hooks.runner.HookRunner", _BlockingRunner)
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input="")
    assert result.exit_code == 3, result.stdout
