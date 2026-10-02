"""What the tree's checkout and its pull request say: the branch, the review, the checks.

The Git surface draws a Batch's generations from the daemon's ledger, and beside them the
branch the tree has checked out and the pull request open for it. Neither is a record
eawf files: the branch is read from ``git`` and the pull request from the host's ``gh``
CLI, each time the console asks, through the read-only forms alone.

A read that cannot be made says why in the answer rather than failing it, because the
frame has to tell an operator which tool to install or log in to; "unavailable" with no
reason is the dead end this reader replaces. Each half is cached for a short while, since
the console re-reads a live read every second and ``gh`` is a network round trip.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.platform.subprocess_detach import no_window_kwargs

logger = logging.getLogger(__name__)

#: How long one ``git`` command may take. A healthy read answers in milliseconds; the cap
#: bounds a repository on a stalled network mount.
GIT_TIMEOUT_SECONDS: Final = 2.0

#: How long ``gh`` may take: it is a round trip to the host.
GH_TIMEOUT_SECONDS: Final = 10.0

#: How long a branch read is reused. A commit shows within this window.
BRANCH_TTL_SECONDS: Final = 5.0

#: How long a pull request read is reused, so a console on screen costs the host one
#: request a minute rather than one a second.
PULL_REQUEST_TTL_SECONDS: Final = 60.0

#: The fields ``gh pr view`` is asked for; :class:`_GhPullRequest` states each of them.
GH_FIELDS: Final = "number,state,reviewDecision,reviews,statusCheckRollup,url"

#: The exit status ``gh`` documents for a command that needs a login it does not hold.
GH_AUTH_REQUIRED: Final = 4

GIT_MISSING: Final = "git is not installed or not on the daemon's PATH"
NOT_A_REPOSITORY: Final = "the tree is not inside a git repository"
NO_COMMIT: Final = "the branch has no commit yet"
GH_MISSING: Final = "gh is not installed or not on the daemon's PATH · install the GitHub CLI"
GH_UNAUTHENTICATED: Final = "gh is not logged in to the host · run gh auth login"
GH_SHAPE: Final = "gh answered in a shape this reader does not know"
DETACHED: Final = "HEAD is detached · no branch to look a pull request up for"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BranchDrift(_Closed):
    """How far the branch is from one other ref.

    Attributes:
        ref: The ref compared against, such as ``origin/main``.
        ahead: Commits the branch has that the ref does not.
        behind: Commits the ref has that the branch does not.
        gone: Whether the ref was configured but no longer exists; the counts are then 0.
    """

    ref: str
    ahead: int = Field(ge=0)
    behind: int = Field(ge=0)
    gone: bool = False


class LastCommit(_Closed):
    """The commit ``HEAD`` points at."""

    sha: str
    subject: str
    committed_at: datetime


class BranchRead(_Closed):
    """The checkout, as ``git`` states it.

    Attributes:
        branch: The checked-out branch; ``None`` when ``HEAD`` is detached.
        head: The commit ``HEAD`` points at.
        upstream: The drift against the branch's upstream; ``None`` when it has none.
        default: The drift against the remote's default branch; ``None`` when the remote
            states none.
    """

    branch: str | None
    head: LastCommit
    upstream: BranchDrift | None = None
    default: BranchDrift | None = None


CheckOutcome = Literal["pass", "fail", "pending", "skipped"]


class CheckRead(_Closed):
    """One check reported on the pull request's head."""

    name: str
    outcome: CheckOutcome


class PullRequestRead(_Closed):
    """The pull request open for the branch, as the host states it.

    Attributes:
        number: The pull request number.
        state: ``OPEN``, ``CLOSED`` or ``MERGED``.
        url: Where it lives on the host.
        review_decision: The host's verdict over the reviews; ``None`` when it states none.
        approvals: Reviewers whose latest review approves.
        changes_requested: Reviewers whose latest review requests changes.
        checks: Every check on its head, in the order the host lists them.
    """

    number: int
    state: str
    url: str
    review_decision: str | None = None
    approvals: int = 0
    changes_requested: int = 0
    checks: tuple[CheckRead, ...] = ()


class RepositoryAnswer(_Closed):
    """The branch and its pull request, each with why it went unread when it did.

    A ``pull_request`` of ``None`` with no ``pull_request_unread`` reason is a fact the
    host stated: no pull request is open for the branch.
    """

    branch: BranchRead | None = None
    branch_unread: str | None = None
    pull_request: PullRequestRead | None = None
    pull_request_unread: str | None = None


class _GhAuthor(_Closed):
    login: str
    id: str | None = None
    name: str | None = None
    is_bot: bool | None = None


class _GhCommit(_Closed):
    oid: str


class _GhReview(_Closed):
    id: str | None = None
    author: _GhAuthor | None = None
    authorAssociation: str | None = None  # noqa: N815 -- gh's wire name
    body: str = ""
    submittedAt: datetime | None = None  # noqa: N815 -- gh's wire name
    includesCreatedEdit: bool = False  # noqa: N815 -- gh's wire name
    reactionGroups: list[object] = Field(default_factory=list)  # noqa: N815 -- gh's wire name
    state: str
    commit: _GhCommit | None = None


