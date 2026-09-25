"""The daemon-owned git worktree one Batch integration runs in.

A worker's candidate reaches integration as a commit, and a commit is
only worth naming while something keeps it alive. The leased worktree's
branch is deleted when the lease is reconciled, so a submission pins the
commit it names under ``refs/eawf/candidates/<candidate>`` at the moment
it is recorded. The pin is taken only when the named commit is the leased
worktree's own HEAD and descends from the base the lease was issued at:
a claim can therefore only ever name work that was done in the workspace
the daemon handed out, and never a commit borrowed from somewhere else.

Integration then runs in a worktree of its own, detached, under the
root's local store. Detached because it must move no branch anybody
reads: the only refs it writes are the pinned delivery under
``refs/eawf/deliveries/<manifest>``, and only once the commit exists. Each
candidate is squash-merged onto the tree and recorded as a scratch commit
so the next one merges against a clean index; the delivery commit is then
authored straight onto the base with the accumulated tree, so none of the
scratch commits is reachable from what is delivered. Commits are made
with plumbing rather than ``git commit``: hooks are a person's policy
over their own commits, and the message of a delivery is already
governed by the commit policy that rendered it.

The worktree is removed when the context closes, delivered or blocked.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import eawf.runtime.worktree.git as git
from eawf.kernel.delivery.integration import (
    MAX_CONFLICT_SIDE_LINES,
    AgentAuthority,
    ConflictFile,
    ConflictHunk,
    ConflictSide,
)
from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.runtime.candidate import CandidateBundle, CandidateRefusal
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.integration.apply import (
    ApplyDisposition,
    CandidateApplication,
    IntegrationRefusal,
    IntegrationRefusedError,
)
from eawf.runtime.integration.commit_policy import DeliveryCommit
from eawf.runtime.integration.recovery import DeliveredRevision
from eawf.runtime.workspace.lease import workspace_path
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)


#: Where integration worktrees are materialized, relative to the tree
#: root. Under the local store, because the worktree lives on this
#: machine, and apart from the leased workspaces so the two never share
#: a path.
INTEGRATION_LOCATOR: Final = "local/epoch2/integration"

#: The ref namespace a submitted candidate's commit is pinned under.
CANDIDATE_REF_PREFIX: Final = "refs/eawf/candidates/"

#: The ref namespace a delivery commit is pinned under.
DELIVERY_REF_PREFIX: Final = "refs/eawf/deliveries/"

#: The artifact reference a submission names its commit by.
COMMIT_ARTIFACT_PREFIX: Final = "artifact://git/commit/"

#: The one spelling of a commit a submission may name: the full object
#: name, so an abbreviation cannot come to mean a second commit later.
_COMMIT_NAME: Final = re.compile(r"^[0-9a-f]{40}$")

#: The longest line one conflict side may carry, per the conflict model.
_CONFLICT_LINE_WIDTH: Final = 1000

#: The message of a scratch commit. Never reachable from a delivery.
_STEP_MESSAGE: Final = "eawf integration step\n"


def submission_commit(submission_ref: str) -> str | None:
    """Return the commit *submission_ref* names, or ``None`` if it names none.

    Args:
        submission_ref: The artifact reference a submission carries.

    Returns:
        The 40-character object name, or ``None`` when the reference is
        not an ``artifact://git/commit/<sha>`` reference.
    """
    if not submission_ref.startswith(COMMIT_ARTIFACT_PREFIX):
        return None
    sha = submission_ref.removeprefix(COMMIT_ARTIFACT_PREFIX)
    return sha if _COMMIT_NAME.fullmatch(sha) else None


def candidate_pin_ref(candidate_ref: str) -> str:
    """Return the ref one candidate's commit is pinned under."""
    return f"{CANDIDATE_REF_PREFIX}{candidate_ref}"


def delivery_pin_ref(manifest_id: str) -> str:
    """Return the ref one delivery commit is pinned under."""
    return f"{DELIVERY_REF_PREFIX}{manifest_id}"


