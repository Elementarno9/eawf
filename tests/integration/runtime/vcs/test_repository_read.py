"""The repository read: the branch from git, the review and checks from gh, and why not.

Every ``gh`` here is a script on a private ``PATH`` that prints a recorded answer, so no
test reaches the host. The git reads run the real ``git`` on throwaway repositories.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.runtime.vcs import repository_read as rr
from eawf.runtime.vcs.repository_read import (
    DETACHED,
    GH_MISSING,
    GH_SHAPE,
    GH_UNAUTHENTICATED,
    GIT_MISSING,
    NO_COMMIT,
    NOT_A_REPOSITORY,
    BranchDrift,
    BranchRead,
    parse_pull_request,
    read_branch,
    read_pull_request,
    read_repository,
)

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).resolve().parents[3] / "fixtures/hosts/gh_pr_view"
GIT: Final = shutil.which("git")
# the scripted gh runs on a PATH holding only itself and git
CAT: Final = shutil.which("cat")
SLEEP: Final = shutil.which("sleep")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with nothing cached, so no read leaks between tests."""
    monkeypatch.setattr(rr, "_branches", {})
    monkeypatch.setattr(rr, "_pull_requests", {})


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate git from the operator's config and give commits a fixed identity."""
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(tmp_path / "home"),
    }.items():
        monkeypatch.setenv(name, value)


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(repo: Path, subject: str) -> None:
    _run(repo, "commit", "--allow-empty", "-m", subject)


def _clone(tmp_path: Path) -> Path:
    """Return a clone of a bare remote whose ``main`` holds one commit, ``origin/HEAD`` set."""
    seed, remote, clone = tmp_path / "seed", tmp_path / "remote.git", tmp_path / "clone"
    seed.mkdir()
    _run(seed, "init", "-b", "main")
    _commit(seed, "first")
    _run(tmp_path, "clone", "--bare", str(seed), str(remote))
    _run(tmp_path, "clone", str(remote), str(clone))
    return clone


def _bin(tmp_path: Path, gh_script: str | None, *, with_git: bool = True) -> Path:
    """Return a ``PATH`` directory holding git (unless left out) and a scripted ``gh``."""
    path = tmp_path / "bin"
    path.mkdir(exist_ok=True)
    if with_git:
        assert GIT is not None
        (path / "git").symlink_to(GIT)
    if gh_script is not None:
        gh = path / "gh"
        gh.write_text(f"#!/bin/sh\n{gh_script}\n", encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return path


# ---------- the gh answer ----------


def test_an_open_pull_request_counts_each_reviewer_by_their_latest_verdict() -> None:
    pr = parse_pull_request(_fixture("open_reviewed.json"))
    assert (pr.number, pr.state, pr.review_decision) == (418, "OPEN", "CHANGES_REQUESTED")
    # reviewer-a requested changes twice and reviewer-c only commented
    assert (pr.approvals, pr.changes_requested) == (1, 1)


def test_every_check_kind_maps_onto_one_outcome() -> None:
    pr = parse_pull_request(_fixture("open_reviewed.json"))
    assert [(c.name, c.outcome) for c in pr.checks] == [
        ("tests", "pass"),
        ("lint", "fail"),
        ("conformance", "pending"),
        ("release", "skipped"),
        ("ci/external", "pending"),
    ]


def test_a_recorded_merged_pull_request_with_no_review_parses() -> None:
    pr = parse_pull_request(_fixture("merged_unreviewed.json"))
    assert (pr.state, pr.review_decision, pr.approvals) == ("MERGED", None, 0)
    assert [c.outcome for c in pr.checks] == ["pass", "pass", "pass"]


def test_a_reviewer_who_requested_changes_and_then_approved_has_approved() -> None:
    raw = (
        '{"number":1,"state":"OPEN","url":"u","reviewDecision":"APPROVED","reviews":['
        '{"author":{"login":"a"},"state":"CHANGES_REQUESTED"},'
        '{"author":{"login":"a"},"state":"APPROVED"}],"statusCheckRollup":[]}'
    )
    pr = parse_pull_request(raw)
    assert (pr.approvals, pr.changes_requested, pr.checks) == (1, 0, ())


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        '{"number":1,"state":"OPEN"}',
        '{"number":1,"state":"OPEN","url":"u","extra":1}',
        '{"number":"x","state":"OPEN","url":"u"}',
        '{"number":1,"state":"OPEN","url":"u","statusCheckRollup":[{"__typename":"Other"}]}',
    ],
)
def test_an_answer_outside_the_asked_shape_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_pull_request(raw)


