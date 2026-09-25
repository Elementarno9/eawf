"""Wave pin reachability is scoped to integration history, not every local branch.

Pins are verified against first-parent ``main`` (local or ``origin/main``),
the first-parent chains of ``v*`` release tags, and the checked-out branch
back to its merge-base with main. A pin kept alive only by some other local
branch (a pre-squash worktree branch) must report as drift, because a fresh
clone of main plus tags cannot see it. Every test builds a real scratch
repository so the ``git log`` rev selection itself is under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.workflow.lifecycle.wave_sha import (
    CommitPinIssue,
    _first_parent_wave_candidates,
    _reachable_wave_keys,
    detect_git_state_drift,
    scan_commit_pins,
)

WAVE = "P28-I01-W01"
OTHER = "P28-I01-W02"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository on ``main`` with one root commit, isolated from user config."""
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
    _commit(root, name="root.txt", message="chore: root")
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit(root: Path, *, name: str, message: str) -> str:
    """Commit one new file and return the new SHA."""
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _wave_message(wave_id: str, summary: str = "feat: land the wave") -> str:
    return f"{summary}\n\nEawf-Wave: {wave_id}\n"


def _branch_only_commit(root: Path, *, branch: str, wave_id: str) -> str:
    """Commit *wave_id* on a side branch, then return to ``main``."""
    _git(root, "checkout", "-q", "-b", branch)
    sha = _commit(root, name=f"{branch.replace('/', '-')}.txt", message=_wave_message(wave_id))
    _git(root, "checkout", "-q", "main")
    return sha