class CandidatePinError(ValueError):
    """A submission names a commit the daemon will not pin.

    Attributes:
        code: The stable refusal code a caller routes on.
        detail: The operator-facing sentence, without the code prefix, so
            a caller that reports code and detail as separate fields does
            not have to parse them back out of the combined message.
    """

    def __init__(self, code: CandidateRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads."""
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


def pin_candidate(
    repo_root: Path,
    *,
    workspace: Path,
    candidate_ref: str,
    submission_ref: str,
    base_commit: str,
) -> str:
    """Pin the commit a submission names, once it is the lease's own work.

    Args:
        repo_root: The repository the leased worktree belongs to.
        workspace: The leased worktree's directory.
        candidate_ref: The candidate the pin is named after.
        submission_ref: The artifact reference the submission carries.
        base_commit: The commit the lease was materialized at.

    Returns:
        The pinned commit.

    Raises:
        CandidatePinError: The reference names no commit, names a commit
            that is not the leased worktree's HEAD, or names one that does
            not strictly descend from the lease's base. Nothing is pinned.
        StateConflict: Git refused the ref update.
    """
    sha = submission_commit(submission_ref)
    if sha is None:
        raise CandidatePinError(
            CandidateRefusal.SUBMISSION_NOT_COMMIT,
            f"the submission must name a commit as {COMMIT_ARTIFACT_PREFIX}<sha>, so the daemon "
            "can hold the work it is asked to integrate",
        )
    try:
        head = git.commit_sha(workspace, "HEAD")
    except cli_errors.CliError:
        head = None
    if head != sha:
        raise CandidatePinError(
            CandidateRefusal.SUBMISSION_NOT_HEAD,
            "the submission names another commit than the leased workspace's HEAD, so it is not "
            "work that workspace produced",
        )
    if sha == base_commit or not git.is_ancestor(repo_root, ancestor=base_commit, descendant=sha):
        raise CandidatePinError(
            CandidateRefusal.SUBMISSION_OFF_BASE,
            "the submitted commit does not descend from the base the lease was issued at, so it "
            "carries no work on that base",
        )
    git.update_ref(repo_root, ref=candidate_pin_ref(candidate_ref), sha=sha)
    logger.info(f"pin_candidate pinned candidate={candidate_ref}")
    return sha


def pin_submission_commit(
    context: Epoch2RootContext,
    *,
    lease: WorkLease,
    candidate_ref: str,
    submission_ref: str,
) -> str:
    """Pin a submission's commit, resolving the repo and workspace off the lease.

    The one entry point every submission surface pins through, so a
    worktree submitted over ``runtime.candidate.submit`` or the semantic
    ``submit_candidate`` tool is held and checked by the same rule.

    Args:
        context: The native context of the canary the workspace belongs to.
        lease: The Run's active lease. Its workspace handle and base commit
            are what the pin is resolved and checked against.
        candidate_ref: The candidate the pin is named after.
        submission_ref: The artifact reference the submission carries.

    Returns:
        The pinned commit.

    Raises:
        CandidatePinError: See :func:`pin_candidate`.
        StateConflict: Git refused the ref update.
    """
    return pin_candidate(
        context.identity.tree_root.parent,
        workspace=workspace_path(context, handle=lease.workspace_handle),
        candidate_ref=candidate_ref,
        submission_ref=submission_ref,
        base_commit=lease.base_commit,
    )


class GitIntegrationWorkspace:
    """One detached git worktree an integration materializes, applies and commits in.

    Used as a context manager: the worktree is removed on exit whether
    the integration delivered, blocked or raised.
    """

    def __init__(self, *, repo_root: Path, directory: Path, batch_ref: BatchUrn) -> None:
        """Bind the workspace to its repository, directory and Batch.

        Args:
            repo_root: The repository the worktree is added to.
            directory: Where the worktree is materialized. Must not exist.
            batch_ref: The Batch whose integration this is, which both
                sides of a conflict are attributed to.
        """
        self._repo_root = repo_root
        self._directory = directory
        self._batch_ref = batch_ref
        self._base: str | None = None
        self._patches: list[str] = []

    @property
    def directory(self) -> Path:
        """Return where the worktree is materialized."""
        return self._directory

    def __enter__(self) -> Self:
        """Return the workspace, not yet materialized."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Remove the worktree, whatever the block concluded."""
        self.close()

    def materialize(self, *, base_commit: str) -> str:
        """Put the worktree at *base_commit* and return the head it reads.

        A second call moves the same worktree rather than adding another,
        which is what a delivery unit of one commit per Task asks for.

        Raises:
            UserError: The base does not resolve.
            StateConflict: Git could not add or reset the worktree.
        """
        if self._directory.exists():
            self._git("reset", "--quiet", "--hard", base_commit)
        else:
            self._directory.parent.mkdir(parents=True, exist_ok=True)
            git.worktree_add(self._repo_root, branch=None, path=self._directory, base=base_commit)
        self._base = base_commit
        self._patches = []
        return git.commit_sha(self._directory, "HEAD")

    def apply(self, bundle: CandidateBundle) -> CandidateApplication:
        """Squash-merge one pinned candidate onto the tree.

        Returns:
            ``applied`` when the merge was clean, or ``conflicted`` with
            every conflicting file when it was not. A conflicted merge is
            reset away, so the tree is left as it was before the call.

        Raises:
            IntegrationRefusedError: The candidate's commit is not pinned
                where its submission was, so there is nothing to apply.
            StateConflict: The merge failed for a reason other than a
                content conflict.
        """
        sha = self._pinned(bundle)
        head = git.commit_sha(self._directory, "HEAD")
        ahead = self._count(f"{head}..{sha}")
        behind = self._count(f"{sha}..{head}")
        merged = git.invoke(
            self._directory, "-c", "merge.conflictStyle=diff3", "merge", "--squash", sha
        )
        if merged.returncode == 0:
            self._step()
            self._patches.append(
                git.diff_digest(self._repo_root, base_sha=bundle.base_commit, head_sha=sha)
            )
            return CandidateApplication(
                candidate_ref=bundle.candidate_ref,
                disposition=ApplyDisposition.APPLIED,
                ahead=ahead,
                behind=behind,
            )
        paths = sorted(self._git("diff", "--name-only", "--diff-filter=U").splitlines())
        if not paths:
            raise cli_errors.StateConflict(
                f"git merge --squash of candidate {bundle.candidate_ref} failed "
                f"(rc={merged.returncode}): {(merged.stderr or merged.stdout).strip()}",
                kind="IntegrityViolation",
            )
        files = tuple(self._conflict_file(path, ours=head, theirs=sha) for path in paths)
        self._git("reset", "--quiet", "--hard", "HEAD")
        logger.info(f"apply conflicted candidate={bundle.candidate_ref} files={len(files)}")
        return CandidateApplication(
            candidate_ref=bundle.candidate_ref,
            disposition=ApplyDisposition.CONFLICTED,
            conflict_files=files,
            ahead=ahead,
            behind=behind,
        )

    def commit(self, delivery: DeliveryCommit) -> DeliveredRevision:
        """Author the delivery commit on the base and pin it.

        Returns:
            The commit object, its tree, its parent and the digests of
            what it delivers.

        Raises:
            RuntimeError: Nothing was materialized, so there is no base.
            StateConflict: Git could not write the commit or its ref.
        """
        base = self._base
        if base is None:
            raise RuntimeError("a delivery is committed only after the workspace is materialized")
        tree = self._git("write-tree")
        head = self._git("commit-tree", tree, "-p", base, "-F", "-", input_text=delivery.message)
        git.update_ref(self._repo_root, ref=delivery_pin_ref(delivery.manifest_id), sha=head)
        self._git("reset", "--quiet", "--hard", head)
        logger.info(f"commit delivered manifest={delivery.manifest_id}")
        return DeliveredRevision(
            head_sha=head,
            tree_sha=tree,
            parent_sha=base,
            patch_digest=canonical_digest(self._patches),
            diff_digest=(
                f"sha256:{git.diff_digest(self._repo_root, base_sha=base, head_sha=head)}"
            ),
            tree_digest=canonical_digest({"git_tree": tree}),
        )

    def close(self) -> None:
        """Remove the worktree if one was materialized.

        A worktree git will not remove is deleted from disk and pruned
        instead, and the failure is logged rather than raised: cleanup
        runs in a ``finally``, where raising would hide why the
        integration itself ended.
        """
        if not self._directory.exists():
            return
        try:
            git.worktree_remove(self._repo_root, path=self._directory, force=True)
        except cli_errors.CliError as error:
            logger.warning(f"close removal_failed fallback=delete error={error}")
            shutil.rmtree(self._directory, ignore_errors=True)
            git.invoke(self._repo_root, "worktree", "prune")

    def _git(self, *args: str, input_text: str | None = None) -> str:
        """Run one git subcommand in the worktree and return its stdout.

        Raises:
            StateConflict: The subcommand exited non-zero.
        """
        result = git.invoke(self._directory, *args, input_text=input_text)
        if result.returncode != 0:
            raise cli_errors.StateConflict(
                f"git {args[0]} failed in the integration workspace (rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip() or 'unknown'}",
                kind="IntegrityViolation",
            )
        return result.stdout.strip()

    def _count(self, range_spec: str) -> int:
        """Return how many commits *range_spec* holds."""
        return int(self._git("rev-list", "--count", range_spec))

    def _step(self) -> None:
        """Record the applied candidate as a scratch commit on HEAD.

        The next squash merges against a clean index this way, and the
        scratch commit is never delivered: the delivery commit parents on
        the base directly.
        """
        tree = self._git("write-tree")
        step = self._git("commit-tree", tree, "-p", "HEAD", "-F", "-", input_text=_STEP_MESSAGE)
        self._git("reset", "--quiet", "--soft", step)

    def _pinned(self, bundle: CandidateBundle) -> str:
        """Return the pinned commit of *bundle*.

        Raises:
            IntegrationRefusedError: The submission names no commit, or no
                pin holds that commit under the candidate's name.
        """
        expected = submission_commit(bundle.submission_ref)
        try:
            pinned = git.commit_sha(self._repo_root, candidate_pin_ref(bundle.candidate_ref))
        except cli_errors.CliError:
            pinned = None
        if expected is None or pinned != expected:
            raise IntegrationRefusedError(
                IntegrationRefusal.CANDIDATE_UNRESOLVABLE,
                f"candidate {bundle.candidate_ref} has no pinned commit matching its submission, "
                "so there is no work to apply",
            )
        return expected

    def _conflict_file(self, path: str, *, ours: str, theirs: str) -> ConflictFile:
        """Return the conflict frame of one unmerged path.

        The hunks are read from the diff3 markers the merge wrote. A
        conflict with no markers -- one side deleted the file, or it is
        binary -- is framed as one hunk holding each side's whole file.
        """
        text = (self._directory / path).read_text(encoding="utf-8", errors="replace")
        regions = _marker_regions(text.splitlines())
        if not regions:
            regions = [(self._blob_lines(ours, path), self._blob_lines(theirs, path))]
        ours_at, theirs_at = self._committed_at(ours), self._committed_at(theirs)
        authority = AgentAuthority(kind="agent", batch_ref=self._batch_ref)
        return ConflictFile(
            path=path,
            hunks=tuple(
                ConflictHunk(
                    index=index,
                    ours=ConflictSide(
                        authority=authority, at=ours_at, sha=ours, lines=_bounded(mine)
                    ),
                    theirs=ConflictSide(
                        authority=authority, at=theirs_at, sha=theirs, lines=_bounded(other)
                    ),
                )
                for index, (mine, other) in enumerate(regions, start=1)
            ),
        )

    def _blob_lines(self, commit: str, path: str) -> list[str]:
        """Return the lines of *path* at *commit*, or none when it is absent there."""
        shown = git.invoke(self._directory, "show", f"{commit}:{path}")
        return shown.stdout.splitlines() if shown.returncode == 0 else []

    def _committed_at(self, commit: str) -> datetime:
        """Return when *commit* was committed."""
        return datetime.fromtimestamp(int(self._git("show", "-s", "--format=%ct", commit)), UTC)


def _marker_regions(lines: list[str]) -> list[tuple[list[str], list[str]]]:
    """Return the ours and theirs lines of every diff3 conflict region, in order."""
    regions: list[tuple[list[str], list[str]]] = []
    ours: list[str] = []
    theirs: list[str] = []
    side: str | None = None
    for line in lines:
        if line.startswith("<<<<<<< "):
            side, ours, theirs = "ours", [], []
        elif side is not None and line.startswith("||||||| "):
            side = "base"
        elif side is not None and line == "=======":
            side = "theirs"
        elif side is not None and line.startswith(">>>>>>> "):
            regions.append((ours, theirs))
            side = None
        elif side == "ours":
            ours.append(line)
        elif side == "theirs":
            theirs.append(line)
    return regions


def _bounded(lines: list[str]) -> tuple[str, ...]:
    """Return *lines* cut to the widths one conflict side may carry."""
    return tuple(line[:_CONFLICT_LINE_WIDTH] for line in lines[:MAX_CONFLICT_SIDE_LINES])


def git_integration_workspace(
    context: Epoch2RootContext, batch_ref: BatchUrn
) -> GitIntegrationWorkspace:
    """Return a fresh integration workspace under *context*'s local store.

    Args:
        context: The native context of the tree the Batch lives in. The
            repository is the tree root's parent.
        batch_ref: The Batch being integrated.

    Returns:
        The workspace, not yet materialized; enter it as a context
        manager so it is removed however the integration ends.

    Raises:
        UndeclaredPathError: The commit policy declares no row for the
            integration directory.
    """
    tree_root = context.identity.tree_root
    directory = context.declared_path(
        tree_root / INTEGRATION_LOCATOR / f"int-{secrets.token_hex(16)}"
    )
    return GitIntegrationWorkspace(
        repo_root=tree_root.parent, directory=directory, batch_ref=batch_ref
    )


__all__ = [
    "CANDIDATE_REF_PREFIX",
    "COMMIT_ARTIFACT_PREFIX",
    "DELIVERY_REF_PREFIX",
    "INTEGRATION_LOCATOR",
    "CandidatePinError",
    "GitIntegrationWorkspace",
    "candidate_pin_ref",
    "delivery_pin_ref",
    "git_integration_workspace",
    "pin_candidate",
    "submission_commit",
]
