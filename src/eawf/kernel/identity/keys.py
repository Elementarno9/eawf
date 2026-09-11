"""Public key grammar and allocators for epoch-2 entities.

An epoch-2 record carries three identities: an immutable UUID that
nothing renders, a public key that every frame, fixture, deep link and
CLI argument spells, and a qualified URN that locates it. This module
owns the middle one.

The grammar here is authoritative over any rendered frame that spells a
key differently. Design frames circulated ``CMP-0001`` for a campaign,
``TSK-0042`` for a task, ``RUN-0001`` for a run, ``EVT-####`` for a
receipt and ``TOOL-0401`` for a provider permission; none of those is a
key this module mints or admits. No alias table, prefix map, or display
translation is offered for them either -- a second spelling of one key is
exactly the adapter shim the naming rule forbids, and it would give the
legacy alias index a second meaning outside migration. A frame carrying
one is an uncorrected design input, and
:func:`validate_entity_key` rejects it before it can reach a fixture or a
golden file.

Two key families are not ordinal-allocated and are supplied instead of
minted: a track key is an operator-chosen symbol (``TRK-RUNTIME``) and a
release key is derived from a normalised version (``REL-0.7.0.dev1``).
:class:`EntityKeyAllocator` refuses those kinds rather than inventing an
ordinal for them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Final

from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.state.canonical_sequence import AllocationTransaction, MonotonicAllocator
from eawf.kernel.state.ids import RE_PROJECT_CODE


class EntityScope(StrEnum):
    """The level of the containment tree an entity kind is addressed at."""

    WORKSPACE = "workspace"
    PROJECT = "project"
    REPOSITORY = "repository"


class EntityKind(StrEnum):
    """Epoch-2 entity kinds, spelled as the URN kind token.

    A multi-word kind hyphenates in the URN (``campaign-finding``), which
    is why the member values are not the Python-style underscored names.
    """

    WORKSPACE = "workspace"
    PROJECT = "project"
    REPOSITORY = "repository"
    TRACK = "track"
    CAMPAIGN = "campaign"
    MILESTONE = "milestone"
    BATCH = "batch"
    TASK = "task"
    RUN = "run"
    RELEASE = "release"
    EVIDENCE = "evidence"
    CAMPAIGN_FINDING = "campaign-finding"
    CLAIM = "claim"
    QUESTION = "question"
    RECEIPT = "receipt"
    PENDING_ACTION = "pending-action"
    PERMISSION = "permission"
    LEGACY_RECORD = "legacy-record"


#: Which slot of the URN addresses each kind. Workspace- and
#: project-level kinds take the reserved ``_`` repository slot;
#: repository-level kinds take a real repository key. The partition is
#: read off the containment tree: a release is a workspace aggregate
#: orthogonal to any track, a claim and an open question are planning
#: records of the project, and everything a track contains -- campaigns,
#: milestones, batches, tasks, runs and the receipts and evidence they
#: produce -- is addressed under the repository that integrates it.
ENTITY_SCOPES: Final[Mapping[EntityKind, EntityScope]] = {
    EntityKind.WORKSPACE: EntityScope.WORKSPACE,
    EntityKind.RELEASE: EntityScope.WORKSPACE,
    EntityKind.PERMISSION: EntityScope.WORKSPACE,
    EntityKind.PROJECT: EntityScope.PROJECT,
    EntityKind.CLAIM: EntityScope.PROJECT,
    EntityKind.QUESTION: EntityScope.PROJECT,
    EntityKind.REPOSITORY: EntityScope.REPOSITORY,
    EntityKind.TRACK: EntityScope.REPOSITORY,
    EntityKind.CAMPAIGN: EntityScope.REPOSITORY,
    EntityKind.MILESTONE: EntityScope.REPOSITORY,
    EntityKind.BATCH: EntityScope.REPOSITORY,
    EntityKind.TASK: EntityScope.REPOSITORY,
    EntityKind.RUN: EntityScope.REPOSITORY,
    EntityKind.EVIDENCE: EntityScope.REPOSITORY,
    EntityKind.CAMPAIGN_FINDING: EntityScope.REPOSITORY,
    EntityKind.RECEIPT: EntityScope.REPOSITORY,
    EntityKind.PENDING_ACTION: EntityScope.REPOSITORY,
    EntityKind.LEGACY_RECORD: EntityScope.REPOSITORY,
}

#: Ordinal-allocated kinds: their key prefix and zero-padded width. A
#: run gets eight digits because a workspace mints them by the thousand;
#: every other family gets four. The width is part of the grammar, not a
#: rendering choice, which is why ``RUN-0001`` is refused.
KEY_ORDINAL_FAMILIES: Final[Mapping[EntityKind, tuple[str, int]]] = {
    EntityKind.CAMPAIGN: ("CAM", 4),
    EntityKind.MILESTONE: ("MLS", 4),
    EntityKind.BATCH: ("BAT", 4),
    EntityKind.RUN: ("RUN", 8),
    EntityKind.EVIDENCE: ("EVD", 4),
    EntityKind.CAMPAIGN_FINDING: ("CFN", 4),
    EntityKind.CLAIM: ("CLM", 4),
    EntityKind.QUESTION: ("QST", 4),
    EntityKind.RECEIPT: ("RCP", 4),
    EntityKind.PENDING_ACTION: ("ACT", 4),
    EntityKind.PERMISSION: ("PERM", 4),
    EntityKind.LEGACY_RECORD: ("LGR", 4),
}

#: Task keys are the one ordinal family whose prefix is not a fixed
#: literal: a task is spelled ``<PROJECT>-####`` so an operator reading
#: ``EAWF-0042`` knows which project it belongs to without a lookup.
TASK_ORDINAL_WIDTH: Final = 4

#: A track key is an operator-chosen symbol under a fixed prefix, unique
#: inside its project and never reused.
RE_TRACK_KEY: Final = re.compile(r"^TRK-[A-Z0-9][A-Z0-9-]{1,31}$")

#: A release key carries the normalised version it publishes, which is
#: the same normalised-version grammar the release spec pins.
RE_RELEASE_KEY: Final = re.compile(r"^REL-\d+\.\d+\.\d+(?:rc\d+|\.dev\d+)?$")

#: Workspace, project and repository keys share the bounded uppercase
#: symbol grammar and are normalised at creation.
RE_SYMBOL_KEY: Final = RE_PROJECT_CODE

#: The reserved repository slot of a workspace- or project-level URN.
RESERVED_REPOSITORY_SLOT: Final = "_"

_PROJECT_KEY_PREFIX: Final = "PRJ-"


def project_code_of(project_key: str) -> str:
    """Return the project code carried by *project_key*.

    A project is addressed as ``PRJ-EAWF`` while the tasks it owns are
    keyed ``EAWF-0042``: the task family spells the bare project code, so
    validating a task key found in a URN needs the prefix stripped. A key
    without the prefix is returned unchanged, because the prefix is a
    registry convention rather than part of the symbol grammar.

    Args:
        project_key: The project slot of a qualified URN.

    Returns:
        The project code a task key of that project must carry.
    """
    if project_key.startswith(_PROJECT_KEY_PREFIX):
        return project_key[len(_PROJECT_KEY_PREFIX) :]
    return project_key


def validate_symbol_key(key: str, *, slot: str) -> str:
    """Return *key* when it matches the bounded uppercase symbol grammar.

    Args:
        key: The workspace, project, or repository key to check.
        slot: Which slot is being validated, for the rejection message.

    Returns:
        *key* unchanged.

    Raises:
        IdentityError: ``symbol_key_invalid`` when *key* is not a bounded
            uppercase symbol.
    """
    if not RE_SYMBOL_KEY.fullmatch(key):
        raise IdentityError(
            IdentityRejection.SYMBOL_KEY_INVALID,
            f"{slot} key {key!r} does not match {RE_SYMBOL_KEY.pattern}",
        )
    return key


def _ordinal_pattern(prefix: str, width: int) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-\d{{{width}}}$")


def format_entity_key(
    kind: EntityKind,
    ordinal: int,
    *,
    project_code: str | None = None,
) -> str:
    """Render *ordinal* as the public key of *kind*.

    Args:
        kind: The entity family the key belongs to.
        ordinal: The allocated ordinal, ``1`` or greater.
        project_code: Required for :attr:`EntityKind.TASK`, whose keys
            are prefixed with the owning project's code; ignored
            otherwise.

    Returns:
        The zero-padded public key.

    Raises:
        IdentityError: ``kind_not_allocatable`` when *kind* has no
            ordinal family, ``key_space_saturated`` when *ordinal* needs
            more digits than the family's width, or
            ``entity_key_invalid`` when a task key is requested without a
            project code.
        ValueError: *ordinal* is not positive.
    """
    if ordinal < 1:
        raise ValueError(f"ordinal must be positive, got {ordinal}")
    prefix, width = _ordinal_family(kind, project_code=project_code)
    if ordinal >= 10**width:
        raise IdentityError(
            IdentityRejection.KEY_SPACE_SATURATED,
            f"{kind.value} ordinal {ordinal} exceeds the {width}-digit key space",
        )
    return f"{prefix}-{ordinal:0{width}d}"


def _ordinal_family(kind: EntityKind, *, project_code: str | None) -> tuple[str, int]:
    """Return the key prefix and digit width of an ordinal-allocated *kind*."""
    if kind is EntityKind.TASK:
        if project_code is None:
            raise IdentityError(
                IdentityRejection.ENTITY_KEY_INVALID,
                "task keys need the owning project code",
            )
        validate_symbol_key(project_code, slot="project code")
        return project_code, TASK_ORDINAL_WIDTH
    family = KEY_ORDINAL_FAMILIES.get(kind)
    if family is None:
        raise IdentityError(
            IdentityRejection.KIND_NOT_ALLOCATABLE,
            f"{kind.value} keys are supplied, not ordinal-allocated",
        )
    return family


#: Kinds whose public key IS the slot key that addresses them: a
#: workspace, project or repository is named by its own symbol rather
#: than by a minted ordinal.
_CONTAINER_KINDS: Final = frozenset(
    {EntityKind.WORKSPACE, EntityKind.PROJECT, EntityKind.REPOSITORY}
)


def validate_entity_key(
    kind: EntityKind,
    key: str,
    *,
    project_code: str | None = None,
) -> str:
    """Return *key* when it is a canonical public key of *kind*.

    Args:
        kind: The entity family the key claims to belong to.
        key: The public key to check.
        project_code: Required for :attr:`EntityKind.TASK`; the code the
            key must be prefixed with.

    Returns:
        *key* unchanged.

    Raises:
        IdentityError: ``entity_key_invalid`` when *key* does not match
            the family's grammar, or ``symbol_key_invalid`` when a
            container kind's key is not a bounded uppercase symbol.
    """
    if kind in _CONTAINER_KINDS:
        return validate_symbol_key(key, slot=kind.value)
    if kind is EntityKind.TRACK:
        return _match_or_reject(kind, key, RE_TRACK_KEY)
    if kind is EntityKind.RELEASE:
        return _match_or_reject(kind, key, RE_RELEASE_KEY)
    prefix, width = _ordinal_family(kind, project_code=project_code)
    return _match_or_reject(kind, key, _ordinal_pattern(prefix, width))


def _match_or_reject(kind: EntityKind, key: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(key):
        raise IdentityError(
            IdentityRejection.ENTITY_KEY_INVALID,
            f"{kind.value} key {key!r} does not match {pattern.pattern}",
        )
    return key


class EntityKeyAllocator:
    """Mints the public keys of one entity kind for one workspace.

    Allocation is monotonic per workspace and happens inside a
    transaction: a key drawn by a transaction that raised is burned, so a
    compaction or a rolled-back import never lets a second record claim a
    key an earlier one already rendered.
    """

    def __init__(
        self,
        *,
        workspace_key: str,
        kind: EntityKind,
        project_code: str | None = None,
        allocated: int = 0,
    ) -> None:
        """Open the key counter of *kind* within *workspace_key*.

        Args:
            workspace_key: The workspace whose key space this counter owns.
            kind: The entity family to mint keys for.
            project_code: Required for :attr:`EntityKind.TASK`.
            allocated: How many keys the workspace already committed.

        Raises:
            IdentityError: ``kind_not_allocatable`` when *kind* has no
                ordinal family, or ``entity_key_invalid`` when a task
                allocator is opened without a project code.
            ValueError: *workspace_key* is empty or *allocated* is
                negative.
        """
        if not workspace_key:
            raise ValueError("workspace_key must be non-empty")
        prefix, width = _ordinal_family(kind, project_code=project_code)
        self._workspace_key = workspace_key
        self._kind = kind
        self._project_code = project_code
        self._counter = MonotonicAllocator(
            name=f"{kind.value}[{workspace_key}]",
            committed=allocated,
            limit=10**width - 1,
        )
        self._prefix = prefix
        self._width = width

    @property
    def kind(self) -> EntityKind:
        """The entity family this allocator mints."""
        return self._kind

    @property
    def workspace_key(self) -> str:
        """The workspace whose key space this allocator owns."""
        return self._workspace_key

    @property
    def allocated(self) -> int:
        """How many keys a completed transaction has committed."""
        return self._counter.committed

    @contextmanager
    def transaction(self) -> Iterator[EntityKeyTransaction]:
        """Open a transaction whose keys commit on clean exit.

        Yields:
            The :class:`EntityKeyTransaction` to draw keys from.
        """
        with self._counter.transaction() as ordinals:
            yield EntityKeyTransaction(
                ordinals=ordinals,
                kind=self._kind,
                project_code=self._project_code,
            )


class EntityKeyTransaction:
    """The keys one transaction has minted, before it commits."""

    def __init__(
        self,
        *,
        ordinals: AllocationTransaction,
        kind: EntityKind,
        project_code: str | None,
    ) -> None:
        """Wrap the ordinal transaction *ordinals* as a key transaction.

        Args:
            ordinals: The underlying monotonic ordinal transaction.
            kind: The entity family being minted.
            project_code: The project code task keys carry, if any.
        """
        self._ordinals = ordinals
        self._kind = kind
        self._project_code = project_code

    def allocate(self) -> str:
        """Mint the next public key.

        Returns:
            A canonical key of the allocator's kind.

        Raises:
            CanonicalSequenceError: The key space is exhausted, which
                aborts the whole transaction.
        """
        return format_entity_key(
            self._kind,
            self._ordinals.allocate(),
            project_code=self._project_code,
        )
