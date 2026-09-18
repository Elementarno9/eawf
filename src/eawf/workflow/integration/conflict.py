"""Blocking an attempt that could not apply, and where its resolution goes.

A conflict is not a prompt to open a terminal. The canonical workspace
and the isolated integration workspace are both the daemon's, and neither
is a place an operator edits, so a blocked attempt has to leave behind
something a surface can read instead: both sides of every hunk, who wrote
each side, and the one place the resolution happens. That last part is
what makes the frame usable rather than merely informative -- a conflict
with no way out invites somebody to go and fix it by hand in a tree
nobody is supposed to touch.

The exit is therefore derived rather than chosen. Each cause routes to
exactly one exit kind, the routes are compiled at import, and the table
is checked both ways: a cause with no route is a startup failure, and so
is an exit kind no cause reaches, because an unreachable exit is a
promise the console makes and the daemon never keeps.

Blocking moves no ref. The attempt enters its terminal blocked state and
the record is written; nothing here authors a commit, selects a
generation or touches a workspace, so the canonical history is exactly
what it was before the candidate was tried.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.delivery.integration import (
    ConflictExit,
    ConflictExitKind,
    ConflictFile,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    IntegrationConflict,
    IntegrationConflictKey,
    IntegrationFailureKind,
)
from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.state.epoch2.base import BranchName, StrictNonNegativeInt
from eawf.kernel.state.epoch2.urns import EvidenceUrn, RepositoryUrn
from eawf.runtime.integration.generations import transition_attempt

logger = logging.getLogger(__name__)


class ConflictCause(StrEnum):
    """Why a candidate could not be applied over the selected Batch base.

    The cause is read off the attempt's own bindings rather than guessed
    from the diff, so two attempts that failed the same way route to the
    same exit however different their hunks look.
    """

    BASE_MOVED = "base_moved"
    CLAIMED_SURFACE = "claimed_surface"
    CANDIDATE_TEXT = "candidate_text"


class ExitRouteError(ValueError):
    """The exit routing table does not pair causes and exits one to one."""


def compile_exit_routes(
    routes: Mapping[ConflictCause, ConflictExitKind],
) -> Mapping[ConflictCause, ConflictExitKind]:
    """Return *routes* once every cause leaves and every exit is reached.

    Args:
        routes: One exit kind per cause.

    Returns:
        The table, as a read-only mapping.

    Raises:
        ExitRouteError: A cause routes nowhere, which would leave a
            conflict frame with no way out, or an exit kind is reached by
            no cause, which would make it a promise nothing keeps.
    """
    missing = sorted(cause.value for cause in ConflictCause if cause not in routes)
    if missing:
        raise ExitRouteError(f"no exit declared for conflict cause {', '.join(missing)}")
    unreached = sorted(kind.value for kind in ConflictExitKind if kind not in set(routes.values()))
    if unreached:
        raise ExitRouteError(f"no conflict cause reaches exit {', '.join(unreached)}")
    return MappingProxyType(dict(routes))


#: Where each cause's resolution lands. A candidate written against a
#: base that has since moved is rebased; a collision on a surface two
#: plans both claimed is an operator's call, because neither side is
#: wrong; anything else is the candidate's own text and is repaired.
EXIT_BY_CAUSE: Final[Mapping[ConflictCause, ConflictExitKind]] = compile_exit_routes(
    {
        ConflictCause.BASE_MOVED: ConflictExitKind.REBASE_TASK,
        ConflictCause.CLAIMED_SURFACE: ConflictExitKind.OPERATOR_DECISION,
        ConflictCause.CANDIDATE_TEXT: ConflictExitKind.REPAIR_TASK,
    }
)


def conflict_cause(attempt: IntegrationAttempt) -> ConflictCause:
    """Return why *attempt* could not apply, from its own bindings.

    Args:
        attempt: The attempt that conflicted.

    Returns:
        The cause, which :data:`EXIT_BY_CAUSE` routes to one exit kind.
    """
    if attempt.source_base.head_sha != attempt.selected_batch_base.head_sha:
        return ConflictCause.BASE_MOVED
    if attempt.conflict_claim_ids:
        return ConflictCause.CLAIMED_SURFACE
    return ConflictCause.CANDIDATE_TEXT


#: How the caller mints the record one exit kind must reference: a Task
#: for a repair or a rebase, a pending action for an operator decision.
ConflictExitFactory = Callable[[ConflictExitKind], QualifiedUrn]


class BlockedIntegration(BaseModel):
    """One blocked attempt and the conflict frame written about it.

    Attributes:
        attempt: The attempt, now terminal and blocked on a conflict.
        conflict: The read-only frame naming both sides and the exit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: IntegrationAttempt
    conflict: IntegrationConflict

    @model_validator(mode="after")
    def _frame_describes_the_attempt(self) -> Self:
        """Require the frame and the attempt to be about one conflict.

        Raises:
            ValueError: The record does not describe the attempt, or the
                attempt is not blocked on a conflict.
        """
        if self.attempt.status is not IntegrationAttemptStatus.BLOCKED:
            raise ValueError(f"attempt {self.attempt.id!r} is not blocked")
        self.conflict.require_attempt(self.attempt)
        return self


