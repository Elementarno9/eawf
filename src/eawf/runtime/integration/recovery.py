"""Where an integration does its slow work, and where it takes the lock.

An integration materializes a workspace, applies a sequence of candidates
into it and authors a commit. All three are slow and all three can fail
halfway, so none of them may run while the canonical state lock is held:
a materialization that stalls would stall every other writer of the tree
behind it, and a crash midway would leave the lock's owner gone with the
lock still taken. The phases are therefore declared, and the declaration
is compiled at import, so a phase nobody assigned a lock discipline to is
a startup failure rather than a phase that quietly runs wherever the code
happens to call it.

Exactly one phase holds the lock, and it is the shortest: the selection
of the new generation as the Batch head. It is a compare-and-swap rather
than a write, because the slow phases ran unguarded and the world may
have moved while they did. Selecting names the head the plan was built
on; a head that is no longer that one means another integration landed
first and this one is a descendant of a superseded generation, which is
refused rather than merged over. Presenting a generation that is already
the head is the same effect asked for twice and replays.

Nothing here opens a session, resolves a repository or runs git. The
workspace, the lock and the generation store are handed in, so the order
of the phases and the shape of the swap stay decidable without a tree.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.config.schema import IntegrationCommitUnit, TaskReference
from eawf.kernel.delivery.integration import (
    IntegrationGeneration,
    IntegrationGenerationKey,
    IntegrationGenerationLedger,
    RepoRelativePath,
)
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind, canonical_digest
from eawf.kernel.runtime.candidate import CandidateBundle
from eawf.kernel.state.epoch2.base import Sha256DigestStr, ShaStr, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import BatchUrn, RepositoryUrn
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.runtime.integration.apply import (
    CandidateApplication,
    IntegrationRefusal,
    IntegrationRefusedError,
    SerialApply,
    apply_serially,
    integration_change,
)
from eawf.runtime.integration.commit_policy import (
    DeliveryCommit,
    DeliveryManifest,
    build_manifest,
    integration_policy_digest,
    render_delivery_commits,
)
from eawf.runtime.integration.generations import select_generation

logger = logging.getLogger(__name__)


class IntegrationPhase(StrEnum):
    """The four steps one integration takes, in the order it takes them."""

    MATERIALIZE = "materialize"
    APPLY = "apply"
    COMMIT = "commit"
    SELECT = "select"


#: The phases in the one order an integration runs them. Kept as data
#: rather than relying on the enum's definition order, which nothing
#: stops a later edit from shuffling.
PHASE_ORDER: Final[tuple[IntegrationPhase, ...]] = (
    IntegrationPhase.MATERIALIZE,
    IntegrationPhase.APPLY,
    IntegrationPhase.COMMIT,
    IntegrationPhase.SELECT,
)


class LockDiscipline(StrEnum):
    """Whether a phase may run while the canonical state lock is held."""

    UNLOCKED = "unlocked"
    STATE_LOCKED = "state_locked"


class PhaseLockTableError(ValueError):
    """The phase lock table does not answer for every declared phase."""


def compile_phase_locks(
    disciplines: Mapping[IntegrationPhase, LockDiscipline],
) -> Mapping[IntegrationPhase, LockDiscipline]:
    """Return *disciplines* once it answers for every phase, exactly once.

    Args:
        disciplines: One lock discipline per phase.

    Returns:
        The table, as a read-only mapping.

    Raises:
        PhaseLockTableError: A phase has no discipline, the order tuple
            and the enum disagree, or more than one phase holds the lock.
            All three would let slow work run under the lock without
            anybody having decided that it should.
    """
    missing = sorted(phase.value for phase in IntegrationPhase if phase not in disciplines)
    if missing:
        raise PhaseLockTableError(f"no lock discipline declared for phase {', '.join(missing)}")
    if set(PHASE_ORDER) != set(IntegrationPhase) or len(PHASE_ORDER) != len(IntegrationPhase):
        raise PhaseLockTableError("the phase order does not list every phase exactly once")
    locked = [
        phase.value
        for phase, discipline in disciplines.items()
        if discipline is LockDiscipline.STATE_LOCKED
    ]
    if len(locked) != 1:
        raise PhaseLockTableError(
            f"exactly one phase holds the state lock, found {locked or 'none'}"
        )
    return MappingProxyType(dict(disciplines))


#: Which phases may hold the canonical state lock. Only the swap does:
#: the other three are slow, and a slow phase under the lock blocks every
#: other writer of the tree for as long as it runs.
PHASE_LOCKS: Final[Mapping[IntegrationPhase, LockDiscipline]] = compile_phase_locks(
    {
        IntegrationPhase.MATERIALIZE: LockDiscipline.UNLOCKED,
        IntegrationPhase.APPLY: LockDiscipline.UNLOCKED,
        IntegrationPhase.COMMIT: LockDiscipline.UNLOCKED,
        IntegrationPhase.SELECT: LockDiscipline.STATE_LOCKED,
    }
)

#: The phases that run with no lock held, in phase order.
UNLOCKED_PHASES: Final[tuple[IntegrationPhase, ...]] = tuple(
    phase for phase in PHASE_ORDER if PHASE_LOCKS[phase] is LockDiscipline.UNLOCKED
)

#: The one phase the lock is held for.
LOCKED_PHASE: Final[IntegrationPhase] = next(
    phase for phase in PHASE_ORDER if PHASE_LOCKS[phase] is LockDiscipline.STATE_LOCKED
)


def integration_generation_id(ordinal: int) -> str:
    """Return the generation key one ordinal names.

    Args:
        ordinal: The generation ordinal, counting from one.

    Returns:
        The ``ING-######`` key.

    Raises:
        ValueError: The ordinal is not positive or does not fit the key.
    """
    if ordinal <= 0 or ordinal > 999999:
        raise ValueError(f"generation ordinal {ordinal} does not fit an ING-###### key")
    return f"ING-{ordinal:06d}"


def generation_record_key(generation: IntegrationGeneration) -> str:
    """Return the ledger key one generation is filed under.

    The Batch key is part of it: the ordinal counts per Batch, so two
    Batches at the same ordinal would otherwise share one line key in a
    ledger that holds every Batch of the tree.
    """
    return f"{generation.id}-{generation.batch_ref.entity_key}"


class DeliveredRevision(BaseModel):
    """The commit object one integration authored, as the workspace saw it.

    Attributes:
        head_sha: The delivery commit.
        tree_sha: Its tree.
        parent_sha: The commit it was authored on, or ``None`` at a root.
        patch_digest: The digest of the patch that was applied.
        diff_digest: The digest of the diff against the target base.
        tree_digest: The digest of the resulting tree's content.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: ShaStr
    tree_sha: ShaStr
    parent_sha: ShaStr | None
    patch_digest: Sha256DigestStr
    diff_digest: Sha256DigestStr
    tree_digest: Sha256DigestStr


