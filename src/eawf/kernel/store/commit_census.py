"""Run the ``.ea/`` commit declaration against what git actually holds.

:mod:`eawf.kernel.store.commit_policy` owns the declaration and the
comparison, and stays a pure function of its inputs so a fixture can
drive it. This module supplies the two inputs only git can answer: the
tracked set, and which of the declaration's probe paths the ignore rules
match.

The ignore probe passes ``--no-index`` deliberately. Without it git
suppresses any path already in the index, which hides exactly the case
the census exists to catch: a declared-committed file that is tracked
today only because it was added before the ignore rule landed, and that
a fresh clone plus ``git add`` would silently drop.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from eawf.kernel.store.commit_policy import (
    REVIEW_CHECKPOINT_TRAILER_KEY,
    AncestryFinding,
    CensusFinding,
    HistoryCommit,
    ancestry_findings,
    census_findings,
    probe_paths,
)
from eawf.runtime.integration.commit_policy import PROVENANCE_TRAILER_KEY

logger = logging.getLogger(__name__)

#: How long a single git invocation may take before the census gives up.
GIT_TIMEOUT_SECONDS: int = 30


class GitUnavailableError(RuntimeError):
    """The census could not read the repository state from git."""


def _git(
    repo_root: Path,
    *args: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_root*.

    Args:
        repo_root: The repository to run in.
        *args: The git arguments, without the leading ``git``.
        stdin: Text piped to the command, when it reads stdin.

    Returns:
        The completed process, with text streams captured.

    Raises:
        GitUnavailableError: The git binary is missing or did not finish
            inside :data:`GIT_TIMEOUT_SECONDS`.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise GitUnavailableError("the git binary is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailableError(f"git {args[0]} timed out in {repo_root}") from exc


def _tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Return every repo-relative path git tracks in *repo_root*.

    Args:
        repo_root: The repository to inspect.

    Returns:
        The tracked paths, in git's order.

    Raises:
        GitUnavailableError: git failed, or the directory is not a repo.
    """
    result = _git(repo_root, "ls-files", "-z")
    if result.returncode != 0:
        raise GitUnavailableError(f"git ls-files failed in {repo_root}: {result.stderr.strip()}")
    return tuple(entry for entry in result.stdout.split("\0") if entry)


def _ignored_probes(repo_root: Path, probes: tuple[str, ...]) -> tuple[str, ...]:
    """Return the probe paths an ignore rule in *repo_root* matches.

    Args:
        repo_root: The repository whose ignore rules are consulted.
        probes: The candidate paths to test.

    Returns:
        The matched subset, in git's order.

    Raises:
        GitUnavailableError: git exited with an error status. Exit 1 only
            means nothing matched, which is not an error.
    """
    if not probes:
        return ()
    result = _git(repo_root, "check-ignore", "--no-index", "--stdin", stdin="\n".join(probes))
    if result.returncode not in (0, 1):
        raise GitUnavailableError(f"git check-ignore failed: {result.stderr.strip()}")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def run_census(repo_root: Path) -> tuple[CensusFinding, ...]:
    """Return every disagreement between *repo_root* and the declaration.

    Args:
        repo_root: The repository to census.

    Returns:
        The findings; empty when the tree agrees with the declaration.

    Raises:
        GitUnavailableError: git could not be consulted.
    """
    probes = probe_paths()
    findings = census_findings(
        tracked=_tracked_paths(repo_root),
        ignored_probes=_ignored_probes(repo_root, probes),
    )
    logger.info(f"run_census repo_root={repo_root} probes={len(probes)} findings={len(findings)}")
    return findings


#: The format one commit is read in: object name, parents, subject, the
#: provenance trailer and the review-checkpoint trailers, NUL-separated so
#: no subject can split a field.
_COMMIT_FORMAT = (
    "%H%x00%P%x00%s%x00"
    f"%(trailers:key={PROVENANCE_TRAILER_KEY},valueonly,separator=)%x00"
    f"%(trailers:key={REVIEW_CHECKPOINT_TRAILER_KEY},valueonly,separator=%x20)%x00"
)


def _commits(repo_root: Path, *args: str) -> tuple[HistoryCommit, ...]:
    """Return the commits one ``git log`` invocation lists, in its order.

    Raises:
        GitUnavailableError: git exited with an error status.
    """
    result = _git(repo_root, "log", f"--format={_COMMIT_FORMAT}", *args)
    if result.returncode != 0:
        raise GitUnavailableError(f"git log failed in {repo_root}: {result.stderr.strip()}")
    commits: list[HistoryCommit] = []
    for record in result.stdout.splitlines():
        if not record:
            continue
        sha, parents, subject, provenance, cites, _ = record.split("\0")
        commits.append(
            HistoryCommit(
                sha=sha,
                parents=tuple(parents.split()),
                subject=subject,
                provenance=provenance or None,
                cites=tuple(cites.split()),
            )
        )
    return tuple(commits)


def run_ancestry_census(
    repo_root: Path, *, branch: str, checkpoint: str
) -> tuple[AncestryFinding, ...]:
    """Return every way *branch* disagrees with the permanent-commit policy.

    Args:
        repo_root: The product repository.
        branch: The default branch whose first-parent history is judged.
        checkpoint: The disposable review checkpoint, as any revision git
            resolves.

    Returns:
        The findings; empty when the branch carries exactly the three
        permanent commits and the checkpoint stays out of its ancestry.

    Raises:
        GitUnavailableError: git could not be consulted, or a revision
            does not resolve.
    """
    history = _commits(repo_root, "--first-parent", "--reverse", branch)
    (probe,) = _commits(repo_root, "-1", checkpoint)
    findings = ancestry_findings(history=history, checkpoint=probe)
    logger.info(
        f"run_ancestry_census repo_root={repo_root} branch={branch!r} "
        f"commits={len(history)} findings={len(findings)}"
    )
    return findings


__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "GitUnavailableError",
    "run_ancestry_census",
    "run_census",
]
