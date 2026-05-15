"""Integration tests for ``eawf wave close --commit`` SHA normalisation.

Stands up a real ``git init``-ed tmp repo, seeds a CLAIMED wave in
``.ea/state.json``, and drives ``wave close --commit <ref>`` via
:class:`CliRunner` to verify:

* Boundary refs (full SHA, short SHA, branch name, tag, ``HEAD~1``)
  all normalise to the same 40-char hex value persisted on
  ``Wave.commit``.
* Invalid / unknown refs surface exit 3 BEFORE mutating state.json.
* ``eawf wave show --commit <id>`` round-trips the pinned SHA rather
  than re-deriving via ``git log --grep``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary is required for wave-close --commit tests",
)


# ---- fixtures ---------------------------------------------------------------


def _seed_repo_with_claimed_wave(
    repo: Path,
    *,
    wave_id: str = "P05-I01-W01",
) -> tuple[Path, list[str], dict[str, str]]:
    """Init a git repo, seed 3 commits + a tag + a branch, write ``.ea/state.json``.

    Returns ``(state_path, commit_shas_oldest_first, refs)`` where
    ``refs`` maps friendly names (``head``, ``head_minus_1``, ``branch``,
    ``tag``) to their original ref string the CLI should accept.
    """
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=repo, check=True)

    shas: list[str] = []
    for i, content in enumerate(("one\n", "two\n", "three\n")):
        (repo / "f.txt").write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=repo, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        shas.append(sha)

    # Seed a side branch + tag pointing at c1 (mid history) so we have
    # multiple resolvable refs that map to the same SHA on no ambiguity.
    subprocess.run(
        ["git", "branch", "side", shas[1]],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "tag", "v0.0.1", shas[1]],
        cwd=repo,
        check=True,
    )

    refs = {
        "head": "HEAD",
        "head_minus_1": "HEAD~1",
        "branch": "side",
        "tag": "v0.0.1",
        "short_sha": shas[2][:8],
        "full_sha": shas[2],
    }

    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:DEMO",
        "updated_at": now,
        "project": {
            "code": "DEMO",
            "slug": "demo",
            "title": "Demo",
            "description": None,
            "domains": ["test"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:DEMO",
        },
        "current": {
            "project_code": "DEMO",
            "subproject_id": None,
            "phase_id": "P05",
            "iter_id": "P05-I01",
            "active_wave_ids": [wave_id],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P05": {
                "id": "P05",
                "scope_id": "DEMO",
                "subproject_id": None,
                "title": "Phase 5",
                "status": "active",
                "iter_ids": ["P05-I01"],
                "outcome_ids": [],
                "opened_at": now,
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P05-I01": {
                "id": "P05-I01",
                "phase_id": "P05",
                "title": "Iter 1",
                "status": "active",
                "wave_ids": [wave_id],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": now,
                "closed_at": None,
            }
        },
        "waves": {
            wave_id: {
                "id": wave_id,
                "iter_id": "P05-I01",
                "title": "W1",
                "status": "claimed",
                "deps": [],
                "file_scopes": ["src/"],
                "claim_session_id": "SES-001",
                "worktree_id": None,
                "outcome": None,
                "opened_at": now,
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    return state_path, shas, refs


@pytest.fixture
def seeded_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path, list[str], dict[str, str]]]:
    """Init repo + state, chdir into it so ``git rev-parse`` resolves locally."""
    repo = tmp_path / "repo"
    state_path, shas, refs = _seed_repo_with_claimed_wave(repo)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.chdir(repo)
    yield repo, state_path, shas, refs


def _read_state(state_path: Path) -> dict[str, object]:
    return orjson.loads(state_path.read_bytes())  # type: ignore[no-any-return]


# ---- boundary refs ----------------------------------------------------------


@pytest.mark.parametrize(
    ("ref_key", "expected_index"),
    [
        ("full_sha", 2),
        ("short_sha", 2),
        ("head", 2),
        ("head_minus_1", 1),
        ("branch", 1),
        ("tag", 1),
    ],
)
def test_wave_close_commit_normalises_each_ref_shape(
    seeded_repo: tuple[Path, Path, list[str], dict[str, str]],
    ref_key: str,
    expected_index: int,
) -> None:
    """Every ref shape normalises to a 40-char hex SHA matching the expected commit."""
    _repo, state_path, shas, refs = seeded_repo
    ref = refs[ref_key]
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "close",
            "P05-I01-W01",
            "--outcome",
            "ok",
            "--commit",
            ref,
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(state_path)
    stored = state["waves"]["P05-I01-W01"]["commit"]  # type: ignore[index]
    assert isinstance(stored, str)
    assert len(stored) == 40
    assert all(c in "0123456789abcdef" for c in stored)
    assert stored == shas[expected_index]


def test_wave_close_without_commit_leaves_field_null(
    seeded_repo: tuple[Path, Path, list[str], dict[str, str]],
) -> None:
    """``wave close`` without ``--commit`` persists ``Wave.commit`` as ``null``."""
    _, state_path, _, _ = seeded_repo
    res = runner.invoke(
        app,
        ["wave", "close", "P05-I01-W01", "--outcome", "ok"],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(state_path)
    assert state["waves"]["P05-I01-W01"]["commit"] is None  # type: ignore[index]


# ---- error paths ------------------------------------------------------------


def test_wave_close_commit_unknown_ref_exits_3_without_mutation(
    seeded_repo: tuple[Path, Path, list[str], dict[str, str]],
) -> None:
    """An unresolvable ref surfaces exit 3; state.json wave stays CLAIMED."""
    _, state_path, _, _ = seeded_repo
    res = runner.invoke(
        app,
        [
            "wave",
            "close",
            "P05-I01-W01",
            "--outcome",
            "ok",
            "--commit",
            "does-not-exist",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 3, res.stdout
    assert "cannot resolve commit ref" in res.stdout
    state = _read_state(state_path)
    # Precondition failure must NOT have flipped status to closed.
    assert state["waves"]["P05-I01-W01"]["status"] == "claimed"  # type: ignore[index]


def test_wave_close_commit_invalid_ref_syntax_exits_3(
    seeded_repo: tuple[Path, Path, list[str], dict[str, str]],
) -> None:
    """A ref containing characters git rejects exits 3 with the canonical message."""
    _, state_path, _, _ = seeded_repo
    # Refs with embedded ".." outside a real range, plus a colon, are
    # rejected by git rev-parse with returncode != 0.
    res = runner.invoke(
        app,
        [
            "wave",
            "close",
            "P05-I01-W01",
            "--outcome",
            "ok",
            "--commit",
            ":not-a-ref:",
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 3, res.stdout
    assert "cannot resolve commit ref" in res.stdout


# ---- round-trip via ``wave show --commit`` ----------------------------------


def test_wave_show_commit_round_trips_pinned_sha(
    seeded_repo: tuple[Path, Path, list[str], dict[str, str]],
) -> None:
    """After ``wave close --commit`` the show verb prints the same SHA."""
    _, state_path, shas, refs = seeded_repo
    res = runner.invoke(
        app,
        [
            "wave",
            "close",
            "P05-I01-W01",
            "--outcome",
            "ok",
            "--commit",
            refs["head_minus_1"],
        ],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert res.exit_code == 0, res.stdout

    show = runner.invoke(
        app,
        ["wave", "show", "P05-I01-W01", "--commit"],
        env={**os.environ, "EA_STATE": str(state_path)},
    )
    assert show.exit_code == 0, show.stdout
    # ``wave show`` prints the SHA on its own line, no trailing decoration.
    assert show.stdout.strip() == shas[1]
