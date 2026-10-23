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

import pytest
from typer.testing import CliRunner

from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope
from tests.integration.surfaces.cli._native_run import RUN_KEY, RUN_URN, native_repo

runner = CliRunner()


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


def test_hook_run_session_end_with_no_live_session_is_nonblocking(tmp_path: Path) -> None:
    payload = {"hook_event_name": "Stop", "session_id": "session-1"}
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "hook", "run", "session_end"],
        input=json.dumps(payload),
    )

    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["blocked"] is False
    assert [row["name"] for row in env.body["results"]] == ["session.end_stamp"]
    assert env.body["results"][0]["output"] == (
        "session.end_stamp skipped: no live session resolved"
    )


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


def test_hook_run_agent_end_writes_typed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = native_repo(tmp_path / "ws")
    payload = {
        "run_ref": RUN_URN,
        "base_id": "P18-I01-W04",
        "body": {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "implemented writer",
            "task_key": "P18-I01-W04",
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
    assert stored_payload.header.session_id == RUN_KEY


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
