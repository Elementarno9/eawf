"""Unit tests for ``eawf skill resume <pause-urn> --choice <label>`` (P26-I02-W07).

Pins the needs_user resume verb:

- A valid pause-urn + a label that matches one of the paused question's
  options resumes (exit 0) and appends a resume row to the event store.
- An unknown / already-resolved pause-urn errors with a non-zero exit.
- A ``--choice`` that is not one of the question's options errors with a
  non-zero (validation-style) exit.

The pause itself is seeded through the shared
:mod:`eawf.workflow.skills.needs_user` library so the test exercises the same
record shape the CLI + TUI both consume.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import (
    RESUME_EVENT_TYPE,
    list_open_pauses,
    record_pause,
    resolve_pause,
)

_SCOPE = "urn:eawf:v1:state:QR"
_SESSION = "urn:eawf:v1:session:cli/SES-resume-test"
_QUESTION = UserQuestion(
    question="Apply the proposed roadmap?",
    options=[
        UserQuestionOption(label="apply", description="apply as-is"),
        UserQuestionOption(label="revise"),
        UserQuestionOption(label="cancel"),
    ],
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Write a minimal placeholder state file under a temp ``.ea`` dir.

    The resume verb only needs the file to exist (its store sibling holds
    the pause rows); the state body is never parsed by the resume path.
    """
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    path.write_bytes(orjson.dumps({"schema_version": "1.0"}))
    return path


def _seed_pause(state_path: Path) -> str:
    return record_pause(state_path, scope_id=_SCOPE, session=_SESSION, question=_QUESTION)


def test_resume_cmd_valid_choice_resumes(state_path: Path) -> None:
    pause_urn = _seed_pause(state_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "resume", pause_urn, "--choice", "revise", "-w", str(state_path.parent.parent)],
    )
    assert result.exit_code == 0, result.stdout
    assert pause_urn in result.stdout
    # The pause is now resolved — no longer open.
    assert list_open_pauses(state_path, scope_id=_SCOPE) == []


def test_resume_cmd_appends_resume_event(state_path: Path) -> None:
    pause_urn = _seed_pause(state_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "resume", pause_urn, "--choice", "apply", "-w", str(state_path.parent.parent)],
    )
    assert result.exit_code == 0, result.stdout
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    resume_rows = [
        line for line in events if orjson.loads(line)["payload"]["event_type"] == RESUME_EVENT_TYPE
    ]
    assert len(resume_rows) == 1
    assert orjson.loads(resume_rows[0])["payload"]["extras"]["choice"] == "apply"


def test_resume_cmd_unknown_urn_errors(state_path: Path) -> None:
    _seed_pause(state_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "skill",
            "resume",
            "urn:eawf:v1:event:QR/needs-user-deadbeef",
            "--choice",
            "apply",
            "-w",
            str(state_path.parent.parent),
        ],
    )
    assert result.exit_code != 0
    assert "unknown or already-resolved pause" in result.stdout


def test_resume_cmd_invalid_choice_errors(state_path: Path) -> None:
    pause_urn = _seed_pause(state_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "resume", pause_urn, "--choice", "nope", "-w", str(state_path.parent.parent)],
    )
    assert result.exit_code != 0
    assert "invalid choice" in result.stdout
    # The pause stays open after a rejected choice.
    assert len(list_open_pauses(state_path, scope_id=_SCOPE)) == 1


def test_resume_cmd_already_resolved_errors(state_path: Path) -> None:
    pause_urn = _seed_pause(state_path)
    resolve_pause(state_path, pause_urn=pause_urn, choice="apply")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "resume", pause_urn, "--choice", "revise", "-w", str(state_path.parent.parent)],
    )
    assert result.exit_code != 0
    assert "unknown or already-resolved pause" in result.stdout
