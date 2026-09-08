"""Workspace resolution and membership algebra over the user registry.

A qualified URN needs a workspace key, and the key has to be derived
the same way from every surface (CLI verb, daemon RPC, importer) or two
surfaces will mint URNs against different workspaces for the same repo.
This module owns that single derivation: :func:`resolve_workspace`
implements the six-step ladder, and the mutation helpers
(:func:`create_workspace`, :func:`update_membership`) own the
compare-and-set rules that both the daemon mutator and the CLI's
daemonless fallback apply.

The ladder, highest precedence first:

1. An explicit operator-supplied key (``--workspace-key``).
2. The ``EAWF_WORKSPACE_KEY`` environment override.
3. The session-local selection made by ``eawf workspace select``.
4. Exact match of the caller's repository root against a registered
   repo path, yielding that repo's project code.
5. The workspaces whose declared membership contains that code -
   exactly one resolves.
6. Otherwise refuse: zero candidates is
   :data:`WORKSPACE_NOT_REGISTERED`, two or more is
   :data:`WORKSPACE_AMBIGUOUS` with the candidate keys attached.

Step 4 compares the root path itself and stops. It deliberately does
not walk toward the filesystem root looking for an enclosing registered
repo: an implicit parent match would grow the resolution surface beyond
what the operator explicitly registered, which is the same rule that
keeps the registry free of scan-based growth. An unregistered root
therefore refuses rather than silently resolving to its parent's
workspace.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from eawf.platform.registry.models import Registry, WorkspaceRecord

logger = logging.getLogger(__name__)


#: Stable error code: no registered workspace matches the request.
WORKSPACE_NOT_REGISTERED: Final[str] = "workspace_not_registered"

#: Stable error code: more than one registered workspace matches and the
#: caller made no selection.
WORKSPACE_AMBIGUOUS: Final[str] = "workspace_ambiguous"

#: Stable error code: ``create`` targeted a key that already exists.
WORKSPACE_ALREADY_REGISTERED: Final[str] = "workspace_already_registered"

#: Stable error code: a membership mutation lost the compare-and-set.
WORKSPACE_REVISION_CONFLICT: Final[str] = "workspace_revision_conflict"


class WorkspaceSource(StrEnum):
    """Which rung of the resolution ladder produced the answer."""

    EXPLICIT = "explicit"
    ENVIRONMENT = "environment"
    SESSION = "session"
    REPO_ROOT = "repo_root"


class WorkspaceResolution(BaseModel):
    """A resolved workspace plus the rung that resolved it.

    Attributes:
        key: The resolved workspace key.
        record: The registered record the key maps to.
        source: The ladder rung that produced the match, so a caller
            can tell an explicit selection from an inferred one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    record: WorkspaceRecord
    source: WorkspaceSource


class WorkspaceResolutionError(Exception):
    """Raised when :func:`resolve_workspace` cannot name one workspace.

    Attributes:
        code: One of :data:`WORKSPACE_NOT_REGISTERED` or
            :data:`WORKSPACE_AMBIGUOUS`. Callers route on this rather
            than on the message text.
        candidates: The competing workspace keys, sorted. Empty for
            :data:`WORKSPACE_NOT_REGISTERED`.
    """

    def __init__(self, *, code: str, message: str, candidates: Iterable[str] = ()) -> None:
        self.code = code
        self.candidates: tuple[str, ...] = tuple(candidates)
        super().__init__(message)


class WorkspaceMutationError(Exception):
    """Raised when a workspace create / membership edit is refused.

    Attributes:
        code: One of :data:`WORKSPACE_ALREADY_REGISTERED`,
            :data:`WORKSPACE_NOT_REGISTERED`, or
            :data:`WORKSPACE_REVISION_CONFLICT`.
    """

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _normalise_root(root: Path) -> Path:
    """Return *root* in the comparable form used for exact matching.

    ``expanduser`` + ``resolve`` collapse ``~``, relative segments and
    symlinked prefixes so a registered ``/tmp/...`` path still matches a
    caller who arrived via ``/private/tmp/...``. Neither call inspects
    directory contents, so this stays a pure path normalisation.
    """
    return Path(root).expanduser().resolve()