class _GhCheckRun(_Closed):
    typename: Literal["CheckRun"] = Field(alias="__typename")
    name: str
    status: str
    conclusion: str = ""
    workflowName: str = ""  # noqa: N815 -- gh's wire name
    detailsUrl: str = ""  # noqa: N815 -- gh's wire name
    startedAt: datetime | None = None  # noqa: N815 -- gh's wire name
    completedAt: datetime | None = None  # noqa: N815 -- gh's wire name


class _GhStatusContext(_Closed):
    typename: Literal["StatusContext"] = Field(alias="__typename")
    context: str
    state: str
    targetUrl: str = ""  # noqa: N815 -- gh's wire name
    startedAt: datetime | None = None  # noqa: N815 -- gh's wire name


_GhCheck = Annotated[_GhCheckRun | _GhStatusContext, Field(discriminator="typename")]


class _GhPullRequest(_Closed):
    number: int
    state: str
    url: str
    reviewDecision: str = ""  # noqa: N815 -- gh's wire name
    reviews: list[_GhReview] = Field(default_factory=list)
    statusCheckRollup: list[_GhCheck] = Field(default_factory=list)  # noqa: N815 -- gh's wire name


_PASSING: Final = frozenset({"SUCCESS", "NEUTRAL"})
_SKIPPED: Final = frozenset({"SKIPPED", "STALE"})
_PENDING_STATES: Final = frozenset({"PENDING", "EXPECTED"})


def _outcome(check: _GhCheckRun | _GhStatusContext) -> CheckOutcome:
    """Return one check's outcome; anything neither passed, skipped nor still running failed."""
    if isinstance(check, _GhCheckRun):
        if check.status != "COMPLETED":
            return "pending"
        verdict = check.conclusion
    else:
        if check.state in _PENDING_STATES:
            return "pending"
        verdict = check.state
    if verdict in _PASSING:
        return "pass"
    return "skipped" if verdict in _SKIPPED else "fail"


def parse_pull_request(raw: str) -> PullRequestRead:
    """Return the pull request ``gh pr view --json`` answered with *raw*.

    Each reviewer is counted once, by their latest approving or change-requesting review,
    because a reviewer who requested changes and later approved has approved.

    Raises:
        ValidationError: *raw* is not the shape :data:`GH_FIELDS` asks for.
    """
    answer = _GhPullRequest.model_validate_json(raw)
    latest: dict[str, str] = {}
    for review in answer.reviews:
        if review.state in ("APPROVED", "CHANGES_REQUESTED"):
            latest[review.author.login if review.author else ""] = review.state
    verdicts = list(latest.values())
    return PullRequestRead(
        number=answer.number,
        state=answer.state,
        url=answer.url,
        review_decision=answer.reviewDecision or None,
        approvals=verdicts.count("APPROVED"),
        changes_requested=verdicts.count("CHANGES_REQUESTED"),
        checks=tuple(
            CheckRead(
                name=check.name if isinstance(check, _GhCheckRun) else check.context,
                outcome=_outcome(check),
            )
            for check in answer.statusCheckRollup
        ),
    )


def _git(root: Path, *args: str) -> str | None:
    """Return the stripped output of one read-only git command, ``None`` when it failed.

    Raises:
        FileNotFoundError: ``git`` is not on ``PATH``.
        subprocess.TimeoutExpired: The command outran :data:`GIT_TIMEOUT_SECONDS`.
    """
    done: subprocess.CompletedProcess[str] = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
        **no_window_kwargs(),
    )
    return done.stdout.strip() if done.returncode == 0 else None


def _drift(root: Path, ref: str) -> BranchDrift:
    """Return how far ``HEAD`` is from *ref*, counted with ``git log`` over both ranges."""
    ahead = _git(root, "log", "--format=%h", f"{ref}..HEAD") or ""
    behind = _git(root, "log", "--format=%h", f"HEAD..{ref}") or ""
    return BranchDrift(ref=ref, ahead=len(ahead.split()), behind=len(behind.split()))


def _upstream(root: Path, branch: str) -> BranchDrift | None:
    """Return the drift against *branch*'s upstream, or ``None`` when it tracks none."""
    line = _git(
        root,
        "for-each-ref",
        "--format=%(upstream:short)%00%(upstream:track,nobracket)",
        f"refs/heads/{branch}",
    )
    ref, _, track = (line or "").partition("\0")
    if not ref:
        return None
    if track == "gone":
        return BranchDrift(ref=ref, ahead=0, behind=0, gone=True)
    counts = {
        word: int(count) for word, count in (part.split(" ") for part in track.split(", ") if part)
    }
    return BranchDrift(ref=ref, ahead=counts.get("ahead", 0), behind=counts.get("behind", 0))