class IntegrationWorkspace(Protocol):
    """The daemon-owned tree one integration runs in.

    Every method is slow and none of them may be called under the
    canonical state lock; :func:`integrate_batch` calls all three before
    it takes it.
    """

    def materialize(self, *, base_commit: ShaStr) -> ShaStr:
        """Return the head the workspace was materialized at."""

    def apply(self, bundle: CandidateBundle) -> CandidateApplication:
        """Apply one sealed candidate and report what happened."""

    def commit(self, delivery: DeliveryCommit) -> DeliveredRevision:
        """Author the delivery commit and return the object it produced."""


class StateLock(Protocol):
    """The canonical state lock, taken for the swap and nothing else."""

    def hold(self) -> AbstractContextManager[None]:
        """Return the lock, held for the duration of the ``with`` block."""


class GenerationStore(Protocol):
    """One Batch's generation history, read and rewritten under the lock."""

    def read(self) -> IntegrationGenerationLedger:
        """Return the Batch's generation history as it stands."""

    def write(self, ledger: IntegrationGenerationLedger) -> None:
        """Leave *ledger* as the Batch's generation history."""


class IntegrationPlan(BaseModel):
    """One delivery, decided before any of it is applied.

    Attributes:
        repository_ref: The repository the Batch lives in.
        manifest: The ordered candidates this delivery carries.
        delivery: The commit the manifest is authored as.
        ordered: The sealed candidates, in integration order.
        source_base: The base the candidates were produced from.
        target_base: The Batch head this delivery is applied over.
        parent_generation_id: The generation the plan was built on, or
            ``None`` when the Batch has no generation yet. The swap
            refuses when the head is no longer this one.
        policy_digest: The digest of the rules the plan was built under.
        affected_criterion_ids: The criteria the change invalidates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_ref: RepositoryUrn
    manifest: DeliveryManifest
    delivery: DeliveryCommit
    ordered: tuple[CandidateBundle, ...] = Field(min_length=1)
    source_base: RevisionBinding
    target_base: RevisionBinding
    parent_generation_id: IntegrationGenerationKey | None
    policy_digest: Sha256DigestStr
    affected_criterion_ids: tuple[GateIdentityStr, ...] = ()

    @property
    def generation(self) -> int:
        """Return the ordinal this delivery would select."""
        return self.manifest.generation

    @property
    def batch_ref(self) -> BatchUrn:
        """Return the Batch being delivered."""
        return self.manifest.batch_ref

    @model_validator(mode="after")
    def _plan_is_coherent(self) -> Self:
        """Tie the manifest, the commit and the two bases to one Batch.

        Raises:
            ValueError: The manifest and the commit carry different
                candidates, the ordinal does not follow the target base,
                a base was taken on another Batch, or the commit's
                manifest id is not this manifest's.
        """
        if self.delivery.manifest_id != self.manifest.manifest_id:
            raise ValueError("the delivery commit names another manifest than the plan's")
        planned = tuple(entry.candidate_ref for entry in self.manifest.entries)
        applied = tuple(bundle.candidate_ref for bundle in self.ordered)
        if planned != applied:
            raise ValueError("the manifest and the ordered candidates are not the same sequence")
        for name, binding in (
            ("source_base", self.source_base),
            ("target_base", self.target_base),
        ):
            if binding.batch_ref != self.manifest.batch_ref:
                raise ValueError(f"{name} binds {binding.batch_ref}, not the manifest's Batch")
        if self.manifest.generation != self.target_base.integration_generation + 1:
            raise ValueError(
                f"generation {self.manifest.generation} does not follow the target base "
                f"generation {self.target_base.integration_generation}"
            )
        return self


def plan_deliveries(
    ordered: Sequence[CandidateBundle],
    *,
    repository_ref: RepositoryUrn,
    batch_ref: BatchUrn,
    source_base: RevisionBinding,
    target_base: RevisionBinding,
    parent_generation_id: IntegrationGenerationKey | None,
    subjects: dict[str, str],
    batch_subject: str,
    unit: IntegrationCommitUnit,
    task_reference: TaskReference,
    affected_criterion_ids: Sequence[GateIdentityStr] = (),
) -> tuple[IntegrationPlan, ...]:
    """Return the deliveries *ordered* becomes under the configured unit.

    Under ``batch`` that is one plan carrying every candidate; under
    ``task`` it is one plan per candidate, each selecting a generation of
    its own and parenting on the plan before it.

    Args:
        ordered: The candidates in integration order.
        repository_ref: The repository the Batch lives in.
        batch_ref: The Batch being delivered.
        source_base: The base the candidates were produced from.
        target_base: The Batch head the first plan is applied over.
        parent_generation_id: The generation the first plan is built on.
        subjects: One subject line per candidate, keyed by candidate ref.
        batch_subject: The squashed commit's subject, used under ``batch``.
        unit: The configured commit unit.
        task_reference: The configured Task reference placement.
        affected_criterion_ids: The criteria the change invalidates.

    Returns:
        The plans, in the order they must be integrated.

    Raises:
        IntegrationRefusedError: Nothing was offered, or a subject with
            its Task references does not fit the subject width.
        KeyError: A candidate has no subject.
    """
    policy_digest = integration_policy_digest(unit=unit, task_reference=task_reference)
    criteria = tuple(affected_criterion_ids)
    groups = [tuple(ordered)] if unit == "batch" else [(bundle,) for bundle in ordered]
    plans: list[IntegrationPlan] = []
    base, parent = target_base, parent_generation_id
    for group in groups:
        manifest = build_manifest(
            group,
            batch_ref=batch_ref,
            generation=base.integration_generation + 1,
            subjects=subjects,
        )
        commits = render_delivery_commits(
            manifest, unit=unit, task_reference=task_reference, batch_subject=batch_subject
        )
        plan = IntegrationPlan(
            repository_ref=repository_ref,
            manifest=manifest,
            delivery=commits[0],
            ordered=group,
            source_base=source_base,
            target_base=base,
            parent_generation_id=parent,
            policy_digest=policy_digest,
            affected_criterion_ids=criteria,
        )
        plans.append(plan)
        parent = integration_generation_id(plan.generation)
        base = base.model_copy(update={"integration_generation": plan.generation})
    logger.debug(f"plan_deliveries unit={unit} plans={len(plans)} candidates={len(ordered)}")
    return tuple(plans)


def build_integration_generation(
    plan: IntegrationPlan, revision: DeliveredRevision, *, now: datetime
) -> IntegrationGeneration:
    """Return the generation one delivered revision selects.

    Args:
        plan: The delivery that was applied.
        revision: The commit object the workspace authored.
        now: The stamp the generation is created at.

    Returns:
        The generation, marked selected, ready for the swap.

    Raises:
        pydantic.ValidationError: The revision does not bind the plan's
            Batch and ordinal.
    """
    change = integration_change(plan.ordered)
    integrated = RevisionBinding(
        repository_ref=plan.repository_ref,
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha=revision.head_sha,
        tree_sha=revision.tree_sha,
        parent_sha=revision.parent_sha,
        batch_ref=plan.batch_ref,
        integration_generation=plan.generation,
        manifest_digest=plan.manifest.digest,
        criteria_digest=canonical_digest(list(plan.affected_criterion_ids)),
        policy_digest=plan.policy_digest,
        environment_digest=None,
        bound_at=now,
    )
    paths: tuple[RepoRelativePath, ...] = change.changed_paths
    return IntegrationGeneration(
        id=integration_generation_id(plan.generation),
        batch_ref=plan.batch_ref,
        generation=plan.generation,
        parent_generation_id=plan.parent_generation_id,
        source_candidate_bundle_id=_bundle_key(plan),
        source_base=plan.source_base,
        target_base=plan.target_base,
        integrated_revision=integrated,
        patch_digest=revision.patch_digest,
        diff_digest=revision.diff_digest,
        tree_digest=revision.tree_digest,
        changed_paths=paths,
        affected_task_refs=tuple(entry.task_ref for entry in plan.manifest.entries),
        affected_criterion_ids=plan.affected_criterion_ids,
        integration_policy_digest=plan.policy_digest,
        selected=True,
        created_at=now,
    )


def _bundle_key(plan: IntegrationPlan) -> str:
    """Return the ``CB-########`` key this delivery's proposal is named by.

    A squashed Batch has no single sealed bundle behind it, so the key is
    derived from the manifest: it names the exact ordered proposal the
    generation came from, and two integrations of the same proposal name
    the same key.
    """
    body = plan.manifest.digest.removeprefix("sha256:")
    return f"CB-{int(body[:8], 16) % 100000000:08d}"


def _same_effect(head: IntegrationGeneration, generation: IntegrationGeneration) -> bool:
    """Return whether *head* is the very effect *generation* asks for.

    The ordinal alone is not enough. A generation is named by its
    ordinal, so two deliveries prepared on one base carry one id while
    delivering different trees; comparing the commit object and the tree
    as well is what keeps the second of them a superseded descendant
    rather than a replay of the first.
    """
    return (
        head.id == generation.id
        and head.integrated_revision.head_sha == generation.integrated_revision.head_sha
        and head.tree_digest == generation.tree_digest
    )


class CasDisposition(StrEnum):
    """What the compare-and-swap did with the generation it was handed."""

    SELECTED = "selected"
    REPLAYED = "replayed"


class CasOutcome(BaseModel):
    """The result of one swap.

    Attributes:
        disposition: Whether this call selected the generation or found
            it already selected.
        generation_id: The generation the swap was about.
        head_generation: The ordinal that is the Batch head afterwards.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: CasDisposition
    generation_id: IntegrationGenerationKey
    head_generation: StrictPositiveInt