def project_codes_at_root(registry: Registry, repo_root: Path) -> frozenset[str]:
    """Return the project codes registered at exactly *repo_root*.

    Args:
        registry: Already-validated registry document.
        repo_root: Repository root the caller is standing in.

    Returns:
        The codes whose registered path normalises to *repo_root*.
        Empty when the root was never explicitly registered - no parent
        directory is consulted.
    """
    target = _normalise_root(repo_root)
    return frozenset(
        entry.code
        for entry in registry.repos.values()
        if _normalise_root(Path(entry.path)) == target
    )


def _resolve_declared(
    registry: Registry,
    *,
    key: str,
    source: WorkspaceSource,
) -> WorkspaceResolution:
    """Resolve a key the caller named outright, or refuse.

    Raises:
        WorkspaceResolutionError: With :data:`WORKSPACE_NOT_REGISTERED`
            when *key* is absent from the registry. A named key is
            never silently downgraded to an inferred one - a typo must
            surface rather than resolve to the wrong workspace.
    """
    record = registry.workspaces.get(key)
    if record is None:
        raise WorkspaceResolutionError(
            code=WORKSPACE_NOT_REGISTERED,
            message=(
                f"workspace {key!r} (from {source.value}) is not registered; "
                f"register it with `eawf workspace add {key}`"
            ),
        )
    return WorkspaceResolution(key=key, record=record, source=source)


def resolve_workspace(
    registry: Registry,
    *,
    explicit_key: str | None = None,
    env_key: str | None = None,
    session_key: str | None = None,
    repo_root: Path | None = None,
) -> WorkspaceResolution:
    """Resolve exactly one workspace via the six-step ladder.

    Args:
        registry: Already-validated registry document.
        explicit_key: Operator-supplied key (rung 1).
        env_key: ``EAWF_WORKSPACE_KEY`` value (rung 2).
        session_key: Session-local selection (rung 3).
        repo_root: Repository root to match exactly (rungs 4-6). When
            ``None`` the inference rungs are skipped entirely.

    Returns:
        The single :class:`WorkspaceResolution` the ladder produced.

    Raises:
        WorkspaceResolutionError: :data:`WORKSPACE_NOT_REGISTERED` when
            no rung matches; :data:`WORKSPACE_AMBIGUOUS` when the root
            belongs to more than one workspace and the caller made no
            selection.
    """
    for candidate, source in (
        (explicit_key, WorkspaceSource.EXPLICIT),
        (env_key, WorkspaceSource.ENVIRONMENT),
        (session_key, WorkspaceSource.SESSION),
    ):
        if candidate:
            return _resolve_declared(registry, key=candidate, source=source)

    if repo_root is None:
        raise WorkspaceResolutionError(
            code=WORKSPACE_NOT_REGISTERED,
            message=(
                "no workspace was named and no repository root was supplied; "
                "pass --workspace-key or run from a registered repository root"
            ),
        )

    member_codes = project_codes_at_root(registry, repo_root)
    candidates = sorted(
        key
        for key, record in registry.workspaces.items()
        if record.member_project_codes & member_codes
    )
    logger.debug(
        f"resolve_workspace repo_root={str(repo_root)!r} "
        f"member_codes={sorted(member_codes)!r} candidates={candidates!r}"
    )
    if len(candidates) == 1:
        key = candidates[0]
        return WorkspaceResolution(
            key=key,
            record=registry.workspaces[key],
            source=WorkspaceSource.REPO_ROOT,
        )
    if candidates:
        raise WorkspaceResolutionError(
            code=WORKSPACE_AMBIGUOUS,
            message=(
                f"repository root {str(repo_root)!r} belongs to {len(candidates)} workspaces "
                f"({', '.join(candidates)}); select one with `eawf workspace select <KEY>` "
                f"or pass --workspace-key"
            ),
            candidates=candidates,
        )
    raise WorkspaceResolutionError(
        code=WORKSPACE_NOT_REGISTERED,
        message=(
            f"repository root {str(repo_root)!r} is not a member of any registered workspace; "
            f"register one with `eawf workspace add <KEY>`"
        ),
    )


