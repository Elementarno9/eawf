"""Unit tests for ``eawf wave verify-commits [--repair]`` (P29-I02-W06).

Exercises the CLI verb end-to-end against an isolated ``state.json``
fixture under ``EA_STATE``. ``derive_wave_sha`` + ``shutil.which`` are
monkeypatched so the scan is deterministic without a real git repo
(mirroring ``tests/unit/test_drift_reconciler.py``).

Covered:

- clean repo -> exit 0, "0 drift".
- each repairable / unrepairable kind is detected + classified.
- drift without ``--repair`` exits ``VALIDATION_ERROR`` (2) AND still
  prints the per-wave report (so CI can read the detail + branch on the
  code).
- ``--repair`` re-pins a ``closed`` wave's mismatch and an unpinned
  derivable wave, persists through the canonical writer, prints a
  repaired/skipped summary, exits 0.
- an unrepairable kind is reported as skipped under ``--repair``.
- ``--repair`` is idempotent: the second run is clean.
- ``--md`` and ``--json`` render the report; ``--md --json`` is rejected.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

W1 = "P28-I01-W01"
W2 = "P28-I01-W02"


def _seed_state(state_dir: Path, *, waves: dict[str, str | None]) -> Path:
    """Write a state.json whose closed waves carry the given commit pins.

    Args:
        state_dir: ``.ea`` directory to write ``state.json`` into.
        waves: ``{wave_id: commit_or_None}`` -- each entry becomes a
            CLOSED wave with that ``Wave.commit`` value.
    """
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
                    "description": None,
                    "domains": [],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:ZZ",
                },
                "current": {
                    "project_code": "ZZ",
                    "subproject_id": None,
                    "phase_id": "P28",
                    "iter_id": "P28-I01",
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {
                    "P28": {
                        "id": "P28",
                        "scope_id": "ZZ",
                        "subproject_id": None,
                        "title": "bootstrap",
                        "status": "active",
                        "iter_ids": ["P28-I01"],
                        "outcome_ids": [],
                        "opened_at": "2026-05-27T00:00:00Z",
                        "closed_at": None,
                        "audit_id": None,
                    }
                },
                "iters": {
                    "P28-I01": {
                        "id": "P28-I01",
                        "phase_id": "P28",
                        "title": "first",
                        "status": "active",
                        "wave_ids": list(wave_rows),
                        "estimate_id": None,
                        "audit_id": None,
                        "opened_at": "2026-05-27T00:00:00Z",
                        "closed_at": None,
                    }
                },
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
def git_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    # ``scan_commit_pins`` now builds a shared SHA index once (W10). These
    # tests patch ``derive_wave_sha`` directly, so stub the builder to an
    # empty map -- no live ``git log`` runs and the patched derive answers.
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )


def _patch_derive(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: table.get(wid),
    )


def _read_commit(state_path: Path, wave_id: str) -> str | None:
    payload = orjson.loads(state_path.read_bytes())
    return payload["waves"][wave_id]["commit"]


# ---- read-only (no --repair) -----------------------------------------------


def test_verify_commits_clean_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """All pins in sync -> exit 0, honest no-drift line."""
    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "a" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits"])
    assert res.exit_code == 0, res.output
    assert "0 drift" in res.output


def test_verify_commits_drift_exits_validation_error_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """A mismatch without --repair exits 2 AND prints the per-wave row."""
    from eawf.surfaces.cli import exit_codes

    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits"])
    assert res.exit_code == exit_codes.VALIDATION_ERROR, res.output
    # The full report is printed BEFORE the non-zero exit.
    assert W1 in res.output
    assert "pinned_mismatch" in res.output
    # Read-only: the on-disk pin is unchanged.
    assert _read_commit(state_path, W1) == "a" * 40


def test_verify_commits_detects_every_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """All four hard kinds + the soft unpinned_derivable kind classify in --json."""
    state_path = _seed_state(
        tmp_path / ".ea",
        waves={
            "P28-I01-W01": "a" * 40,  # mismatch (derive -> b)
            "P28-I01-W02": "c" * 40,  # pinned_but_missing (derive -> None)
            "P28-I01-W03": None,  # closed_no_pin (derive -> None)
            "P28-I01-W04": None,  # unpinned_derivable (derive -> e)
        },
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(
        monkeypatch,
        {"P28-I01-W01": "b" * 40, "P28-I01-W04": "e" * 40},
    )

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits"])
    assert res.exit_code != 0, res.output  # drift -> non-zero
    payload = json.loads(res.stdout)
    kinds = {row["wave"]: row["kind"] for row in payload["drifts"]}
    assert kinds == {
        "P28-I01-W01": "pinned_mismatch",
        "P28-I01-W02": "pinned_but_missing",
        "P28-I01-W03": "closed_no_pin",
        "P28-I01-W04": "unpinned_derivable",
    }
    repairable = {row["wave"]: row["repairable"] for row in payload["drifts"]}
    assert repairable == {
        "P28-I01-W01": True,
        "P28-I01-W02": False,
        "P28-I01-W03": False,
        "P28-I01-W04": True,
    }


def test_verify_commits_closed_unfindable_when_git_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git off PATH classifies an unpinned closed wave as closed_unfindable."""
    state_path = _seed_state(tmp_path / ".ea", waves={W1: None})
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: None)

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits"])
    payload = json.loads(res.stdout)
    assert payload["drifts"][0]["kind"] == "closed_unfindable"