def block_attempt(
    attempt: IntegrationAttempt,
    *,
    conflict_id: IntegrationConflictKey,
    repository_ref: RepositoryUrn,
    branch: BranchName,
    ahead: StrictNonNegativeInt,
    behind: StrictNonNegativeInt,
    files: Sequence[ConflictFile],
    diagnostic_ref: EvidenceUrn,
    exits: ConflictExitFactory,
    at: datetime,
) -> BlockedIntegration:
    """Block *attempt* on a conflict and write the frame it leaves behind.

    Args:
        attempt: The attempt that conflicted, in its applying state.
        conflict_id: The key of the record being written.
        repository_ref: The repository the Batch lives in.
        branch: The integration branch the conflict was seen on.
        ahead: How far the candidate is ahead of the selected base.
        behind: How far it is behind it.
        files: Every conflicting path with both sides of each hunk.
        diagnostic_ref: The evidence URN the frame is filed under.
        exits: How the caller mints the record the exit references.
        at: When the attempt was blocked.

    Returns:
        The terminal attempt and its conflict record. No commit is
        authored and no generation is selected, so no canonical ref moves.

    Raises:
        IllegalIntegrationTransitionError: The attempt cannot be blocked
            from the state it holds.
        ValueError: No conflicting file was named, or the frame does not
            describe the attempt.
        pydantic.ValidationError: The minted exit reference addresses
            another entity kind than the routed exit requires.
    """
    if not files:
        raise ValueError(f"attempt {attempt.id!r} cannot be blocked with no conflicting file")
    cause = conflict_cause(attempt)
    kind = EXIT_BY_CAUSE[cause]
    blocked = transition_attempt(
        attempt,
        to=IntegrationAttemptStatus.BLOCKED,
        at=at,
        failure_kind=IntegrationFailureKind.CONFLICT,
        diagnostic_ref=str(diagnostic_ref),
    )
    conflict = IntegrationConflict(
        id=conflict_id,
        attempt_id=attempt.id,
        batch_ref=attempt.batch_ref,
        repository_ref=repository_ref,
        branch=branch,
        ahead=ahead,
        behind=behind,
        files=tuple(files),
        exit=ConflictExit(kind=kind, ref=exits(kind)),
    )
    logger.info(
        f"block_attempt attempt_id={attempt.id} cause={cause.value} exit={kind.value} "
        f"files={len(conflict.files)}"
    )
    return BlockedIntegration(attempt=blocked, conflict=conflict)


__all__ = [
    "EXIT_BY_CAUSE",
    "BlockedIntegration",
    "ConflictCause",
    "ConflictExitFactory",
    "ExitRouteError",
    "block_attempt",
    "compile_exit_routes",
    "conflict_cause",
]
