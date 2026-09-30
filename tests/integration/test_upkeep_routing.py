"""The epoch-1 upkeep verbs write through the daemon, and nothing writes around it.

Session close and recover and worktree merge-back and cleanup must keep
working on an epoch-1 tree mid-migration. The daemon writes them; the
in-process writer runs only under the explicit daemonless carve-out, an
unreachable daemon is an error rather than a cue to write locally, and a
tree carrying the epoch marker is refused before anything is read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.app import app
from tests.integration._memory_native import QR_DOCUMENT
from tests.integration._upkeep_daemon import upkeep_daemon  # noqa: F401

pytestmark = pytest.mark.integration

runner = CliRunner()


def _session(session_id: str, started_at: datetime) -> dict[str, Any]:
    return {
        "id": session_id,
        "role": "executor",
        "runtime": "claude",
        "scope_id": "QR",
        "status": "active",
        "claimed_wave_ids": [],
        "worktree_ids": [],
        "artifact_ids": [],
        "started_at": started_at.isoformat(),
        "ended_at": None,
        "summary": None,
    }


@pytest.fixture
def epoch1_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An epoch-1 tree holding one live and one long-silent session."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    now = datetime.now(UTC)
    body = json.loads(json.dumps(QR_DOCUMENT))
    body["current"]["active_session_ids"] = ["SES-LIVE", "SES-OLD"]
    body["agent_sessions"] = {
        "SES-LIVE": _session("SES-LIVE", now),
        "SES-OLD": _session("SES-OLD", now - timedelta(hours=2)),
    }
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def _event_types(state_path: Path) -> list[str]:
    events = store_path(state_path, StoreKind.EVENT)
    if not events.exists():
        return []
    return [
        json.loads(line)["payload"]["event_type"]
        for line in events.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sessions(state_path: Path) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))["agent_sessions"]
    return body


@pytest.mark.usefixtures("upkeep_daemon")
def test_session_close_is_written_by_the_daemon(epoch1_tree: Path) -> None:
    result = runner.invoke(app, ["--json", "session", "close", "SES-LIVE"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "closed"
    assert _sessions(epoch1_tree)["SES-LIVE"]["status"] == "closed"
    events = _event_types(epoch1_tree)
    assert "session.close" in events
    assert "state.session_close" in events


@pytest.mark.usefixtures("upkeep_daemon")
def test_session_recover_is_written_by_the_daemon(epoch1_tree: Path) -> None:
    result = runner.invoke(app, ["--json", "session", "recover", "--age", "30"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["marked_session_ids"] == ["SES-OLD"]
    assert _sessions(epoch1_tree)["SES-OLD"]["status"] == "stale"
    events = _event_types(epoch1_tree)
    assert "session.recover.summary" in events
    assert "state.session_recover" in events


@pytest.mark.usefixtures("upkeep_daemon")
def test_a_refusal_keeps_its_class_and_kind(epoch1_tree: Path) -> None:
    """Error path: a missing session is NotFound, not a flattened wire error."""
    result = runner.invoke(app, ["--json", "session", "close", "SES-MISSING"])
    assert result.exit_code == 1, result.output
    assert '"kind": "NotFound"' in result.output


def test_the_daemonless_carve_out_writes_in_process(
    epoch1_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    def _no_daemon(*_a: object, **_k: object) -> None:
        raise AssertionError("the carve-out must not reach for a daemon")

    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", _no_daemon)
    result = runner.invoke(app, ["--json", "session", "close", "SES-LIVE"])
    assert result.exit_code == 0, result.output
    assert _sessions(epoch1_tree)["SES-LIVE"]["status"] == "closed"
    assert "state.session_close" not in _event_types(epoch1_tree)


def test_an_unreachable_daemon_writes_nothing(
    epoch1_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: no silent in-process write when the daemon cannot be reached."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)

    def _unreachable(*_a: object, **_k: object) -> None:
        raise cli_errors.DaemonUnreachable("daemon auto-spawn failed: test")

    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", _unreachable)
    before = epoch1_tree.read_bytes()
    result = runner.invoke(app, ["session", "close", "SES-LIVE"])
    assert result.exit_code != 0, result.output
    assert epoch1_tree.read_bytes() == before


@pytest.mark.usefixtures("upkeep_daemon")
def test_a_tree_carrying_the_epoch_marker_is_refused(epoch1_tree: Path) -> None:
    """Error path: the daemon never writes the frozen document of an epoch-2 tree."""
    marker = epoch1_tree.parent / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    before = epoch1_tree.read_bytes()
    result = runner.invoke(app, ["session", "recover"])
    assert result.exit_code == 2, result.output
    assert "legacy_operation_removed" in result.output
    assert epoch1_tree.read_bytes() == before