# ---------- the git reads ----------


@pytest.mark.usefixtures("git_env")
def test_a_branch_reads_its_drift_from_upstream_and_the_default_branch(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    _run(clone, "checkout", "-b", "topic", "--track", "origin/main")
    _commit(clone, "one")
    _commit(clone, "two")
    read = read_branch(clone)
    assert isinstance(read, BranchRead)
    assert read.branch == "topic"
    assert read.head.subject == "two"
    assert read.upstream == BranchDrift(ref="origin/main", ahead=2, behind=0)
    assert read.default == BranchDrift(ref="origin/main", ahead=2, behind=0)


@pytest.mark.usefixtures("git_env")
def test_a_branch_behind_its_upstream_counts_behind(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    _commit(clone, "ahead")
    _run(clone, "push", "origin", "main")
    _run(clone, "reset", "--hard", "HEAD~1")
    read = read_branch(clone)
    assert isinstance(read, BranchRead)
    assert read.upstream == BranchDrift(ref="origin/main", ahead=0, behind=1)


@pytest.mark.usefixtures("git_env")
def test_a_branch_with_no_upstream_states_none(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    _run(clone, "checkout", "-b", "local")
    read = read_branch(clone)
    assert isinstance(read, BranchRead)
    assert read.upstream is None
    assert read.default == BranchDrift(ref="origin/main", ahead=0, behind=0)


@pytest.mark.usefixtures("git_env")
def test_an_upstream_that_was_deleted_reads_gone(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    _run(clone, "push", "origin", "main:topic")
    _run(clone, "fetch", "origin")
    _run(clone, "checkout", "-b", "topic", "--track", "origin/topic")
    _run(clone, "push", "origin", "--delete", "topic")
    _run(clone, "fetch", "--prune", "origin")
    read = read_branch(clone)
    assert isinstance(read, BranchRead)
    assert read.upstream == BranchDrift(ref="origin/topic", ahead=0, behind=0, gone=True)


@pytest.mark.usefixtures("git_env")
def test_a_repository_without_a_remote_states_no_default(tmp_path: Path) -> None:
    _run(tmp_path, "init", "-b", "main")
    _commit(tmp_path, "only")
    read = read_branch(tmp_path)
    assert isinstance(read, BranchRead)
    assert (read.branch, read.upstream, read.default) == ("main", None, None)


@pytest.mark.usefixtures("git_env")
def test_a_detached_head_names_no_branch(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    _run(clone, "checkout", "--detach")
    read = read_branch(clone)
    assert isinstance(read, BranchRead)
    assert (read.branch, read.upstream) == (None, None)


@pytest.mark.usefixtures("git_env")
def test_a_repository_with_no_commit_says_so(tmp_path: Path) -> None:
    _run(tmp_path, "init", "-b", "main")
    assert read_branch(tmp_path) == NO_COMMIT


@pytest.mark.usefixtures("git_env")
def test_a_directory_outside_any_repository_says_so(tmp_path: Path) -> None:
    assert read_branch(tmp_path) == NOT_A_REPOSITORY


def test_a_missing_git_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(_bin(tmp_path, None, with_git=False)))
    assert read_branch(tmp_path) == GIT_MISSING


# ---------- the gh call ----------


def test_a_missing_gh_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(_bin(tmp_path, None)))
    assert read_pull_request(tmp_path, "topic") == GH_MISSING


def test_a_gh_that_is_not_logged_in_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(_bin(tmp_path, "echo 'gh auth login' >&2; exit 4")))
    assert read_pull_request(tmp_path, "topic") == GH_UNAUTHENTICATED


def test_a_branch_with_no_pull_request_is_a_stated_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = "echo 'no pull requests found for branch \"topic\"' >&2; exit 1"
    monkeypatch.setenv("PATH", str(_bin(tmp_path, script)))
    assert read_pull_request(tmp_path, "topic") is None


def test_any_other_gh_failure_names_its_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = "printf '\\nnone of the git remotes point to a known host\\nmore\\n' >&2; exit 1"
    monkeypatch.setenv("PATH", str(_bin(tmp_path, script)))
    assert read_pull_request(tmp_path, "topic") == (
        "gh failed · none of the git remotes point to a known host"
    )


def test_a_silent_gh_failure_says_it_gave_no_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(_bin(tmp_path, "exit 2")))
    assert read_pull_request(tmp_path, "topic") == "gh failed without saying why"


def test_a_gh_answer_of_another_shape_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(_bin(tmp_path, "echo '{\"number\": 1}'")))
    assert read_pull_request(tmp_path, "topic") == GH_SHAPE