def get_workspace(registry: Registry, key: str) -> WorkspaceRecord:
    """Return the record filed under *key*.

    Raises:
        WorkspaceMutationError: With :data:`WORKSPACE_NOT_REGISTERED`
            when *key* is absent.
    """
    record = registry.workspaces.get(key)
    if record is None:
        raise WorkspaceMutationError(
            code=WORKSPACE_NOT_REGISTERED,
            message=f"workspace {key!r} is not registered",
        )
    return record


def list_workspaces(registry: Registry) -> list[WorkspaceRecord]:
    """Return every registered workspace, ordered by key."""
    return [registry.workspaces[key] for key in sorted(registry.workspaces)]


def _with_workspaces(registry: Registry, workspaces: dict[str, WorkspaceRecord]) -> Registry:
    """Return a copy of *registry* carrying *workspaces* and a fresh stamp."""
    return Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=registry.active_code,
        repos=dict(registry.repos),
        workspaces=workspaces,
    )


def create_workspace(registry: Registry, *, record: WorkspaceRecord) -> Registry:
    """Return *registry* with *record* added.

    Args:
        registry: Already-validated registry document.
        record: The already-validated workspace to file under its key.

    Returns:
        A new :class:`Registry`; the input is left untouched.

    Raises:
        WorkspaceMutationError: With
            :data:`WORKSPACE_ALREADY_REGISTERED` when the key is taken.
            Overwriting is refused rather than merged so a re-run with
            a different member set cannot silently drop members.
    """
    if record.key in registry.workspaces:
        raise WorkspaceMutationError(
            code=WORKSPACE_ALREADY_REGISTERED,
            message=(
                f"workspace {record.key!r} is already registered; "
                f"edit it with `eawf workspace member add/remove`"
            ),
        )
    workspaces = dict(registry.workspaces)
    workspaces[record.key] = record
    return _with_workspaces(registry, workspaces)


def update_membership(
    registry: Registry,
    *,
    key: str,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    expected_revision: int | None = None,
) -> Registry:
    """Return *registry* with the membership of *key* edited.

    Args:
        registry: Already-validated registry document.
        key: Workspace to edit.
        add: Project codes to include.
        remove: Project codes to drop. Removing the home repo, or the
            last member, fails validation on the rebuilt record.
        expected_revision: When supplied, the revision the caller read.
            A mismatch refuses the write instead of clobbering the
            edit that moved the record on.

    Returns:
        A new :class:`Registry` whose record carries ``revision + 1``.

    Raises:
        WorkspaceMutationError: :data:`WORKSPACE_NOT_REGISTERED` when
            *key* is absent; :data:`WORKSPACE_REVISION_CONFLICT` when
            the compare-and-set fails.
        pydantic.ValidationError: When the resulting membership is
            empty or no longer contains the home repo.
    """
    current = get_workspace(registry, key)
    if expected_revision is not None and expected_revision != current.revision:
        raise WorkspaceMutationError(
            code=WORKSPACE_REVISION_CONFLICT,
            message=(
                f"workspace {key!r} is at revision {current.revision}, "
                f"caller expected {expected_revision}; re-read and retry"
            ),
        )
    members = (current.member_project_codes | frozenset(add)) - frozenset(remove)
    updated = WorkspaceRecord(
        key=current.key,
        title=current.title,
        member_project_codes=members,
        home_project_code=current.home_project_code,
        revision=current.revision + 1,
        updated_at=datetime.now(UTC),
    )
    workspaces = dict(registry.workspaces)
    workspaces[key] = updated
    return _with_workspaces(registry, workspaces)


__all__ = [
    "WORKSPACE_ALREADY_REGISTERED",
    "WORKSPACE_AMBIGUOUS",
    "WORKSPACE_NOT_REGISTERED",
    "WORKSPACE_REVISION_CONFLICT",
    "WorkspaceMutationError",
    "WorkspaceResolution",
    "WorkspaceResolutionError",
    "WorkspaceSource",
    "create_workspace",
    "get_workspace",
    "list_workspaces",
    "project_codes_at_root",
    "resolve_workspace",
    "update_membership",
]
