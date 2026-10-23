"""End-to-end proof of the amend path through the commit lint.

The amend signal the cap relies on is environmental: ``git commit --amend``
preserves the original author date and exports it to the commit-msg hook as
``GIT_AUTHOR_DATE``, while a fresh commit stamps the current time. A unit test
can only assert what the lint does with an injected environment; this module
drives real ``git commit`` invocations through the real hook so the inference
itself is pinned against the git binary that ships on the machine.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_TASK = "EAWF-0001"
_GENERATION_ID = "gen-0123456789abcdef"
_SUBJECT = "fix: the Task deliverable"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo* with the fixture's identity and hooks.

    ``core.hooksPath`` is pinned to the repo's own hook dir so a developer's
    global hooks path cannot shadow the hook under test.
    """
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
            "-c",
            f"core.hooksPath={repo / '.git' / 'hooks'}",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_message(repo: Path, subject: str) -> Path:
    path = repo / "message.txt"
    path.write_text(f"{subject}\n\nTask: {_TASK}" + _TRAILER, encoding="utf-8")
    return path


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Return a repo whose commit-msg hook is the real commit-prefix lint.

    The repo's untracked ``.ea/`` tree marks it epoch 2 with Task
    ``EAWF-0001`` CLAIMED, so the Task trailer is proved and the
    one-commit-per-Task cap is what the commits actually exercise. It starts
    with one commit for that Task.
    """
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, text=True)
    hook = root / ".git" / "hooks" / "commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{_LINT_PATH}" "$1"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _write_json(root / ".ea" / "epoch2-opt-in.json", {"opt_in": True})
    _write_json(
        root / ".ea" / "generations" / "EPOCH2_ACTIVE.json",
        {"epoch": 2, "generation_id": _GENERATION_ID},
    )
    _write_json(
        root / ".ea" / "generations" / _GENERATION_ID / "state.json",
        {"task": {_TASK: {"key": _TASK, "status": "CLAIMED"}}},
    )
    (root / "deliverable.txt").write_text("first\n", encoding="utf-8")
    assert _git(root, "add", "deliverable.txt").returncode == 0
    seed = _git(root, "commit", "-F", str(_write_message(root, _SUBJECT)))
    assert seed.returncode == 0, seed.stderr
    return root


def _head_author_epoch(repo: Path) -> int:
    proc = _git(repo, "log", "-1", "--format=%at")
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


def test_real_amend_adding_bytes_passes_the_commit_lint(repo: Path) -> None:
    """A real ``--amend`` that adds deliverable bytes is not read as a second commit."""
    (repo / "deliverable.txt").write_text("first\nsecond\n", encoding="utf-8")
    assert _git(repo, "add", "deliverable.txt").returncode == 0

    amended = _git(
        repo,
        "commit",
        "--amend",
        "-F",
        str(_write_message(repo, f"{_SUBJECT}, enlarged")),
    )

    assert amended.returncode == 0, amended.stderr
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_real_second_commit_for_the_same_task_is_rejected(repo: Path) -> None:
    """A real second commit naming the same Task is still capped."""
    (repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    assert _git(repo, "add", "extra.txt").returncode == 0

    # An explicit author date one minute past the tip's makes the fresh commit
    # deterministic: without it a commit written inside the same clock second
    # as the seed would carry the tip's own author date.
    second = _git(
        repo,
        "commit",
        f"--date=@{_head_author_epoch(repo) + 60} +0000",
        "-F",
        str(_write_message(repo, f"{_SUBJECT}, again")),
    )

    assert second.returncode != 0
    assert f"second commit for task {_TASK}" in second.stderr
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_real_state_only_commit_for_the_same_task_skips_the_cap(repo: Path) -> None:
    """Boundary: a fresh commit staging only bookkeeping adds no deliverable bytes."""
    evidence = repo / ".ea" / "store" / "evidence.jsonl"
    _write_json(evidence, {"task": _TASK})
    assert _git(repo, "add", ".ea/store/evidence.jsonl").returncode == 0

    fold = _git(
        repo,
        "commit",
        f"--date=@{_head_author_epoch(repo) + 60} +0000",
        "-F",
        str(_write_message(repo, f"{_SUBJECT}, its records")),
    )

    assert fold.returncode == 0, fold.stderr
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"
