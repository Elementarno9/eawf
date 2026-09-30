"""Test helper that seeds a git repository whose tree holds one claimed wave, for worktree tests."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import orjson


def seed_repo_with_state(workdir: Path, *, on_main: bool = False) -> tuple[Path, Path]:
    """Initialise a git repo + .ea/state.json. Returns (repo_root, state_path)."""
    workdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=workdir, check=True)
    (workdir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workdir, check=True)
    if not on_main:
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/eawf-v0.1"],
            cwd=workdir,
            check=True,
        )

    state_path = workdir / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:DEMO",
        "updated_at": datetime.now(UTC).isoformat(),
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
            "track_id": None,
            "phase_id": "P05",
            "iter_id": "P05-I01",
            "active_wave_ids": ["P05-I01-W01"],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P05": {
                "id": "P05",
                "scope_id": "DEMO",
                "track_id": None,
                "title": "Phase 5",
                "status": "active",
                "iter_ids": ["P05-I01"],
                "outcome_ids": [],
                "opened_at": datetime.now(UTC).isoformat(),
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
                "wave_ids": ["P05-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            "P05-I01-W01": {
                "id": "P05-I01-W01",
                "iter_id": "P05-I01",
                "title": "W1",
                "status": "claimed",
                "deps": [],
                "file_scopes": ["src/eawf/runtime/worktree/"],
                "claim_session_id": "SES-001",
                "worktree_id": None,
                "outcome": None,
                "opened_at": datetime.now(UTC).isoformat(),
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    return workdir, state_path


def create_wave_worktree(repo: Path, state_path: Path, wave_id: str) -> Path:
    """Create the worktree for ``wave_id`` through the library and return its path.

    The CLI ``worktree create`` verb retired at the flag day, but the exempt
    ``worktree merge-back`` and ``worktree cleanup`` verbs still need a
    worktree row to act on, so tests lay one down the way the verb did.
    """
    from eawf.runtime.worktree import create_worktree, worktree_registry_lock
    from eawf.surfaces.cli._mutation import state_transaction

    with worktree_registry_lock(repo, timeout=5.0), state_transaction(state_path) as state:
        record = create_worktree(state, repo_root=repo, wave_id=wave_id)
    return repo / record.path
