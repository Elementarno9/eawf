"""End-to-end proof of the two amend paths through the commit lint.

The amend signal the cap relies on is environmental: ``git commit --amend``
preserves the original author date and exports it to the commit-msg hook as
``GIT_AUTHOR_DATE``, while a fresh commit stamps the current time. A unit test
can only assert what the lint does with an injected environment; this module
drives real ``git commit`` invocations through the real hook so the inference
itself is pinned against the git binary that ships on the machine.

The second scenario is the fold the single-wave-close diagnostic prescribes:
close the wave, stage the close records, amend them onto the wave commit. The
wave is necessarily CLOSED by then, so the claimed-proof check used to refuse
the very action the other diagnostic instructs. Both directions are driven
here through real commits: bookkeeping-only folds land, deliverable bytes
under a closed wave do not.
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
_SUBJECT = "[P31-W27] fix: the wave deliverable"


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
    path.write_text(subject + _TRAILER, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Return a repo whose commit-msg hook is the real commit-prefix lint.

    The repo carries no ``.ea/`` tree, so the hierarchy and claimed-proof
    checks stand down and the one-commit-per-wave cap is what the commits
    actually exercise. It starts with one commit for wave ``P31-W27``.
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
    (root / "deliverable.txt").write_text("first\n", encoding="utf-8")
    assert _git(root, "add", "deliverable.txt").returncode == 0
    seed = _git(root, "commit", "-F", str(_write_message(root, _SUBJECT)))
    assert seed.returncode == 0, seed.stderr
    return root


def _seed_close_records(repo: Path, *, wave_status: str) -> None:
    """Write and stage the close records a wave close leaves behind.

    The state names phase ``P31`` / iter ``P31-I01`` / wave ``P31-I01-W27`` --
    the wave the seeded commit carries -- with the phase and iter still ACTIVE,
    which is the shape the operator folds under: one wave closes at a time
    while its iter stays open.
    """
    wave_id = "P31-I01-W27"
    payload = {
        "current": {"phase_id": "P31", "iter_id": "P31-I01"},
        "phases": {"P31": {"id": "P31", "status": "active", "iter_ids": ["P31-I01"]}},
        "iters": {
            "P31-I01": {
                "id": "P31-I01",
                "phase_id": "P31",
                "status": "active",
                "wave_ids": [wave_id],
            }
        },
        "waves": {wave_id: {"id": wave_id, "iter_id": "P31-I01", "status": wave_status}},
    }
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    evidence = repo / ".ea" / "store" / "evidence.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"wave_id": "P31-I01-W27"}\n', encoding="utf-8")
    assert _git(repo, "add", ".ea/state.json", ".ea/store/evidence.jsonl").returncode == 0


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


def test_real_second_commit_for_the_same_wave_is_rejected(repo: Path) -> None:
    """A real second commit naming the same wave is still capped."""
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
    assert "second commit for wave P31-I01-W27" in second.stderr
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_real_fold_amend_lands_close_records_under_a_closed_wave(repo: Path) -> None:
    """The fold the close diagnostic instructs succeeds after the wave closes."""
    _seed_close_records(repo, wave_status="closed")

    folded = _git(repo, "commit", "--amend", "-F", str(_write_message(repo, _SUBJECT)))

    assert folded.returncode == 0, folded.stderr
    tracked = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert ".ea/state.json" in tracked
    assert ".ea/store/evidence.jsonl" in tracked


def test_real_amend_adding_bytes_is_rejected_under_a_closed_wave(repo: Path) -> None:
    """Deliverable bytes still need a live wave: a closed one proves bookkeeping."""
    _seed_close_records(repo, wave_status="closed")
    (repo / "deliverable.txt").write_text("first\nsmuggled\n", encoding="utf-8")
    assert _git(repo, "add", "deliverable.txt").returncode == 0

    smuggled = _git(repo, "commit", "--amend", "-F", str(_write_message(repo, _SUBJECT)))

    assert smuggled.returncode != 0
    assert "canonical status 'closed' is not CLAIMED or IN_PROGRESS" in smuggled.stderr