def test_verify_commits_md_renders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits", "--md"])
    assert "| wave | kind |" in res.output
    assert "pinned_mismatch" in res.output


def test_verify_commits_md_and_json_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    from eawf.surfaces.cli import exit_codes

    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "a" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits", "--md"])
    assert res.exit_code == exit_codes.USER_ERROR, res.output


# ---- --repair --------------------------------------------------------------


def test_verify_commits_repair_repins_mismatch_and_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """--repair pins both repairable kinds through the canonical writer."""
    state_path = _seed_state(
        tmp_path / ".ea",
        waves={W1: "a" * 40, W2: None},  # mismatch + unpinned_derivable
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40, W2: "c" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits", "--repair"])
    assert res.exit_code == 0, res.output
    assert "2 re-pinned" in res.output
    # The on-disk state now carries the git-derived SHAs.
    assert _read_commit(state_path, W1) == "b" * 40
    assert _read_commit(state_path, W2) == "c" * 40


def test_verify_commits_repair_reports_unrepairable_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """--repair skips kinds with no derivable SHA and names them."""
    state_path = _seed_state(
        tmp_path / ".ea",
        waves={W1: "a" * 40, W2: None},
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    # W1 pinned_but_missing (derive None); W2 unpinned_derivable (derive -> c).
    _patch_derive(monkeypatch, {W2: "c" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits", "--repair"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["repaired_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["repaired"][0]["wave"] == W2
    assert payload["skipped"][0]["wave"] == W1
    assert payload["skipped"][0]["kind"] == "pinned_but_missing"
    # W1's unreachable pin is left intact; W2 hardened.
    assert _read_commit(state_path, W1) == "a" * 40
    assert _read_commit(state_path, W2) == "c" * 40


def test_verify_commits_repair_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """A second --repair (then a read-only verify) finds nothing to do."""
    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40})

    from eawf.surfaces.cli.app import app

    runner = CliRunner()
    first = runner.invoke(app, ["wave", "verify-commits", "--repair"])
    assert first.exit_code == 0, first.output
    assert _read_commit(state_path, W1) == "b" * 40

    # Second repair: pin now equals derived -> nothing repaired.
    second = runner.invoke(app, ["--json", "wave", "verify-commits", "--repair"])
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["repaired_count"] == 0
    assert payload["skipped_count"] == 0

    # And a read-only verify is now clean (exit 0).
    verify = runner.invoke(app, ["wave", "verify-commits"])
    assert verify.exit_code == 0, verify.output
    assert "0 drift" in verify.output