def test_a_gh_that_outruns_its_timeout_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(_bin(tmp_path, f"exec {SLEEP} 5")))
    monkeypatch.setattr(rr, "GH_TIMEOUT_SECONDS", 0.2)
    assert read_pull_request(tmp_path, "topic") == "gh did not answer within 0.2 s"


def test_gh_is_asked_for_the_branch_without_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "argv"
    script = f'echo "$@ $GH_PROMPT_DISABLED" > {record}; {CAT} {FIXTURES / "open_reviewed.json"}'
    monkeypatch.setenv("PATH", str(_bin(tmp_path, script)))
    pr = read_pull_request(tmp_path, "topic")
    assert pr is not None and not isinstance(pr, str)
    assert pr.number == 418
    assert record.read_text().split() == ["pr", "view", "topic", "--json", rr.GH_FIELDS, "1"]


# ---------- the cached whole ----------


@pytest.mark.usefixtures("git_env")
def test_the_whole_read_carries_both_halves_and_calls_gh_once_per_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone(tmp_path)
    calls = tmp_path / "calls"
    script = f"echo x >> {calls}; {CAT} {FIXTURES / 'open_reviewed.json'}"
    monkeypatch.setenv("PATH", f"{_bin(tmp_path, script)}")
    first = read_repository(clone)
    second = read_repository(clone)
    assert first == second
    assert first.branch is not None and first.branch.branch == "main"
    assert first.pull_request is not None and first.pull_request.number == 418
    assert calls.read_text().count("x") == 1
    monkeypatch.setattr(rr, "PULL_REQUEST_TTL_SECONDS", 0.0)
    read_repository(clone)
    assert calls.read_text().count("x") == 2


@pytest.mark.usefixtures("git_env")
def test_a_missing_gh_leaves_the_branch_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone(tmp_path)
    monkeypatch.setenv("PATH", str(_bin(tmp_path, None)))
    answer = read_repository(clone)
    assert answer.branch is not None
    assert (answer.pull_request, answer.pull_request_unread) == (None, GH_MISSING)


@pytest.mark.usefixtures("git_env")
def test_a_detached_head_looks_no_pull_request_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone(tmp_path)
    _run(clone, "checkout", "--detach")
    monkeypatch.setenv("PATH", str(_bin(tmp_path, "exit 99")))
    assert read_repository(clone).pull_request_unread == DETACHED


@pytest.mark.usefixtures("git_env")
def test_an_unreadable_checkout_leaves_both_halves_unread(tmp_path: Path) -> None:
    answer = read_repository(tmp_path)
    assert (answer.branch_unread, answer.pull_request_unread) == (
        NOT_A_REPOSITORY,
        NOT_A_REPOSITORY,
    )