def select_delivered_generation(
    store: GenerationStore,
    generation: IntegrationGeneration,
    *,
    expected_head: IntegrationGenerationKey | None,
) -> CasOutcome:
    """Make *generation* the Batch head, or refuse a superseded descendant.

    The whole effect is a read, a comparison and one write, so the caller
    holds the canonical state lock for as little as the swap itself.

    Args:
        store: The Batch's generation history.
        generation: The generation to select; must be marked selected.
        expected_head: The generation the plan was built on, or ``None``
            when the Batch had no generation then.

    Returns:
        Whether the swap selected the generation or replayed one that was
        already the head.

    Raises:
        IntegrationRefusedError: The head is not the one the plan was
            built on, so this delivery is a descendant of a superseded
            generation and applying it would drop whatever landed first.
        ValueError: The generation is not marked selected.
        pydantic.ValidationError: The generation does not extend the
            history linearly.
    """
    ledger = store.read()
    head = ledger.head
    if head is not None and _same_effect(head, generation):
        logger.info(f"select_delivered_generation replayed generation={generation.id}")
        return CasOutcome(
            disposition=CasDisposition.REPLAYED,
            generation_id=generation.id,
            head_generation=head.generation,
        )
    head_id = None if head is None else head.id
    if head_id != expected_head:
        raise IntegrationRefusedError(
            IntegrationRefusal.GENERATION_SUPERSEDED,
            f"generation {generation.id} was prepared on {expected_head or 'no generation'} but "
            f"the Batch head is {head_id or 'no generation'}, so it is a descendant of a "
            "superseded generation",
        )
    store.write(select_generation(ledger, generation))
    logger.info(
        f"select_delivered_generation selected generation={generation.id} "
        f"ordinal={generation.generation}"
    )
    return CasOutcome(
        disposition=CasDisposition.SELECTED,
        generation_id=generation.id,
        head_generation=generation.generation,
    )


