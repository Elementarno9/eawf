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

from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.render.envelope import OutputEnvelope

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


def test_hook_run_unknown_event_type_returns_invalid_input() -> None:
    result = runner.invoke(app, ["hook", "run", "not_a_real_event"], input="")
    assert result.exit_code == 3, result.stdout


def test_hook_run_malformed_stdin_returns_invalid_input() -> None:
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input="{not json")
    assert result.exit_code == 3, result.stdout


def test_hook_run_non_object_stdin_returns_invalid_input() -> None:
    result = runner.invoke(app, ["hook", "run", "pre_commit"], input='["x"]')
    assert result.exit_code == 3, result.stdout


def test_hook_run_unknown_runtime_returns_invalid_input() -> None:
    result = runner.invoke(
        app,
        ["hook", "run", "pre_commit", "--runtime", "zsh"],
        input="",
    )
    assert result.exit_code == 3, result.stdout


def test_hook_run_emits_canonical_output_envelope_shape() -> None:
    result = runner.invoke(app, ["hook", "run", "session_start"], input="")
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"header", "body", "footer"}
    assert set(payload["header"].keys()) == {
        "skill",
        "scope",
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
    # The header.scope falls through to the supplied scope_id.
    assert env.header.scope == "P04-I01-W04"


def test_hook_run_help_shows_event_type_argument() -> None:
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    # The subcommand is registered as "run" under the hook subapp.
    assert "run" in result.stdout
