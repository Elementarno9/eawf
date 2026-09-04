"""CLI tests for ``eawf iter close --archive-specs`` + the phase escape hatch.

Two close-time deliverables land in P30-I14-W09:

* ``eawf iter close --archive-specs`` runs a post-close cascade: every wave
  spec under the iter is git-removed, its cache row flipped to ``ARCHIVED``,
  and its blob SHA recorded so ``eawf spec show <urn> --from-git`` recovers
  the body. WITHOUT the flag the specs stay untouched.
* :func:`~eawf.workflow.lifecycle.spec_archive.archive_phase_specs` is the
  phase-level escape hatch (back-fill): one call archives every remaining
  (non-``ARCHIVED``) spec under a phase, reusing the same force path.

Both reuse the P30-I14-W08 daemon force-archive path
(:func:`eawf.runtime.daemon.methods.spec.archive` with ``force=True``),
driven in-process so the tests need no live daemon. The close itself runs
under ``EAWF_DAEMONLESS=1`` so the in-process WAL-backed mutation path
applies.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec import cache as spec_cache
from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.state.models import State
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.spec import _inprocess_init
from eawf.workflow.lifecycle.spec_archive import archive_phase_specs

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 10, tzinfo=UTC)
_PHASE = "P30"
_ITER = "P30-I14"
_WAVE_A = "P30-I14-W01"
_WAVE_B = "P30-I14-W02"
_REPO_CODE = "EAWF"


def _git(repo_root: Path, *args: str) -> None:
    """Run a git command in *repo_root* with check=True."""
    subprocess.run(["git", *args], cwd=repo_root, check=True)


def _init_git_repo(repo_root: Path) -> None:
    """Initialise a deterministic, gpg-free git repo at *repo_root*."""
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "--quiet", "-b", "main")
    _git(repo_root, "config", "user.email", "ci@example.invalid")
    _git(repo_root, "config", "user.name", "ci")
    _git(repo_root, "config", "commit.gpgsign", "false")


def _state_payload() -> dict[str, Any]:
    """A minimal valid State: one ACTIVE phase + iter with two CLOSED waves.

    Both waves are CLOSED so ``iter close`` clears the no-open-waves gate; the
    iter is ACTIVE and is the current iter so the close is the legal edge.
    """

    def _wave(wave_id: str) -> dict[str, Any]:
        return {
            "id": wave_id,
            "iter_id": _ITER,
            "title": "deliver the wave",
            "status": "closed",
            "file_scopes": [],
            "success_criteria": [],
            "gates": [],
            "effort_bucket": "M",
            "agent_role": "executor",
            "opened_at": _T0.isoformat(),
            "closed_at": _T0.isoformat(),
            "outcome": "ok",
            "sessions": {},
            "intent": None,
        }

    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": _REPO_CODE,
            "slug": "eawf",
            "title": "EAWF",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {"project_code": _REPO_CODE, "phase_id": _PHASE, "iter_id": _ITER},
        "workspace": None,
        "phases": {
            _PHASE: {
                "id": _PHASE,
                "scope_id": _REPO_CODE,
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": [_ITER],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            _ITER: {
                "id": _ITER,
                "phase_id": _PHASE,
                "title": "I14",
                "status": "active",
                "wave_ids": [_WAVE_A, _WAVE_B],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {_WAVE_A: _wave(_WAVE_A), _WAVE_B: _wave(_WAVE_B)},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(repo_root: Path) -> Path:
    """Validate + persist the seed state.json under ``<repo>/.ea/``."""
    state_path = repo_root / ".ea" / "state.json"
    state = State.model_validate(_state_payload())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _init_and_commit_spec(repo_root: Path, scope_id: str, *, body: str) -> Path:
    """Init the spec cache row for *scope_id*, write *body*, and git-commit it.

    Uses the in-process ``spec init`` writer (no daemon) to seed the cache
    entry + scaffold file, overwrites the file with the known *body* so the
    recovery assertion has a deterministic string, then commits the file so
    the archive ``git rm`` has a tracked file and ``git log`` carries the blob.
    """
    _inprocess_init(
        scope_id=scope_id,
        title="Wave",
        repo_code=_REPO_CODE,
        repo_root=repo_root,
    )
    spec_path = spec_writer.spec_file_path(scope_id, repo_root=repo_root)
    spec_path.write_text(body, encoding="utf-8")
    rel = spec_path.relative_to(repo_root)
    _git(repo_root, "add", str(rel))
    _git(repo_root, "commit", "--quiet", "-m", f"seed {scope_id}")
    return spec_path


def _seed_repo(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Wire env + git repo + state + two committed wave specs.

    Returns a map of wave id -> on-disk spec path so the caller asserts the
    removal / persistence directly.
    """
    _init_git_repo(repo_root)
    state_path = _write_state(repo_root)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_SPEC_CACHE_DIR", str(repo_root / ".ea" / "spec-cache"))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    paths = {
        _WAVE_A: _init_and_commit_spec(repo_root, _WAVE_A, body=f"# {_WAVE_A} body\n"),
        _WAVE_B: _init_and_commit_spec(repo_root, _WAVE_B, body=f"# {_WAVE_B} body\n"),
    }
    return paths