class IntegrationOutcomeKind(StrEnum):
    """How one integration ended."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"


class IntegrationOutcome(BaseModel):
    """What one run of :func:`integrate_batch` concluded.

    Attributes:
        kind: Whether the delivery landed or a candidate blocked it.
        applied: How far the serial pass got.
        revision: The delivery commit, or ``None`` when blocked.
        generation: The generation that was selected, or ``None``.
        swap: The swap's result, or ``None`` when blocked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: IntegrationOutcomeKind
    applied: SerialApply
    revision: DeliveredRevision | None = None
    generation: IntegrationGeneration | None = None
    swap: CasOutcome | None = None

    @model_validator(mode="after")
    def _outcome_matches_kind(self) -> Self:
        """Require the delivery facts exactly when the delivery landed.

        Raises:
            ValueError: A blocked outcome carries a revision, a
                generation or a swap, or a delivered one is missing any
                of them or contradicts the serial pass.
        """
        delivered = self.kind is IntegrationOutcomeKind.DELIVERED
        present = (self.revision, self.generation, self.swap)
        if delivered and any(item is None for item in present):
            raise ValueError("a delivered outcome carries its revision, generation and swap")
        if not delivered and any(item is not None for item in present):
            raise ValueError("a blocked outcome carries no revision, generation or swap")
        if delivered == self.applied.blocked:
            raise ValueError("the outcome kind and the serial pass disagree about the block")
        return self


