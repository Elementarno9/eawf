"""The permanent-commit ancestry policy, and proof that it reds on real defects.

The history group builds a throwaway repository per case with real git:
a bootstrap, a disposable review checkpoint on a side branch, a delivery
parented on the bootstrap whose manifest cites the checkpoint, and an
observed publication. The defects are the ones this repository has
shipped or the specification forbids: a ``state: start`` commit per Task
start (this branch carried 53 of them before the status projection), a
delivery rebased onto its review checkpoint, and a delivery whose
provenance forgets the checkpoint.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.store.commit_census import run_ancestry_census
from eawf.kernel.store.commit_policy import (
    PERMANENT_COMMIT_ORDER,
    REVIEW_CHECKPOINT_TRAILER_KEY,
    AncestryFindingKind,
    HistoryCommit,
    PermanentCommitKind,
    ancestry_findings,
    classify_commit,
)
from tools.ea_commit_census import GIT_UNAVAILABLE_EXIT
from tools.ea_commit_census import main as census_main

_SHA_A, _SHA_B, _SHA_C = "a" * 40, "b" * 40, "c" * 40
_MANIFEST = "manifest://MAN-DELIVERY-001"


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* against an isolated config."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, message: str) -> str:
    """Write one file and commit it with *message*, returning the new head."""
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init(tmp_path: Path) -> tuple[Path, str]:
    """Return a repository holding only the bootstrap commit, and that commit."""
    repo = tmp_path / "product"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "canary@example.com")
    _git(repo, "config", "user.name", "canary")
    return repo, _commit(repo, "README", "chore: initialize hookguard repository")


def _checkpoint(repo: Path) -> str:
    """Commit the review checkpoint on a side branch and return to main."""
    _git(repo, "checkout", "-q", "-b", "review")
    sha = _commit(repo, "gen6.py", "feat: implement secure webhook verification")
    _git(repo, "checkout", "-q", "main")
    return sha


def _deliver(repo: Path, checkpoint: str | None) -> None:
    """Commit the verified delivery, citing *checkpoint* when one is given."""
    cited = "" if checkpoint is None else f"\n{REVIEW_CHECKPOINT_TRAILER_KEY}: {checkpoint}"
    _commit(
        repo,
        "gen7.py",
        f"feat: add secure webhook verification\n\nEawf-Provenance: {_MANIFEST}{cited}",
    )


def _publish(repo: Path) -> None:
    """Commit the observed publication record."""
    _commit(repo, "RELEASE", "chore: record observed v0.1.0 publication")


def _kinds(repo: Path, checkpoint: str) -> list[AncestryFindingKind]:
    """Return the finding kinds the census raises on main against *checkpoint*."""
    findings = run_ancestry_census(repo, branch="main", checkpoint=checkpoint)
    return [finding.kind for finding in findings]


def test_three_permanent_commits_and_a_disposable_checkpoint_pass(tmp_path: Path) -> None:
    repo, _ = _init(tmp_path)
    checkpoint = _checkpoint(repo)
    _deliver(repo, checkpoint)
    _publish(repo)
    assert _kinds(repo, checkpoint) == []


def test_a_per_task_status_commit_is_chatter(tmp_path: Path) -> None:
    repo, _ = _init(tmp_path)
    checkpoint = _checkpoint(repo)
    _commit(repo, "state.json", "state: start EAWF-0230, 0249 to 0251 and 0253")
    _deliver(repo, checkpoint)
    _publish(repo)
    kinds = _kinds(repo, checkpoint)
    assert kinds == [AncestryFindingKind.CHATTER_COMMIT, AncestryFindingKind.BROKEN_ANCESTRY]


def test_a_delivery_descended_from_its_checkpoint_is_refused(tmp_path: Path) -> None:
    repo, _ = _init(tmp_path)
    checkpoint = _checkpoint(repo)
    _git(repo, "merge", "-q", "--ff-only", "review")
    _deliver(repo, checkpoint)
    _publish(repo)
    kinds = _kinds(repo, checkpoint)
    assert AncestryFindingKind.CHECKPOINT_IN_ANCESTRY in kinds
    assert AncestryFindingKind.CHATTER_COMMIT in kinds


def test_a_delivery_that_does_not_cite_its_checkpoint_is_refused(tmp_path: Path) -> None:
    repo, _ = _init(tmp_path)
    checkpoint = _checkpoint(repo)
    _deliver(repo, None)
    _publish(repo)
    assert _kinds(repo, checkpoint) == [AncestryFindingKind.CHECKPOINT_UNCITED]


def test_a_missing_publication_breaks_the_permanent_order(tmp_path: Path) -> None:
    repo, _ = _init(tmp_path)
    checkpoint = _checkpoint(repo)
    _deliver(repo, checkpoint)
    assert _kinds(repo, checkpoint) == [AncestryFindingKind.PERMANENT_ORDER]


def test_the_census_tool_reds_on_chatter_and_passes_the_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = _init(tmp_path)
    checkpoint = _checkpoint(repo)
    _deliver(repo, checkpoint)
    _publish(repo)
    argv = ["--repo-root", str(repo), "--ancestry", "main", "--checkpoint", "review"]
    assert census_main(argv) == 0
    _commit(repo, "state.json", "state: start EAWF-0199")
    assert census_main(argv) == 1
    assert "chatter_commit" in capsys.readouterr().err


def test_the_census_tool_refuses_an_unresolvable_checkpoint(tmp_path: Path) -> None:
    repo, _ = _init(tmp_path)
    argv = ["--repo-root", str(repo), "--ancestry", "main", "--checkpoint", "no-such-ref"]
    assert census_main(argv) == GIT_UNAVAILABLE_EXIT


def test_the_census_tool_needs_both_ancestry_flags(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        census_main(["--repo-root", str(tmp_path), "--ancestry", "main"])


def test_an_empty_history_names_the_checkpoint() -> None:
    checkpoint = HistoryCommit(sha=_SHA_A, parents=(_SHA_B,), subject="feat: review")
    (finding,) = ancestry_findings(history=(), checkpoint=checkpoint)
    assert finding.kind is AncestryFindingKind.PERMANENT_ORDER
    assert finding.sha == _SHA_A
    assert finding.render().startswith("permanent_order ")


@pytest.mark.parametrize(
    ("commit", "expected"),
    [
        (HistoryCommit(sha=_SHA_A, subject="chore: initialize"), PermanentCommitKind.BOOTSTRAP),
        (
            HistoryCommit(sha=_SHA_B, parents=(_SHA_A,), subject="feat: x", provenance=_MANIFEST),
            PermanentCommitKind.DELIVERY,
        ),
        (
            HistoryCommit(
                sha=_SHA_C, parents=(_SHA_B,), subject="chore: record partial v0.1.0 publication"
            ),
            PermanentCommitKind.PUBLICATION,
        ),
        (HistoryCommit(sha=_SHA_C, parents=(_SHA_B,), subject="state: start EAWF-0199"), None),
        (
            HistoryCommit(
                sha=_SHA_C, parents=(_SHA_B,), subject="feat: x", provenance="urn:not-a-manifest"
            ),
            None,
        ),
    ],
)
def test_classify_commit(commit: HistoryCommit, expected: PermanentCommitKind | None) -> None:
    assert classify_commit(commit) is expected


def test_the_permanent_order_is_bootstrap_delivery_publication() -> None:
    assert [kind.value for kind in PERMANENT_COMMIT_ORDER] == [
        "bootstrap",
        "delivery",
        "publication",
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"sha": "not-a-sha", "subject": "x"},
        {"sha": _SHA_A, "subject": ""},
        {"sha": _SHA_A, "subject": "x", "parents": ["XYZ"]},
        {"sha": _SHA_A, "subject": "x", "unknown": 1},
    ],
)
def test_history_commit_refuses_malformed_input(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HistoryCommit.model_validate(fields)
