"""The integration read models: what the Git surface and the conflict card draw.

Two routes answer what happened when a Batch's sealed candidates were made into one
delivery. ``git.pr`` renders the generations the Batch has taken, newest last, and
``merge.conflict`` renders the frame a blocked attempt left behind.

Three facts of the delivery records shape the models.

The selection is positional. A generation is the head because it is the newest line of
the Batch's ledger, never because a stored flag says so, so the read model recomputes
``selected`` from position and ignores whatever the line carried. Two readers of one
ledger then agree without either of them having to trust a field an old writer set.

The ordinals start above the base. A Batch's base binding is generation one and an
attempt is prepared against a base that must precede its own ordinal, so the first
ordinal an integration can produce is :data:`FIRST_INTEGRATED_GENERATION` and a record
at the base ordinal does not exist. The count of deliveries is therefore the count of
generation records, and the Git surface never has to tell a binding from a delivery.

A conflict frame is read-only and has to name its way out. The card shows one hunk at a
time, because two sides of one region are already four authorities to hold in mind; the
hunks of every uncleared conflict are flattened into one ordered run so the card's
position is ``n of m`` over the whole conflict rather than over whichever file it
happened to start in. The typed exit rides on the frame, because a conflict a surface
cannot leave is the one that invites someone to edit the canonical workspace by hand.

Nothing here reads the workspace, the ledger or a lock. The inputs are one validated
:class:`~eawf.kernel.projection.compute.RouteProjection` and the already-validated
delivery records the caller holds, so a view is a pure function of both.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from eawf.kernel.delivery.integration import (
    ConflictAuthority,
    ConflictExitKind,
    ConflictHunk,
    IntegrationConflict,
    IntegrationGeneration,
)
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    status_and,
    unstated,
)

logger = logging.getLogger(__name__)

#: The family name a refusal from this module names.
FAMILY: Final = "integration"

#: The console routes this module states a read model for. Both are bound by
#: :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`, so both are served by
#: ``projection.<route>.read`` and by ``projection.<route>.reconnect``.
INTEGRATION_ROUTES: Final[tuple[str, ...]] = ("git.pr", "merge.conflict")

#: The route that renders a Batch's generations.
GIT_PR_ROUTE: Final = "git.pr"

#: The route that renders one hunk of a blocked attempt's conflict.
MERGE_CONFLICT_ROUTE: Final = "merge.conflict"

#: The generation ordinal a Batch's base binding holds before anything is integrated.
BASE_GENERATION: Final = 1

#: The lowest ordinal an integration can produce. An attempt is prepared against the
#: selected base and yields that base's generation plus one, and the record refuses a
#: target base that does not precede it, so no generation record exists at the base
#: ordinal and every record the Git surface draws is a delivery.
FIRST_INTEGRATED_GENERATION: Final = BASE_GENERATION + 1

#: The item whose producer would state the pull request a delivery was opened as. The
#: daemon integrates in an isolated workspace and touches no remote, so nothing in this
#: tree observes a review, its approvals or its checks.
PULL_REQUEST_PRODUCER: Final = "DEL-041 pull-request observation"

#: What each integration route renders per row, in column order. The first field of both
#: is the status the Batch document states; the review columns name the producer they
#: wait on, so the frame says which item would fill the cell.
INTEGRATION_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        "git.pr": status_and(
            unstated("review", missing_producer=PULL_REQUEST_PRODUCER),
            unstated("checks", missing_producer=PULL_REQUEST_PRODUCER),
        ),
        "merge.conflict": status_and(unstated("resolution")),
    }
)


check_field_tables(family=FAMILY, routes=INTEGRATION_ROUTES, fields=INTEGRATION_FIELDS)


def authority_label(authority: ConflictAuthority) -> str:
    """Return who wrote one side of a hunk, as the card names them.

    Args:
        authority: The typed reference the side carries. A display name is not an
            authority, so only the Batch an agent worked under and a principal's
            immutable key can be named.

    Returns:
        The agent's Batch key, or the principal's key.
    """
    if authority.kind == "agent":
        return f"the agent of {authority.batch_ref.entity_key}"
    return authority.principal_key


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationRow:
    """One generation a Batch has taken, as the Git surface draws it.

    Attributes:
        key: The generation's ``ING-######`` key.
        ordinal: Its position in the Batch's delivery order, never below
            :data:`FIRST_INTEGRATED_GENERATION`.
        selected: Whether it is the Batch head, recomputed from position.
        parent_key: The generation it descends from, or ``None`` for the first.
        head_sha: The commit the delivery produced.
        tree_sha: The tree that commit carries, which is the content identity.
        candidate_bundle_id: The sealed bundle the delivery was taken from.
        changed_paths: The repo-relative paths the delivery touched, in record order.
        created_at: When the generation was created.
    """

    key: str
    ordinal: int
    selected: bool
    parent_key: str | None
    head_sha: str
    tree_sha: str
    candidate_bundle_id: str
    changed_paths: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ConflictSideView:
    """One side of a hunk: who wrote it, when, at which commit, and what it says."""

    authority: str
    at: datetime
    sha: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class HunkView:
    """One conflicting region, at its position across the whole conflict.

    Attributes:
        conflict_key: The conflict frame the hunk belongs to.
        path: The repo-relative file the region sits in.
        position: The hunk's one-based position across every uncleared conflict, which
            is what the card's ``n of m`` counts.
        file_index: The hunk's one-based position within its own file.
        ours: The side the Batch's agent wrote.
        theirs: The side it conflicts with.
    """

    conflict_key: str
    path: str
    position: int
    file_index: int
    ours: ConflictSideView
    theirs: ConflictSideView


@dataclass(frozen=True, slots=True, kw_only=True)
class ConflictView:
    """One blocked attempt's frame, without its hunks.

    Attributes:
        key: The conflict's ``INC-######`` key.
        attempt_key: The attempt it blocked.
        branch: The integration branch the conflict was seen on.
        ahead: How far the candidate is ahead of the selected Batch base.
        behind: How far it is behind that base.
        paths: The conflicting files, in record order.
        exit_kind: Where resolution of this conflict lands.
        exit_ref: The record that exit addresses.
        cleared: Whether the conflict has been cleared.
    """

    key: str
    attempt_key: str
    branch: str
    ahead: int
    behind: int
    paths: tuple[str, ...]
    exit_kind: ConflictExitKind
    exit_ref: str
    cleared: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class GitPrReadModel(RouteReadModel):
    """The Git surface's read model: the Batch rows, and the generations it has taken."""

    generations: tuple[GenerationRow, ...] = ()

    def selected_generation(self) -> GenerationRow | None:
        """Return the Batch head, or ``None`` when no generation is held."""
        return next((row for row in self.generations if row.selected), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class MergeConflictReadModel(RouteReadModel):
    """The conflict card's read model: the frames held, and their hunks in one run."""

    conflicts: tuple[ConflictView, ...] = ()
    hunks: tuple[HunkView, ...] = ()

    def hunk_at(self, position: int) -> HunkView | None:
        """Return the hunk at a zero-based offset, or ``None`` when it is past the end.

        Args:
            position: The offset the card's cursor sits at.

        Returns:
            The one hunk the card draws, or ``None`` for an offset no hunk holds, which
            includes every offset of a conflict-free Batch.
        """
        if position < 0 or position >= len(self.hunks):
            return None
        return self.hunks[position]

    def conflict_of(self, hunk: HunkView) -> ConflictView | None:
        """Return the frame ``hunk`` belongs to, or ``None`` when none is held."""
        return next((item for item in self.conflicts if item.key == hunk.conflict_key), None)


def build_generation_rows(
    generations: Sequence[IntegrationGeneration],
) -> tuple[GenerationRow, ...]:
    """Return one row per generation, with the head recomputed from position.

    The stored ``selected`` flag is deliberately not read. Every generation was the head
    when it was written, so a reader that trusted the flag would draw a Batch with as
    many heads as it has deliveries; exactly the newest line is the head now.

    Args:
        generations: The Batch's generations, oldest first.

    Returns:
        One row per generation in input order, the last of them selected.
    """
    last = len(generations) - 1
    rows = tuple(
        GenerationRow(
            key=generation.id,
            ordinal=generation.generation,
            selected=index == last,
            parent_key=generation.parent_generation_id,
            head_sha=generation.integrated_revision.head_sha,
            tree_sha=generation.integrated_revision.tree_sha,
            candidate_bundle_id=generation.source_candidate_bundle_id,
            changed_paths=generation.changed_paths,
            created_at=generation.created_at,
        )
        for index, generation in enumerate(generations)
    )
    logger.debug(f"build_generation_rows generations={len(rows)}")
    return rows


def _side(hunk: ConflictHunk, *, ours: bool) -> ConflictSideView:
    """Return one side of ``hunk`` as the card draws it."""
    side = hunk.ours if ours else hunk.theirs
    return ConflictSideView(
        authority=authority_label(side.authority),
        at=side.at,
        sha=side.sha,
        lines=side.lines,
    )


def build_conflict_views(
    conflicts: Sequence[IntegrationConflict],
) -> tuple[tuple[ConflictView, ...], tuple[HunkView, ...]]:
    """Return the conflict frames held and their hunks, flattened into one ordered run.

    A cleared conflict keeps its frame and contributes no hunk: the record says the
    blockage is over, and drawing its regions would put an operator back on a decision
    somebody already made.

    Args:
        conflicts: The conflict records the caller holds, in record order.

    Returns:
        The frames, and every hunk of every uncleared frame numbered from one across the
        whole run so the card's position is ``n of m`` over the conflict rather than over
        one file of it.
    """
    frames: list[ConflictView] = []
    hunks: list[HunkView] = []
    for conflict in conflicts:
        frames.append(
            ConflictView(
                key=conflict.id,
                attempt_key=conflict.attempt_id,
                branch=conflict.branch,
                ahead=conflict.ahead,
                behind=conflict.behind,
                paths=tuple(item.path for item in conflict.files),
                exit_kind=conflict.exit.kind,
                exit_ref=str(conflict.exit.ref),
                cleared=conflict.cleared_at is not None,
            )
        )
        if conflict.cleared_at is not None:
            continue
        for item in conflict.files:
            for hunk in item.hunks:
                hunks.append(
                    HunkView(
                        conflict_key=conflict.id,
                        path=item.path,
                        position=len(hunks) + 1,
                        file_index=hunk.index,
                        ours=_side(hunk, ours=True),
                        theirs=_side(hunk, ours=False),
                    )
                )
    logger.debug(f"build_conflict_views frames={len(frames)} hunks={len(hunks)}")
    return tuple(frames), tuple(hunks)


def build_integration_view(
    projection: RouteProjection,
    *,
    generations: Sequence[IntegrationGeneration] = (),
    conflicts: Sequence[IntegrationConflict] = (),
) -> RouteReadModel:
    """Return the read model one integration route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        generations: The Batch's generations, oldest first; drawn by the Git surface.
        conflicts: The conflict frames held; drawn by the conflict card.

    Returns:
        A :class:`GitPrReadModel` for the Git surface and a
        :class:`MergeConflictReadModel` for the conflict card, each with an empty
        record run when the caller holds none.

    Raises:
        ValueError: The projection is for a route this module states no read model for.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=INTEGRATION_FIELDS)
    if projection.route == GIT_PR_ROUTE:
        return GitPrReadModel(
            route=model.route,
            read_model=model.read_model,
            scope_id=model.scope_id,
            source_cursor=model.source_cursor,
            digest=model.digest,
            complete=model.complete,
            rows=model.rows,
            counts=model.counts,
            specs=model.specs,
            generations=build_generation_rows(generations),
        )
    frames, hunks = build_conflict_views(conflicts)
    return MergeConflictReadModel(
        route=model.route,
        read_model=model.read_model,
        scope_id=model.scope_id,
        source_cursor=model.source_cursor,
        digest=model.digest,
        complete=model.complete,
        rows=model.rows,
        counts=model.counts,
        specs=model.specs,
        conflicts=frames,
        hunks=hunks,
    )


__all__ = [
    "BASE_GENERATION",
    "FAMILY",
    "FIRST_INTEGRATED_GENERATION",
    "GIT_PR_ROUTE",
    "INTEGRATION_FIELDS",
    "INTEGRATION_ROUTES",
    "MERGE_CONFLICT_ROUTE",
    "PULL_REQUEST_PRODUCER",
    "ConflictSideView",
    "ConflictView",
    "GenerationRow",
    "GitPrReadModel",
    "HunkView",
    "MergeConflictReadModel",
    "authority_label",
    "build_conflict_views",
    "build_generation_rows",
    "build_integration_view",
]