def _state(pins: dict[str, str | None]) -> State:
    waves: dict[str, Any] = {
        wave_id: {
            "id": wave_id,
            "iter_id": wave_id.rsplit("-", 1)[0],
            "title": f"wave {wave_id}",
            "status": "closed",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "commit": commit,
            "opened_at": "2026-05-27T00:00:00Z",
            "closed_at": "2026-05-27T00:01:00Z",
        }
        for wave_id, commit in pins.items()
    }
    return State.model_validate(
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
            "waves": waves,
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _kinds(root: Path, pins: dict[str, str | None]) -> dict[str, str]:
    return {d.wave_id: d.kind for d in detect_git_state_drift(_state(pins), repo_root=root)}


# ---- gate-fire proof ---------------------------------------------------------


def test_detect_drift_branch_only_pin_is_drift_while_main_pin_is_clean(repo: Path) -> None:
    """The seeded defect: a pin alive only on a local branch must not read clean."""
    on_main = _commit(repo, name="w2.txt", message=_wave_message(OTHER))
    branch_only = _branch_only_commit(repo, branch="feature/x-v1.0-p28-w01", wave_id=WAVE)

    kinds = _kinds(repo, {WAVE: branch_only, OTHER: on_main})

    assert kinds == {WAVE: "pinned_but_missing"}
    assert branch_only not in _reachable_wave_keys(repo)
    assert on_main in _reachable_wave_keys(repo)


def test_scan_commit_pins_branch_only_pin_is_unresolvable(repo: Path) -> None:
    branch_only = _branch_only_commit(repo, branch="stale", wave_id=WAVE)

    issues = scan_commit_pins(_state({WAVE: branch_only}), repo_root=repo)

    assert [(i.wave_id, i.kind, i.resolution) for i in issues] == [
        (WAVE, "pinned_but_missing", "unresolvable")
    ]


# ---- what counts as integration history --------------------------------------


def test_detect_drift_checked_out_branch_pins_are_clean(repo: Path) -> None:
    """A live phase branch does not report its own unmerged wave pins as drift."""
    _git(repo, "checkout", "-q", "-b", "feature/x-v1.0")
    fresh = _commit(repo, name="w1.txt", message=_wave_message(WAVE))

    assert _kinds(repo, {WAVE: fresh}) == {}


def test_detect_drift_checked_out_branch_counts_only_back_to_merge_base(repo: Path) -> None:
    """Below the merge-base the checked-out chain adds nothing main lacks.

    ``side`` lands on main only as a merge's second parent; a branch forked
    from it reaches it by first parent, yet it sits at the merge-base, so the
    pin still drifts while the branch's own fresh commit stays clean.
    """
    side_only = _branch_only_commit(repo, branch="side", wave_id=WAVE)
    _git(repo, "merge", "-q", "--no-ff", "-m", "chore: merge side", "side")
    _git(repo, "checkout", "-q", "-b", "feature/x-v1.0", side_only)
    fresh = _commit(repo, name="w2.txt", message=_wave_message(OTHER))

    assert _kinds(repo, {WAVE: side_only, OTHER: fresh}) == {WAVE: "pinned_but_missing"}


def test_detect_drift_release_tag_pin_is_clean_but_other_tag_is_drift(repo: Path) -> None:
    released = _branch_only_commit(repo, branch="release", wave_id=WAVE)
    _git(repo, "tag", "v1.0.0", released)
    spiked = _branch_only_commit(repo, branch="spike", wave_id=OTHER)
    _git(repo, "tag", "poc/spike", spiked)

    assert _kinds(repo, {WAVE: released, OTHER: spiked}) == {OTHER: "pinned_but_missing"}


def test_detect_drift_origin_main_counts_when_local_main_is_absent(repo: Path) -> None:
    on_main = _commit(repo, name="w1.txt", message=_wave_message(WAVE))
    _git(repo, "update-ref", "refs/remotes/origin/main", on_main)
    _git(repo, "checkout", "-q", "--detach")
    _git(repo, "branch", "-q", "-D", "main")

    assert _kinds(repo, {WAVE: on_main}) == {}


def test_detect_drift_without_main_falls_back_to_checked_out_chain(repo: Path) -> None:
    _git(repo, "branch", "-q", "-m", "main", "trunk")
    on_trunk = _commit(repo, name="w1.txt", message=_wave_message(WAVE))
    _git(repo, "checkout", "-q", "-b", "stray")
    stray = _commit(repo, name="stray.txt", message=_wave_message(OTHER))
    _git(repo, "checkout", "-q", "trunk")

    assert _kinds(repo, {WAVE: on_trunk, OTHER: stray}) == {OTHER: "pinned_but_missing"}


def test_first_parent_wave_candidates_skip_second_parent_commits(repo: Path) -> None:
    """A merged side branch's own commit is not a first-parent candidate."""
    side = _branch_only_commit(repo, branch="side", wave_id=WAVE)
    _git(repo, "merge", "-q", "--no-ff", "-m", "chore: merge side", "side")

    assert WAVE not in _first_parent_wave_candidates(repo)
    assert side not in _reachable_wave_keys(repo)


def test_reachable_wave_keys_empty_outside_a_repository(tmp_path: Path) -> None:
    assert _reachable_wave_keys(tmp_path) == {}
    assert _first_parent_wave_candidates(tmp_path) == {}


def test_reachable_wave_keys_empty_on_unborn_head(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    assert _reachable_wave_keys(root) == {}


# ---- classification of a squashed history ------------------------------------


def test_scan_commit_pins_classifies_each_drift_resolution(repo: Path) -> None:
    """Squashed, duplicated and lost waves map to the three resolutions."""
    repairable_pin = _branch_only_commit(repo, branch="w01", wave_id=WAVE)
    ambiguous_pin = _branch_only_commit(repo, branch="w02", wave_id=OTHER)
    lost_wave = "P28-I01-W03"
    lost_pin = _branch_only_commit(repo, branch="w03", wave_id=lost_wave)
    squashed = _commit(repo, name="sq1.txt", message=_wave_message(WAVE, "feat: squash one"))
    _commit(repo, name="sq2.txt", message=_wave_message(OTHER, "feat: first copy"))
    _commit(repo, name="sq3.txt", message=_wave_message(OTHER, "feat: second copy"))

    issues = scan_commit_pins(
        _state({WAVE: repairable_pin, OTHER: ambiguous_pin, lost_wave: lost_pin}),
        repo_root=repo,
    )

    by_wave = {i.wave_id: (i.kind, i.resolution, i.git_commit) for i in issues}
    assert by_wave == {
        WAVE: ("pinned_mismatch", "repairable", squashed),
        OTHER: ("ambiguous_successor", "ambiguous", None),
        lost_wave: ("pinned_but_missing", "unresolvable", None),
    }


def test_detect_drift_matches_scan_hard_kinds(repo: Path) -> None:
    """``status`` drift and ``verify-commits`` share one history scope."""
    branch_only = _branch_only_commit(repo, branch="w01", wave_id=WAVE)
    _commit(repo, name="w2.txt", message=_wave_message(OTHER))
    state = _state({WAVE: branch_only, OTHER: None})

    drift = {d.wave_id: d.kind for d in detect_git_state_drift(state, repo_root=repo)}
    scan = {i.wave_id: i.kind for i in scan_commit_pins(state, repo_root=repo)}

    assert drift == {k: v for k, v in scan.items() if v != "unpinned_derivable"}
    assert scan[OTHER] == "unpinned_derivable"


@pytest.mark.parametrize(
    ("kind", "repairable", "expected"),
    [
        ("pinned_mismatch", True, "repairable"),
        ("unpinned_derivable", True, "repairable"),
        ("ambiguous_successor", False, "ambiguous"),
        ("pinned_but_missing", False, "unresolvable"),
        ("closed_no_pin", False, "unresolvable"),
        ("closed_unfindable", False, "unresolvable"),
    ],
)
def test_commit_pin_issue_resolution_per_kind(kind: Any, repairable: bool, expected: str) -> None:
    issue = CommitPinIssue(wave_id=WAVE, kind=kind, repairable=repairable)
    assert issue.resolution == expected
