"""``eawf wave prune-branches`` deletes only local heads an archive ref already preserves.

Every test builds a scratch repository shaped like the real one this verb
was written for: waves ran on ``-pNN-wMM`` branches and were cherry-picked
onto ``main``, and harness leftovers (``worktree-agent-*``, ``p33-preguard``)
sit alongside them, unrelated to any wave. ``main``, ``plugins-dist``, and
the checked-out long-running branch (``feature/zz-v1.0``, standing in for
the real ``feature/<symbol>-v<X.Y>``) must survive every prune. A branch is
only ever deleted once ``wave archive-refs`` has preserved its exact head;
otherwise the whole batch is refused, and nothing is deleted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.workflow.lifecycle.wave_archive import (
    WaveArchiveError,
    list_misc_branches,
    misc_archive_ref_for_branch,
    prune_branches,
)
from tests.integration.test_wave_verify_commits_cmd import _commit_wave, _git, _seed_state

W01 = "P28-I01-W01"
W06 = "P28-I01-W06"
WAVE_BRANCHES = ("feature/zz-v1.0-p28-w01", "feature/zz-v1.0-p28-w06")
MISC_BRANCHES = ("worktree-agent-a1b2c3d4e5", "p33-preguard")
PROTECTED = ("main", "plugins-dist", "feature/zz-v1.0")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    """A repo checked out on the phase branch, with wave, misc, and protected branches.

    ``feature/zz-v1.0-p28-w01``/``...-w06`` are cherry-picked onto ``main``
    (state still pins their pre-cherry-pick SHAs, matching the real
    P34 history). ``worktree-agent-a1b2c3d4e5`` and ``p33-preguard`` sit at
    ``main``'s head, unrelated to any wave -- the harness leftovers a
    wave-only sweep misses. ``main``, ``plugins-dist``, and the checked-out
    ``feature/zz-v1.0`` are the three that must survive every prune.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "root.txt").write_text("root\n", encoding="utf-8")
    _git(root, "add", "root.txt")
    _git(root, "commit", "-q", "-m", "chore: root")
    _git(root, "branch", "plugins-dist")
    _git(root, "branch", "feature/zz-v1.0")
    pins: dict[str, str] = {}
    for wave, branch in zip(("W01", "W06"), WAVE_BRANCHES, strict=True):
        _git(root, "switch", "-q", "-c", branch, "main")
        pins[wave] = _commit_wave(root, name=f"{wave}.txt", wave_id=f"P28-I01-{wave}")
    _git(root, "switch", "-q", "main")
    _git(root, "cherry-pick", pins["W01"], pins["W06"])
    for branch in MISC_BRANCHES:
        _git(root, "branch", branch, "main")
    _git(root, "switch", "-q", "feature/zz-v1.0")
    state_path = _seed_state(root / ".ea", waves={W01: pins["W01"], W06: pins["W06"]})
    monkeypatch.setenv("EA_STATE", str(state_path))
    return root, pins


def _invoke(*args: str) -> tuple[int, str]:
    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, list(args))
    return res.exit_code, res.output


def _local_branches(root: Path) -> set[str]:
    out = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return set(out.splitlines())


def _branch_exists(root: Path, branch: str) -> bool:
    return branch in _local_branches(root)


def _archive_refs(root: Path) -> dict[str, str]:
    out = _git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/eawf/archive")
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