def integrate_batch(
    plan: IntegrationPlan,
    *,
    workspace: IntegrationWorkspace,
    lock: StateLock,
    store: GenerationStore,
    now: datetime,
) -> IntegrationOutcome:
    """Run one delivery: materialize, apply, commit, then swap under the lock.

    The first three phases run with no lock held, because each of them is
    slow and any of them may fail. Only the swap takes the lock, and it
    takes it for one read, one comparison and one write.

    Args:
        plan: The delivery to integrate.
        workspace: The daemon-owned tree it runs in.
        lock: The canonical state lock, taken for the swap alone.
        store: The Batch's generation history.
        now: The stamp the generation is created at.

    Returns:
        The delivered generation, or the block a conflicting candidate
        left behind. A blocked run authors no commit and moves no ref.

    Raises:
        IntegrationRefusedError: The Batch head moved while the slow
            phases ran, so the delivery is a superseded descendant.
    """
    workspace.materialize(base_commit=plan.manifest.base_commit)
    applied = apply_serially(plan.ordered, applier=workspace.apply)
    if applied.blocked:
        logger.info(
            f"integrate_batch blocked batch={plan.batch_ref.entity_key} "
            f"candidate={applied.blocked_on}"
        )
        return IntegrationOutcome(kind=IntegrationOutcomeKind.BLOCKED, applied=applied)
    revision = workspace.commit(plan.delivery)
    generation = build_integration_generation(plan, revision, now=now)
    with lock.hold():
        swap = select_delivered_generation(
            store, generation, expected_head=plan.parent_generation_id
        )
    return IntegrationOutcome(
        kind=IntegrationOutcomeKind.DELIVERED,
        applied=applied,
        revision=revision,
        generation=generation,
        swap=swap,
    )


__all__ = [
    "LOCKED_PHASE",
    "PHASE_LOCKS",
    "PHASE_ORDER",
    "UNLOCKED_PHASES",
    "CasDisposition",
    "CasOutcome",
    "DeliveredRevision",
    "GenerationStore",
    "IntegrationOutcome",
    "IntegrationOutcomeKind",
    "IntegrationPhase",
    "IntegrationPlan",
    "IntegrationWorkspace",
    "LockDiscipline",
    "PhaseLockTableError",
    "StateLock",
    "build_integration_generation",
    "compile_phase_locks",
    "generation_record_key",
    "integrate_batch",
    "integration_generation_id",
    "plan_deliveries",
    "select_delivered_generation",
]
