"""Unit tests for ``eawf wave ack-drift``.

Exercises the verb end-to-end against an isolated ``state.json`` under a
``-w`` workspace whose repo root carries a ``.git`` marker (so
``_resolve_repo_root_for_drift`` returns it without a real git shell-out).
``derive_wave_sha`` + ``shutil.which`` are monkeypatched so the drift scan is
deterministic without a real git repo.

Covered:

- acking an explicit wave id writes ``<repo>/.eawf/drift-acks.json`` and the
  json envelope reports the new ack.
- ``--all`` acks every currently-drifting closed wave.
- the verb is additive + idempotent (re-acking is a byte-stable no-op).
- invalid wave ids and the ids-plus-``--all`` combination are rejected.
- after acking, ``eawf doctor`` stops warning on the acked drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

W1 = "P28-I01-W01"
W2 = "P28-I01-W02"


def _seed_state(workspace: Path, *, waves: dict[str, str | None]) -> Path:
    """Write a ``state.json`` + ``.git`` marker under *workspace*."""
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    wave_rows = {
        wid: {
            "id": wid,
            "iter_id": "P28-I01",
            "title": f"wave {wid}",
            "status": "closed",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": "done",
            "commit": commit,
            "opened_at": "2026-05-27T00:00:00Z",
            "closed_at": "2026-05-27T00:01:00Z",
        }
        for wid, commit in waves.items()
    }
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:ZZ",
                "updated_at": "2026-05-27T00:00:00Z",
                "project": {
                    "code": "ZZ",
                    "slug": "zz",
                    "title": "ZZ",
                    "description": "",
                    "domains": [],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:ZZ",
                },
                "current": {
                    "project_code": "ZZ",
                    "track_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": wave_rows,
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    return state_path


@pytest.fixture
def git_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git on PATH, empty index, and a derive that finds nothing (all drift)."""
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )


def _ack_path(workspace: Path) -> Path:
    return workspace / ".eawf" / "drift-acks.json"


def test_ack_drift_explicit_wave_writes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    _seed_state(tmp_path, waves={W1: "a" * 40, W2: "b" * 40})
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "-w", str(tmp_path), "wave", "ack-drift", W1])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["newly_acked"] == [W1]
    assert payload["total_acked"] == 1
    ack_file = _ack_path(tmp_path)
    assert ack_file.exists()
    assert json.loads(ack_file.read_text())["acked_wave_ids"] == [W1]


def test_ack_drift_all_acks_every_drifting_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    _seed_state(tmp_path, waves={W1: "a" * 40, W2: "b" * 40})
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "-w", str(tmp_path), "wave", "ack-drift", "--all"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert sorted(payload["acked"]) == [W1, W2]
    assert json.loads(_ack_path(tmp_path).read_text())["acked_wave_ids"] == [W1, W2]


def test_ack_drift_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    _seed_state(tmp_path, waves={W1: "a" * 40})
    from eawf.surfaces.cli.app import app

    runner = CliRunner()
    runner.invoke(app, ["-w", str(tmp_path), "wave", "ack-drift", W1])
    body_one = _ack_path(tmp_path).read_text()
    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "wave", "ack-drift", W1])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["newly_acked"] == []
    assert _ack_path(tmp_path).read_text() == body_one


def test_ack_drift_rejects_invalid_wave_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    _seed_state(tmp_path, waves={W1: "a" * 40})
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["-w", str(tmp_path), "wave", "ack-drift", "not-a-wave"])
    assert res.exit_code != 0
    assert "invalid wave id" in res.output


def test_ack_drift_rejects_ids_and_all_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    _seed_state(tmp_path, waves={W1: "a" * 40})
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["-w", str(tmp_path), "wave", "ack-drift", "--all", W1])
    assert res.exit_code != 0
    assert "OR --all" in res.output or "not both" in res.output


def test_ack_drift_rejects_no_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    _seed_state(tmp_path, waves={W1: "a" * 40})
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["-w", str(tmp_path), "wave", "ack-drift"])
    assert res.exit_code != 0
    assert "no wave ids" in res.output


def test_doctor_stops_warning_after_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_drift: None
) -> None:
    """End-to-end: ack the drift, then ``doctor`` reports git_state_drift ok."""
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "probe.json"))
    _seed_state(tmp_path, waves={W1: "a" * 40})
    from eawf.surfaces.cli.app import app

    runner = CliRunner()
    # Before ack: doctor warns on the drift.
    before = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    payload = json.loads(before.output)
    drift = next(c for c in payload["checks"] if c["name"] == "git_state_drift")
    assert drift["status"] == "warn"

    # Ack it.
    runner.invoke(app, ["-w", str(tmp_path), "wave", "ack-drift", "--all"])

    # After ack: the drift row is ok.
    after = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    payload = json.loads(after.output)
    drift = next(c for c in payload["checks"] if c["name"] == "git_state_drift")
    assert drift["status"] == "ok"