def _object_exists(root: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def test_misc_archive_ref_for_branch_prefixes_with_misc() -> None:
    assert misc_archive_ref_for_branch("p33-preguard") == "refs/eawf/archive/misc/p33-preguard"
    assert (
        misc_archive_ref_for_branch("worktree-agent-a1b2c3")
        == "refs/eawf/archive/misc/worktree-agent-a1b2c3"
    )


def test_list_misc_branches_excludes_wave_and_protected_branches(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, _pins = repo
    misc = {b.branch for b in list_misc_branches(repo_root=root)}
    assert misc == set(MISC_BRANCHES)


def test_archive_refs_include_misc_writes_misc_refs(repo: tuple[Path, dict[str, str]]) -> None:
    root, _pins = repo
    code, output = _invoke("--json", "wave", "archive-refs", "--include-misc")
    assert code == 0, output
    assert json.loads(output)["written_count"] == len(WAVE_BRANCHES) + len(MISC_BRANCHES)
    refs = _archive_refs(root)
    for branch in MISC_BRANCHES:
        assert misc_archive_ref_for_branch(branch) in refs


def test_archive_refs_without_include_misc_leaves_misc_branches_unarchived(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, _pins = repo
    assert _invoke("wave", "archive-refs")[0] == 0
    refs = _archive_refs(root)
    for branch in MISC_BRANCHES:
        assert misc_archive_ref_for_branch(branch) not in refs


def test_prune_dry_run_previews_without_deleting(repo: tuple[Path, dict[str, str]]) -> None:
    root, _pins = repo
    assert _invoke("wave", "archive-refs", "--include-misc")[0] == 0
    before = _local_branches(root)
    code, output = _invoke("--json", "wave", "prune-branches", "--dry-run")
    assert code == 0, output
    payload = json.loads(output)
    assert payload["dry_run"] is True
    assert {row["branch"] for row in payload["deleted"]} == set(WAVE_BRANCHES) | set(MISC_BRANCHES)
    assert {row["branch"] for row in payload["skipped"]} == set(PROTECTED)
    assert _local_branches(root) == before


def test_prune_deletes_archived_heads_and_keeps_protected(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, _pins = repo
    assert _invoke("wave", "archive-refs", "--include-misc")[0] == 0
    code, output = _invoke("--json", "wave", "prune-branches")
    assert code == 0, output
    payload = json.loads(output)
    assert payload["deleted_count"] == len(WAVE_BRANCHES) + len(MISC_BRANCHES)
    assert payload["dry_run"] is False
    assert _local_branches(root) == set(PROTECTED)


def test_prune_refuses_when_a_branch_is_unarchived(repo: tuple[Path, dict[str, str]]) -> None:
    root, _pins = repo
    # Wave branches only -- the misc pair is deliberately left unarchived.
    assert _invoke("wave", "archive-refs")[0] == 0
    before = _local_branches(root)
    code, output = _invoke("wave", "prune-branches")
    assert code == exit_codes.USER_ERROR, output
    assert "worktree-agent-a1b2c3d4e5" in output
    assert "p33-preguard" in output
    # All or nothing: the archived wave branches survive the refused batch too.
    assert _local_branches(root) == before


def test_prune_refuses_when_archive_ref_points_elsewhere(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, pins = repo
    assert _invoke("wave", "archive-refs", "--include-misc")[0] == 0
    _git(root, "update-ref", "refs/eawf/archive/P28/W01", pins["W06"])
    before = _local_branches(root)
    code, output = _invoke("wave", "prune-branches")
    assert code == exit_codes.USER_ERROR, output
    assert "feature/zz-v1.0-p28-w01" in output
    assert _local_branches(root) == before


def test_prune_skips_branch_checked_out_in_a_worktree(
    repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, _pins = repo
    wt_path = tmp_path / "w06-wt"
    _git(root, "worktree", "add", "-q", str(wt_path), "feature/zz-v1.0-p28-w06")
    assert _invoke("wave", "archive-refs", "--include-misc")[0] == 0
    code, output = _invoke("--json", "wave", "prune-branches")
    assert code == 0, output
    payload = json.loads(output)
    deleted = {row["branch"] for row in payload["deleted"]}
    skipped = {row["branch"]: row["reason"] for row in payload["skipped"]}
    assert "feature/zz-v1.0-p28-w01" in deleted
    assert "feature/zz-v1.0-p28-w06" not in deleted
    assert skipped["feature/zz-v1.0-p28-w06"] == "checked out in a worktree"
    assert _branch_exists(root, "feature/zz-v1.0-p28-w06")


def test_prune_removes_stale_worktree_registration(
    repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, _pins = repo
    wt_path = tmp_path / "stale-wt"
    _git(root, "worktree", "add", "--detach", "-q", str(wt_path), "main")
    assert _invoke("wave", "archive-refs", "--include-misc")[0] == 0
    shutil.rmtree(wt_path)
    code, output = _invoke("--json", "wave", "prune-branches")
    assert code == 0, output
    payload = json.loads(output)
    assert str(wt_path) in payload["pruned_worktrees"]
    remaining = _git(root, "worktree", "list", "--porcelain")
    assert str(wt_path) not in remaining


def test_drift_and_pin_reachability_unchanged_after_prune(
    repo: tuple[Path, dict[str, str]],
) -> None:
    root, pins = repo
    assert _invoke("wave", "archive-refs", "--include-misc")[0] == 0
    before = _invoke("--json", "wave", "verify-commits")
    assert _object_exists(root, pins["W01"])
    assert _object_exists(root, pins["W06"])
    assert _invoke("wave", "prune-branches")[0] == 0
    after = _invoke("--json", "wave", "verify-commits")
    assert before == after
    # The pruned branches are gone, but the archive refs kept both objects alive.
    assert _object_exists(root, pins["W01"])
    assert _object_exists(root, pins["W06"])


def test_prune_branches_outside_a_repository_raises(tmp_path: Path) -> None:
    with pytest.raises(WaveArchiveError):
        prune_branches(repo_root=tmp_path)


def test_prune_branches_nothing_to_prune_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "f.txt").write_text("f\n", encoding="utf-8")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "chore: root")

    result = prune_branches(repo_root=root)

    assert result.deleted == []
    assert result.pruned_worktrees == []
    assert dict(result.skipped) == {"main": "checked out (current branch)"}