def read_branch(root: Path) -> BranchRead | str:
    """Return the checkout of the repository *root* sits in, or why it cannot be read.

    Args:
        root: Any directory inside the working tree.

    Returns:
        The branch, its last commit and its drift against its upstream and against the
        remote's default branch (``origin/HEAD``); or the sentence saying why not.
    """
    try:
        if _git(root, "rev-parse", "--show-toplevel") is None:
            return NOT_A_REPOSITORY
        last = _git(root, "log", "-1", "--format=%H%x00%s%x00%cI")
        if not last:
            return NO_COMMIT
        sha, subject, at = last.split("\0")
        name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        branch = None if name in (None, "HEAD") else name
        default = _git(root, "rev-parse", "--abbrev-ref", "origin/HEAD")
        return BranchRead(
            branch=branch,
            head=LastCommit(sha=sha, subject=subject, committed_at=datetime.fromisoformat(at)),
            upstream=_upstream(root, branch) if branch else None,
            default=_drift(root, default) if default else None,
        )
    except FileNotFoundError:
        return GIT_MISSING
    except subprocess.TimeoutExpired:
        return f"git did not answer within {GIT_TIMEOUT_SECONDS:g} s"


def _gh_failure(stderr: str) -> str:
    """Return the sentence a failed ``gh`` call is drawn as: its first line of complaint."""
    first = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    return f"gh failed · {first}" if first else "gh failed without saying why"


def read_pull_request(root: Path, branch: str) -> PullRequestRead | str | None:
    """Return the pull request open for *branch*, ``None`` when there is none, or why not.

    Args:
        root: The working tree ``gh`` resolves the host repository from.
        branch: The branch the pull request is looked up for.

    Returns:
        The pull request; ``None`` when the host states none is open for *branch*; or
        the sentence naming the tool that is missing or not logged in, or what failed.
    """
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
    try:
        done: subprocess.CompletedProcess[str] = subprocess.run(
            ["gh", "pr", "view", branch, "--json", GH_FIELDS],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=GH_TIMEOUT_SECONDS,
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return GH_MISSING
    except subprocess.TimeoutExpired:
        return f"gh did not answer within {GH_TIMEOUT_SECONDS:g} s"
    if done.returncode == GH_AUTH_REQUIRED:
        return GH_UNAUTHENTICATED
    if done.returncode != 0:
        if "no pull requests found" in done.stderr:
            return None
        return _gh_failure(done.stderr)
    try:
        return parse_pull_request(done.stdout)
    except ValidationError as exc:
        logger.warning(f"repository_read gh shape errors={exc.error_count()}")
        return GH_SHAPE


_lock = threading.Lock()
_branches: dict[Path, tuple[float, BranchRead | str]] = {}
_pull_requests: dict[tuple[Path, str], tuple[float, PullRequestRead | str | None]] = {}


def read_repository(root: Path) -> RepositoryAnswer:
    """Return the branch of the tree at *root* and the pull request open for it.

    Each half is reused while it is younger than its TTL, so a console that re-reads every
    second spawns ``git`` every few seconds and calls the host once a minute. One read runs
    at a time, so two consoles on one daemon share a read rather than racing two.

    Args:
        root: The working tree, the parent of its ``.ea`` directory.

    Returns:
        The answer, with each half's reason when it went unread.
    """
    with _lock:
        now = time.monotonic()
        held = _branches.get(root)
        if held is None or now - held[0] >= BRANCH_TTL_SECONDS:
            held = (now, read_branch(root))
            _branches[root] = held
        branch = held[1]
        if isinstance(branch, str):
            return RepositoryAnswer(branch_unread=branch, pull_request_unread=branch)
        if branch.branch is None:
            return RepositoryAnswer(branch=branch, pull_request_unread=DETACHED)
        key = (root, branch.branch)
        cached = _pull_requests.get(key)
        if cached is None or now - cached[0] >= PULL_REQUEST_TTL_SECONDS:
            cached = (now, read_pull_request(root, branch.branch))
            _pull_requests[key] = cached
    found = cached[1]
    logger.debug(f"read_repository branch={branch.branch} pr={type(found).__name__}")
    if isinstance(found, str):
        return RepositoryAnswer(branch=branch, pull_request_unread=found)
    return RepositoryAnswer(branch=branch, pull_request=found)


__all__ = [
    "BRANCH_TTL_SECONDS",
    "DETACHED",
    "GH_FIELDS",
    "GH_MISSING",
    "GH_SHAPE",
    "GH_UNAUTHENTICATED",
    "GIT_MISSING",
    "NOT_A_REPOSITORY",
    "NO_COMMIT",
    "PULL_REQUEST_TTL_SECONDS",
    "BranchDrift",
    "BranchRead",
    "CheckOutcome",
    "CheckRead",
    "LastCommit",
    "PullRequestRead",
    "RepositoryAnswer",
    "parse_pull_request",
    "read_branch",
    "read_pull_request",
    "read_repository",
]
