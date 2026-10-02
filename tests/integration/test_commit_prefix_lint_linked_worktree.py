"""A commit in a linked worktree proves its Task against the main checkout.

A Task is created in the main checkout after the worktree branched, so the
worktree's own generation never holds it, and its RUNNING status sits only
in the main checkout's machine-local status projection. Real ``git commit``
invocations run the real commit-msg hook inside the linked worktree, with
the ``GIT_DIR`` git exports to it, so the main checkout is found the way
the hook finds it in use.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eawf.kernel.store.compaction import read_document, write_document

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_GENERATION = "gen-0123456789abcdef"
_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo* with the fixture's identity."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Lint Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def checkouts(tmp_path: Path) -> tuple[Path, Path]:
    """Return a marked main checkout and a worktree branched from it.

    The commit-msg hook is the real lint, installed in the common git dir
    both checkouts share. EAWF-0250 is created in main only after the
    worktree branched, then started, so its committed row reads PLANNED.
    """
    main = tmp_path / "main"
    ea = main / ".ea"
    _write(ea / "epoch2-opt-in.json", {"opt_in": True})
    _write(ea / "generations" / "EPOCH2_ACTIVE.json", {"epoch": 2, "generation_id": _GENERATION})
    _write(ea / "state.json", {})
    document_path = ea / "generations" / _GENERATION / "state.json"
    write_document(document_path, {"batch": {"BAT-0001": {"status": "ACTIVE"}}, "task": {}})
    (main / ".gitignore").write_text(".ea/generations/gen-*/local/\n", encoding="utf-8")
    assert _git(main, "init", "-q").returncode == 0
    hook = main / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_LINT_PATH}" "$1"\n', encoding="utf-8")
    hook.chmod(0o755)
    assert _git(main, "add", "-A").returncode == 0
    seeded = _git(main, "commit", "-q", "--no-verify", "-m", "chore: seed")
    assert seeded.returncode == 0, seeded.stderr
    linked = tmp_path / "linked"
    added = _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
    assert added.returncode == 0, added.stderr

    document = read_document(document_path)
    row = {"key": "EAWF-0250", "batch_ref": "BAT-0001", "status": "PLANNED", "revision": 1}
    document["task"]["EAWF-0250"] = row
    write_document(document_path, document)
    document["task"]["EAWF-0250"] = row | {"status": "RUNNING", "revision": 2}
    write_document(document_path, document)
    committed = json.loads(document_path.read_text(encoding="utf-8"))
    assert committed["task"]["EAWF-0250"]["status"] == "PLANNED"
    return main, linked


def _commit(linked: Path, key: str) -> subprocess.CompletedProcess[str]:
    # Pinned so a developer's global hooks path cannot shadow the hook under test.
    hooks = linked.parent / "main" / ".git" / "hooks"
    (linked / "change.txt").write_text(f"{key}\n", encoding="utf-8")
    assert _git(linked, "add", "change.txt").returncode == 0
    message = linked.parent / "message.txt"
    message.write_text(f"fix: land it\n\nTask: {key}{_TRAILER}", encoding="utf-8")
    return _git(linked, "-c", f"core.hooksPath={hooks}", "commit", "-q", "-F", str(message))


def test_a_task_created_in_main_after_the_branch_proves_a_worktree_commit(
    checkouts: tuple[Path, Path],
) -> None:
    _, linked = checkouts

    landed = _commit(linked, "EAWF-0250")

    assert landed.returncode == 0, landed.stderr


def test_a_task_main_does_not_hold_is_refused_from_the_worktree(
    checkouts: tuple[Path, Path],
) -> None:
    """Gate-fire: the same commit naming an unknown Task reds."""
    _, linked = checkouts

    refused = _commit(linked, "EAWF-0999")

    assert refused.returncode != 0
    assert "the canonical generation has no such Task" in refused.stderr