def _cache_status(repo_root: Path, scope_id: str) -> str | None:
    """Return the cache-row status for *scope_id*, or ``None`` when absent."""
    spec_urn = spec_writer.build_spec_urn(scope_id, repo_code=_REPO_CODE)
    entry = spec_cache.find_cached_entry(
        spec_urn,
        phase_id=_PHASE,
        cache_dir=repo_root / ".ea" / "spec-cache",
    )
    return None if entry is None else entry.status


def _show_from_git(repo_root: Path, scope_id: str) -> str:
    """Recover *scope_id*'s archived body via ``spec show <urn> --from-git``."""
    spec_urn = spec_writer.build_spec_urn(scope_id, repo_code=_REPO_CODE)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--workspace", str(repo_root), "spec", "show", spec_urn, "--from-git"],
    )
    assert result.exit_code == 0, result.output
    return result.output


# --------------------------------------------------------------------------- #
# Criterion 1 — iter close WITH the flag archives every wave spec.
# --------------------------------------------------------------------------- #
def test_iter_close_archive_specs_removes_marks_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--archive-specs`` git-removes each spec, ARCHIVES it, and recovers it."""
    repo_root = tmp_path / "repo"
    paths = _seed_repo(repo_root, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(repo_root),
            "iter",
            "close",
            _ITER,
            "--audit",
            "AUD-01",
            "--archive-specs",
        ],
    )
    assert result.exit_code == 0, result.output

    # The iter is closed in state.
    payload = orjson.loads((repo_root / ".ea" / "state.json").read_bytes())
    assert payload["iters"][_ITER]["status"] == "closed"

    for wave_id, spec_path in paths.items():
        # File git-removed from the working tree.
        assert not spec_path.exists(), wave_id
        # Cache row flipped to ARCHIVED.
        assert _cache_status(repo_root, wave_id) == "ARCHIVED", wave_id
        # Body recovered from git history.
        assert f"{wave_id} body" in _show_from_git(repo_root, wave_id)


# --------------------------------------------------------------------------- #
# Criterion 1 — WITHOUT the flag the specs stay untouched.
# --------------------------------------------------------------------------- #
def test_iter_close_without_flag_leaves_specs_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain ``iter close`` (no flag) does not archive any wave spec."""
    repo_root = tmp_path / "repo"
    paths = _seed_repo(repo_root, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--workspace", str(repo_root), "iter", "close", _ITER, "--audit", "AUD-01"],
    )
    assert result.exit_code == 0, result.output

    payload = orjson.loads((repo_root / ".ea" / "state.json").read_bytes())
    assert payload["iters"][_ITER]["status"] == "closed"

    for wave_id, spec_path in paths.items():
        # File still on disk and the cache row still DRAFT.
        assert spec_path.exists(), wave_id
        assert _cache_status(repo_root, wave_id) == "DRAFT", wave_id


# --------------------------------------------------------------------------- #
# Criterion 2 — the phase escape hatch archives all remaining specs in one call.
# --------------------------------------------------------------------------- #
def test_archive_phase_specs_back_fills_all_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``archive_phase_specs`` call archives every non-ARCHIVED phase spec."""
    repo_root = tmp_path / "repo"
    paths = _seed_repo(repo_root, monkeypatch)

    archived = archive_phase_specs(_PHASE, repo_code=_REPO_CODE, repo_root=repo_root)
    assert sorted(archived) == [_WAVE_A, _WAVE_B]

    for wave_id, spec_path in paths.items():
        assert not spec_path.exists(), wave_id
        assert _cache_status(repo_root, wave_id) == "ARCHIVED", wave_id

    # Idempotent re-run: nothing remains to archive.
    again = archive_phase_specs(_PHASE, repo_code=_REPO_CODE, repo_root=repo_root)
    assert again == []
