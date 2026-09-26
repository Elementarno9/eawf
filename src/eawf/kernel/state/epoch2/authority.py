"""Deciding which authority epoch one tree is in, and refusing native writes.

Epoch 2 is not something a caller asks for. A tree is in epoch 2 when it
carries two files, and only then: the declaration its owner wrote into it
-- a disposable canary, or a live repository's opt-in against a verified
backup -- and the epoch marker the cutover (or a canary's birth) writes
last. Either file alone is a tree that is still epoch 1 -- a declared tree
nobody activated, or an activated tree nobody declared -- and a native
mutation against either would be a second writer on a tree whose epoch-1
authority is still the real one.

The answer is read from the tree on every call rather than cached. A
tree's epoch changes exactly when one of the two files appears or goes
away, and a cached answer is the one that would still say "epoch 2" after
a rollback removed the marker.

Anything short of both files parsing cleanly resolves to epoch 1. That
is the conservative direction for the one question this module exists to
answer -- may a native mutator write here -- because an epoch-1 answer
refuses the write and leaves the tree alone.

The resolver reuses the canary fence rather than re-reading the files
itself: an epoch-2 answer carries the fence-cleared
:class:`~eawf.kernel.migration.epoch2.canary.DisposableTarget`, so a
native write path derives the generation it writes from the same proof
object the cutover writes through.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
    OPT_IN_DECLARATION_FILENAME,
    DisposableTarget,
)
from eawf.kernel.migration.epoch2.errors import MigrationTargetNotDisposableError
from eawf.kernel.migration.epoch2.generation import GENERATION_ID_PATTERN, read_marker
from eawf.kernel.state.epoch2.urns import RepositoryUrn
from eawf.kernel.state.ids import RE_PROJECT_CODE

logger = logging.getLogger(__name__)


#: The stable code a native mutation refused for want of epoch-2 authority
#: carries, on the wire and in every in-process raise.
NATIVE_AUTHORITY_REQUIRED: Final = "native_authority_required"


class AuthorityGap(StrEnum):
    """Why a tree was not granted epoch-2 authority.

    Attributes:
        UNDECLARED: The tree carries no valid declaration of either
            kind, or carries both. Absent, unreadable and malformed are one
            answer, as they are at the cutover's own fence.
        MARKER_ABSENT: The tree is declared but no epoch marker was
            written, so nothing activated it.
        MARKER_UNREADABLE: A marker exists but does not parse, so the
            tree cannot say which generation it would read from.
    """

    UNDECLARED = "undeclared"
    MARKER_ABSENT = "marker_absent"
    MARKER_UNREADABLE = "marker_unreadable"


class RootAuthority(BaseModel):
    """The authority epoch one tree resolved to.

    Attributes:
        root: The tree's root, the directory that holds the declaration
            and the ``generations/`` directory.
        epoch: ``2`` only when both files are present and valid.
        target: The fence-cleared tree at epoch 2, ``None`` at epoch 1.
            Every native write derives its paths from this object.
        generation_id: The generation the marker names at epoch 2,
            ``None`` at epoch 1.
        gap: Why epoch 2 was withheld, ``None`` at epoch 2.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    epoch: Literal[1, 2]
    target: DisposableTarget | None = None
    generation_id: Annotated[str, Field(pattern=GENERATION_ID_PATTERN)] | None = None
    gap: AuthorityGap | None = None

    @model_validator(mode="after")
    def _epoch_matches_evidence(self) -> Self:
        """Refuse an answer whose epoch disagrees with what backs it.

        Returns:
            The validated answer.

        Raises:
            ValueError: An epoch-2 answer lacks its target or generation,
                or names a gap; or an epoch-1 answer carries a target or
                generation, or names no gap.
        """
        granted = self.target is not None and self.generation_id is not None
        carries = self.target is not None or self.generation_id is not None
        if self.epoch == 2 and (not granted or self.gap is not None):
            raise ValueError("an epoch-2 answer needs a target and a generation and no gap")
        if self.epoch == 1 and (self.gap is None or carries):
            raise ValueError("an epoch-1 answer names its gap and carries no target or generation")
        return self


