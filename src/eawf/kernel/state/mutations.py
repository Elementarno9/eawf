"""Mutation discriminated union — the typed payload for ``state.mutate``.

The daemon's :func:`state.mutate` RPC accepts exactly one :class:`Mutation`
per call; the discriminator :attr:`MutationKind` names which lifecycle
transition (or kindred state edit) the daemon should apply. Each kind
maps onto exactly one apply function inside
:mod:`eawf.runtime.daemon.methods.state` so the dispatch is closed + auditable.

Every kind in :class:`MutationKind` resolves to a real apply function in
:mod:`eawf.runtime.daemon.methods.state` — the wave / phase / iter kinds delegate
to :mod:`eawf.workflow.lifecycle.transitions`; the roadmap kinds map onto the
planner transitions (``plan_wave`` / ``remove_wave_plan`` /
``set_wave_deps`` / ``edit_wave_plan`` / ``archive_phase``); and
``EVENT_APPEND`` is an append-only audit row with no structural state
change.

The five ``MEMORY_*`` kinds (``MEMORY_ADD`` / ``MEMORY_UPDATE`` /
``MEMORY_SUPERSEDE`` / ``MEMORY_PRUNE`` / ``MEMORY_REVIEW``) ship their
apply functions in this module (``apply_memory_*``) — they are pure
state mutators that only touch :attr:`State.memory_index`, so they have
no JSONL-side-effects to push into the daemon module. The daemon's
``_APPLY_REGISTRY`` re-exports them in a later wave; the cache-only
shape keeps replay round-trips (state → mutate → re-validate) entirely
in-process. Memory **recall** is intentionally absent — recall is a
read-only operation and never mutates :class:`State`.

Per the spike brief (`.ea/local/research/2026-05-19-p24-c02-impl-waves.md`
§4 "W09") this module deliberately uses a **loose discriminated union**:
the per-variant :attr:`MutationBase.params` field carries the kind-
specific dict. Per-variant Pydantic subclasses (one per
:class:`MutationKind`) land in C03-IMPL when the spec catalogue is
fully enumerated; until then the dict shape is contract-tested in the
daemon apply functions, not by the discriminator. The five ``MEMORY_*``
kinds added in P28-I02-W04 do ship typed payload models (e.g.
:class:`MemoryAddPayload`) alongside their apply functions so the
in-process replay tests can validate the param dict before apply.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import (
    Confidence,
    DecisionStatus,
    MemoryStatus,
    MemoryTier,
)
from eawf.kernel.state.models import MemorySummary, State

logger = logging.getLogger(__name__)


class MutationKind(StrEnum):
    """Closed enumeration of state-mutation kinds.

    Each variant names exactly one CLI verb that mutates ``state.json``;
    the daemon's :func:`state.mutate` apply table maps each kind onto
    the corresponding :mod:`eawf.workflow.lifecycle.transitions` function. Every
    kind now resolves to a real apply function — the roadmap kinds
    (:attr:`ROADMAP_REVISE`, :attr:`ROADMAP_APPLY`, :attr:`ROADMAP_DROP`)
    dispatch to the planner transitions, :attr:`WAVE_RELEASE` un-claims a
    wave back to PENDING, and :attr:`EVENT_APPEND` records an append-only
    audit row with no structural state change.

    The five ``MEMORY_*`` kinds map onto :func:`apply_memory_add` /
    :func:`apply_memory_update` / :func:`apply_memory_supersede` /
    :func:`apply_memory_prune` / :func:`apply_memory_review` in this
    module. They mutate :attr:`State.memory_index` only — the JSONL
    side-effect (append to ``memory.jsonl``) stays in the calling
    surface (:mod:`eawf.platform.memory.store` etc.). ``MEMORY_RECALL``
    is intentionally absent: recall is read-only.
    """

    WAVE_CLAIM = "wave_claim"
    WAVE_CLOSE = "wave_close"
    WAVE_FAIL = "wave_fail"
    WAVE_RELEASE = "wave_release"
    PHASE_OPEN = "phase_open"
    PHASE_ACTIVATE = "phase_activate"
    PHASE_CLOSE = "phase_close"
    ITER_OPEN = "iter_open"
    ITER_CLOSE = "iter_close"
    EVENT_APPEND = "event_append"
    ROADMAP_REVISE = "roadmap_revise"
    ROADMAP_APPLY = "roadmap_apply"
    ROADMAP_DROP = "roadmap_drop"
    MEMORY_ADD = "memory_add"
    MEMORY_UPDATE = "memory_update"
    MEMORY_SUPERSEDE = "memory_supersede"
    MEMORY_PRUNE = "memory_prune"
    MEMORY_REVIEW = "memory_review"
    DECISION_OBSOLETE = "decision_obsolete"


class Mutation(BaseModel):
    """One state-mutation payload sent across the daemon RPC boundary.

    Attributes:
        kind: :class:`MutationKind` discriminator; the daemon dispatches
            to a per-kind apply function.
        scope_id: Canonical scope id (wave id, phase id, iter id, etc.)
            this mutation targets. Carried verbatim into the event
            envelope so subscribers can filter by scope without
            decoding ``params``.
        mutation_id: Stable client-side identifier (typically a fresh
            uuid4 hex). Used by the daemon to correlate the in-flight
            mutation across logs + WAL records.
        idempotency_key: Optional caller-supplied key for the V5
            cross-runtime retry path; a repeat call with the same
            key inside the daemon's idempotency window returns the
            cached envelope with ``idempotent_replay=True``.
        params: Kind-specific parameter dict. Loose-typed in W09; the
            daemon apply functions are the contract. Per-variant
            Pydantic subclasses land in C03-IMPL.
    """

    model_config = ConfigDict(extra="forbid")

    kind: MutationKind
    scope_id: str = Field(min_length=1)
    mutation_id: str = Field(min_length=1)
    idempotency_key: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# ---- MEMORY_* error type ----------------------------------------------------


class MemoryMutationError(ValueError):
    """Raised when a ``MEMORY_*`` apply rejects the mutation.

    Mirrors the :class:`~eawf.platform.memory.gc.GcError` /
    :class:`~eawf.platform.memory.prune.PruneError` pattern so the
    daemon's existing ``LifecycleError`` → ``-32602 invalid_params``
    mapping carries memory rejections with the same wire shape.
    """


# ---- MEMORY_* typed payload models -----------------------------------------


class _StrictPayload(BaseModel):
    """Base for memory-mutation payload models — strict + extra="forbid"."""

    model_config = ConfigDict(extra="forbid")


class MemoryAddPayload(_StrictPayload):
    """Params for :attr:`MutationKind.MEMORY_ADD`.

    Attributes:
        id: Stable memory id (``MEM-<UTC-date>-<NN>``); the caller is
            responsible for allocating a fresh id (see
            :func:`eawf.platform.memory.store._next_memory_id`).
        scope_id: Scope the memory entry belongs to.
        summary: Short summary text — what
            :func:`eawf.platform.memory.store._summary_text` composed when
            the caller wrote the JSONL envelope.
        confidence: :class:`Confidence` bucket.
        store_record_id: JSONL record id (typically ``id`` itself).
        status: Initial :class:`MemoryStatus`; defaults to ``ACTIVE``.
        review_due: Optional review-due timestamp.
        tier: Initial :class:`MemoryTier`; defaults to ``WORKING``.
        promoted_to_artifact_id: Optional artifact link.
    """

    id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    confidence: Confidence
    store_record_id: str = Field(min_length=1)
    status: MemoryStatus = MemoryStatus.ACTIVE
    review_due: datetime | None = None
    tier: MemoryTier = MemoryTier.WORKING
    promoted_to_artifact_id: str | None = None


class MemoryUpdatePayload(_StrictPayload):
    """Params for :attr:`MutationKind.MEMORY_UPDATE`.

    Every field other than :attr:`id` is optional; only supplied fields
    are written through to the existing summary. Unsupplied fields are
    left unchanged.

    Attributes:
        id: Memory id to update.
        summary: New summary text (optional).
        confidence: New confidence bucket (optional).
        status: New status (optional).
        review_due: New review-due timestamp; pass an explicit ``None``
            wrapped in a sentinel via ``review_due_clear=True`` to clear
            it (the bare ``None`` here means "don't touch").
        review_due_clear: Set ``True`` to explicitly clear ``review_due``.
        tier: New tier (optional).
        promoted_to_artifact_id: New artifact link (optional).
    """

    id: str = Field(min_length=1)
    summary: str | None = None
    confidence: Confidence | None = None
    status: MemoryStatus | None = None
    review_due: datetime | None = None
    review_due_clear: bool = False
    tier: MemoryTier | None = None
    promoted_to_artifact_id: str | None = None


class MemorySupersedePayload(_StrictPayload):
    """Params for :attr:`MutationKind.MEMORY_SUPERSEDE`.

    Marks an existing entry ``superseded`` and inserts a new entry
    pointing back via :attr:`promoted_to_artifact_id` (re-purposed as the
    supersede link until a dedicated ``superseded_by`` field lands on
    :class:`MemorySummary`).

    Attributes:
        old_id: Existing memory id being superseded.
        new_entry: Full payload for the replacement entry.
    """

    old_id: str = Field(min_length=1)
    new_entry: MemoryAddPayload


class MemoryPrunePayload(_StrictPayload):
    """Params for :attr:`MutationKind.MEMORY_PRUNE`.

    Soft-prune the named entry: flip status to ``PRUNED``. The JSONL
    side-effect (writing an ``expired_at`` envelope) stays in
    :func:`eawf.platform.memory.prune.prune_memory`.

    Attributes:
        id: Memory id to prune.
    """

    id: str = Field(min_length=1)


class MemoryReviewPayload(_StrictPayload):
    """Params for :attr:`MutationKind.MEMORY_REVIEW`.

    Stamp the entry as ``reviewed-now`` by advancing ``review_due``
    forward. The model has no dedicated ``last_reviewed_at`` field;
    ``review_due`` carries both "when to next review" and the implicit
    "last touched" semantics for this surface.

    Attributes:
        id: Memory id to review.
        review_due: New review-due timestamp (``now + cadence`` at the
            caller's discretion).
    """

    id: str = Field(min_length=1)
    review_due: datetime


# ---- MEMORY_* apply functions ----------------------------------------------


def _memory_index(state: State) -> dict[str, MemorySummary]:
    """Return ``state.memory_index`` as a non-``None`` dict in place.

    Creates a fresh empty dict on the state when the field is currently
    ``None`` so callers can index into it without re-checking.
    """
    if state.memory_index is None:
        state.memory_index = {}
    return state.memory_index


def apply_memory_add(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.MEMORY_ADD` — insert a new summary.

    Args:
        state: Loaded :class:`State`. Mutated in place.
        mutation: :class:`Mutation` carrying a :class:`MemoryAddPayload`
            in ``params``.

    Raises:
        MemoryMutationError: when an entry with the supplied ``id``
            already exists.
    """
    payload = MemoryAddPayload.model_validate(mutation.params)
    index = _memory_index(state)
    if payload.id in index:
        raise MemoryMutationError(f"memory entry already exists: {payload.id!r}")
    index[payload.id] = MemorySummary(
        id=payload.id,
        scope_id=payload.scope_id,
        summary=payload.summary,
        confidence=payload.confidence,
        status=payload.status,
        store_record_id=payload.store_record_id,
        review_due=payload.review_due,
        tier=payload.tier,
        promoted_to_artifact_id=payload.promoted_to_artifact_id,
    )
    logger.info(f"apply_memory_add id={payload.id!r} scope={payload.scope_id!r}")


def apply_memory_update(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.MEMORY_UPDATE` — patch an existing summary.

    Only fields supplied on the payload are updated; everything else
    stays as is. The ``review_due_clear=True`` flag is the explicit
    "set to ``None``" signal, since a bare ``None`` on the payload
    means "don't touch".

    Args:
        state: Loaded :class:`State`. Mutated in place.
        mutation: :class:`Mutation` carrying a :class:`MemoryUpdatePayload`
            in ``params``.

    Raises:
        MemoryMutationError: when no entry matches ``id``.
    """
    payload = MemoryUpdatePayload.model_validate(mutation.params)
    index = _memory_index(state)
    existing = index.get(payload.id)
    if existing is None:
        raise MemoryMutationError(f"unknown memory entry: {payload.id!r}")
    updates: dict[str, Any] = {}
    if payload.summary is not None:
        updates["summary"] = payload.summary
    if payload.confidence is not None:
        updates["confidence"] = payload.confidence
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.review_due_clear:
        updates["review_due"] = None
    elif payload.review_due is not None:
        updates["review_due"] = payload.review_due
    if payload.tier is not None:
        updates["tier"] = payload.tier
    if payload.promoted_to_artifact_id is not None:
        updates["promoted_to_artifact_id"] = payload.promoted_to_artifact_id
    if updates:
        index[payload.id] = existing.model_copy(update=updates)
    logger.info(f"apply_memory_update id={payload.id!r} fields={sorted(updates)}")


def apply_memory_supersede(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.MEMORY_SUPERSEDE`.

    Flips the old entry's status to :attr:`MemoryStatus.SUPERSEDED` and
    inserts the replacement entry. The new entry's
    :attr:`MemorySummary.promoted_to_artifact_id` is populated with
    ``old_id`` as the supersede link until a dedicated
    ``supersedes`` field lands on the model.

    Args:
        state: Loaded :class:`State`. Mutated in place.
        mutation: :class:`Mutation` carrying a
            :class:`MemorySupersedePayload` in ``params``.

    Raises:
        MemoryMutationError: when the old entry is missing or the new
            entry's id already exists.
    """
    payload = MemorySupersedePayload.model_validate(mutation.params)
    index = _memory_index(state)
    old = index.get(payload.old_id)
    if old is None:
        raise MemoryMutationError(f"unknown memory entry: {payload.old_id!r}")
    if payload.new_entry.id in index:
        raise MemoryMutationError(
            f"replacement memory entry already exists: {payload.new_entry.id!r}"
        )
    index[payload.old_id] = old.model_copy(update={"status": MemoryStatus.SUPERSEDED})
    supersede_link = (
        payload.new_entry.promoted_to_artifact_id
        if payload.new_entry.promoted_to_artifact_id is not None
        else payload.old_id
    )
    index[payload.new_entry.id] = MemorySummary(
        id=payload.new_entry.id,
        scope_id=payload.new_entry.scope_id,
        summary=payload.new_entry.summary,
        confidence=payload.new_entry.confidence,
        status=payload.new_entry.status,
        store_record_id=payload.new_entry.store_record_id,
        review_due=payload.new_entry.review_due,
        tier=payload.new_entry.tier,
        promoted_to_artifact_id=supersede_link,
    )
    logger.info(f"apply_memory_supersede old_id={payload.old_id!r} new_id={payload.new_entry.id!r}")


def apply_memory_prune(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.MEMORY_PRUNE` — flip status to ``PRUNED``.

    Soft-delete only. The JSONL ``expired_at`` envelope is written by
    :func:`eawf.platform.memory.prune.prune_memory`, not here.

    Args:
        state: Loaded :class:`State`. Mutated in place.
        mutation: :class:`Mutation` carrying a :class:`MemoryPrunePayload`
            in ``params``.

    Raises:
        MemoryMutationError: when no entry matches ``id`` or the entry
            is already ``PRUNED`` (idempotency lives in the calling
            surface, not in apply).
    """
    payload = MemoryPrunePayload.model_validate(mutation.params)
    index = _memory_index(state)
    existing = index.get(payload.id)
    if existing is None:
        raise MemoryMutationError(f"unknown memory entry: {payload.id!r}")
    if existing.status == MemoryStatus.PRUNED:
        raise MemoryMutationError(f"memory entry already pruned: {payload.id!r}")
    index[payload.id] = existing.model_copy(update={"status": MemoryStatus.PRUNED})
    logger.info(f"apply_memory_prune id={payload.id!r}")


def apply_memory_review(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.MEMORY_REVIEW` — stamp a fresh review_due.

    Args:
        state: Loaded :class:`State`. Mutated in place.
        mutation: :class:`Mutation` carrying a :class:`MemoryReviewPayload`
            in ``params``.

    Raises:
        MemoryMutationError: when no entry matches ``id``.
    """
    payload = MemoryReviewPayload.model_validate(mutation.params)
    index = _memory_index(state)
    existing = index.get(payload.id)
    if existing is None:
        raise MemoryMutationError(f"unknown memory entry: {payload.id!r}")
    index[payload.id] = existing.model_copy(update={"review_due": payload.review_due})
    logger.info(
        f"apply_memory_review id={payload.id!r} review_due={payload.review_due.isoformat()}"
    )


# ---- DECISION_OBSOLETE error type + payload + apply ------------------------


class DecisionMutationError(ValueError):
    """Raised when a ``DECISION_*`` apply rejects the mutation.

    Mirrors :class:`MemoryMutationError` so the daemon's existing
    ``LifecycleError`` -> ``-32602 invalid_params`` mapping carries
    decision rejections with the same wire shape.
    """


class DecisionObsoletePayload(_StrictPayload):
    """Params for :attr:`MutationKind.DECISION_OBSOLETE`.

    Marks an existing decision as no-longer-relevant: flips ``status`` to
    :attr:`DecisionStatus.OBSOLETE` and stamps ``obsoleted_at`` with the
    caller-supplied UTC timestamp (typically ``datetime.now(UTC)``).

    Attributes:
        id: Existing decision id to obsolete.
        obsoleted_at: UTC timestamp to record on the row; the caller
            supplies the value so the apply stays pure (no
            wall-clock read inside the mutator).
    """

    id: str = Field(min_length=1)
    obsoleted_at: datetime


def apply_decision_obsolete(state: State, mutation: Mutation) -> None:
    """Apply :attr:`MutationKind.DECISION_OBSOLETE`.

    Flips the named decision's status to :attr:`DecisionStatus.OBSOLETE`
    and stamps ``obsoleted_at``. Only :attr:`DecisionStatus.ACTIVE`
    decisions may be obsoleted: a :attr:`DecisionStatus.SUPERSEDED` row
    carries a non-null ``superseded_by`` link that the
    ``INV.DECISION.LINK_WITHOUT_SUPERSEDED`` invariant requires status
    to remain ``SUPERSEDED`` for, and an already-:attr:`OBSOLETE` row
    has nothing to do (the repeat-call rejection makes the no-op
    visible to the caller).

    Args:
        state: Loaded :class:`State`. Mutated in place.
        mutation: :class:`Mutation` carrying a :class:`DecisionObsoletePayload`
            in ``params``.

    Raises:
        DecisionMutationError: when no decision matches ``id`` or the
            decision is not :attr:`DecisionStatus.ACTIVE`.
    """
    payload = DecisionObsoletePayload.model_validate(mutation.params)
    decisions = state.decisions or {}
    existing = decisions.get(payload.id)
    if existing is None:
        raise DecisionMutationError(f"unknown decision: {payload.id!r}")
    if existing.status == DecisionStatus.OBSOLETE:
        raise DecisionMutationError(f"decision already obsolete: {payload.id!r}")
    if existing.status != DecisionStatus.ACTIVE:
        raise DecisionMutationError(
            f"cannot obsolete decision in status {existing.status.value!r}: {payload.id!r}"
        )
    decisions[payload.id] = existing.model_copy(
        update={
            "status": DecisionStatus.OBSOLETE,
            "obsoleted_at": payload.obsoleted_at,
        }
    )
    state.decisions = decisions
    logger.info(
        f"apply_decision_obsolete id={payload.id!r} obsoleted_at={payload.obsoleted_at.isoformat()}"
    )


__all__ = [
    "DecisionMutationError",
    "DecisionObsoletePayload",
    "MemoryAddPayload",
    "MemoryMutationError",
    "MemoryPrunePayload",
    "MemoryReviewPayload",
    "MemorySupersedePayload",
    "MemoryUpdatePayload",
    "Mutation",
    "MutationKind",
    "apply_decision_obsolete",
    "apply_memory_add",
    "apply_memory_prune",
    "apply_memory_review",
    "apply_memory_supersede",
    "apply_memory_update",
]