class CanaryRepositoryRef(BaseModel):
    """The typed reference a checkpoint configuration declares a canary by.

    It carries no filesystem path, so it can sit in a committed
    configuration: the repository URN is what membership references
    resolve inside, and the project code is the row the explicit
    registry locates the canary's tree by on one machine.

    Attributes:
        repository: The canary's repository URN.
        project_code: The registry code the canary is registered under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: RepositoryUrn
    project_code: Annotated[str, Field(pattern=RE_PROJECT_CODE.pattern)]


class NativeAuthorityRequiredError(Exception):
    """A native mutation was aimed at a tree that is not in epoch 2.

    Attributes:
        code: The stable refusal code.
        gap: Which half of the evidence was missing.
    """

    code: ClassVar[str] = NATIVE_AUTHORITY_REQUIRED

    def __init__(self, *, root_name: str, gap: AuthorityGap) -> None:
        """Build the refusal for the tree named ``root_name``.

        Args:
            root_name: The tree root's directory name. Never the full
                path, so a refusal pasted into a ticket carries no home
                directory.
            gap: Why epoch 2 was withheld.
        """
        self.gap = gap
        super().__init__(
            f"{root_name} resolves to authority epoch 1 ({gap.value}): a native mutation "
            f"needs {CANARY_DECLARATION_FILENAME} or {OPT_IN_DECLARATION_FILENAME}, and "
            f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}, so nothing was written"
        )


def _epoch1(root: Path, gap: AuthorityGap) -> RootAuthority:
    """Return the epoch-1 answer for ``root``, logging why."""
    logger.debug(f"resolve_authority root={root.name} epoch=1 gap={gap.value}")
    return RootAuthority(root=root, epoch=1, gap=gap)


def resolve_authority(root: Path) -> RootAuthority:
    """Return the authority epoch the tree at ``root`` is in.

    Args:
        root: The tree's root directory. A directory that does not exist
            resolves to epoch 1, like any other undeclared tree.

    Returns:
        Epoch 2 with the fence-cleared target and the marked generation
        when the tree carries a valid declaration and a valid marker;
        epoch 1 with the gap otherwise. The resolver never raises for a
        missing or malformed file, because each of those is an answer.
    """
    resolved = Path(root)
    try:
        target = DisposableTarget.require(resolved)
    except MigrationTargetNotDisposableError:
        return _epoch1(resolved, AuthorityGap.UNDECLARED)
    try:
        marker = read_marker(target)
    except (OSError, ValueError) as error:
        logger.warning(
            f"resolve_authority root={resolved.name} marker_unreadable={error.__class__.__name__}"
        )
        return _epoch1(resolved, AuthorityGap.MARKER_UNREADABLE)
    if marker is None:
        return _epoch1(resolved, AuthorityGap.MARKER_ABSENT)
    logger.debug(f"resolve_authority root={resolved.name} epoch=2 gen={marker.generation_id}")
    return RootAuthority(root=resolved, epoch=2, target=target, generation_id=marker.generation_id)


def require_native_authority(root: Path) -> RootAuthority:
    """Return the epoch-2 answer for ``root``, or refuse the native write.

    Args:
        root: The tree's root directory.

    Returns:
        The epoch-2 answer, carrying the target the write derives from.

    Raises:
        NativeAuthorityRequiredError: The tree resolves to epoch 1. The
            check reads and writes nothing else, so a refused caller has
            left the tree exactly as it found it.
    """
    authority = resolve_authority(root)
    if authority.gap is not None:
        raise NativeAuthorityRequiredError(root_name=authority.root.name, gap=authority.gap)
    return authority


__all__ = [
    "NATIVE_AUTHORITY_REQUIRED",
    "AuthorityGap",
    "CanaryRepositoryRef",
    "NativeAuthorityRequiredError",
    "RootAuthority",
    "require_native_authority",
    "resolve_authority",
]
