"""What an operator is shown before accepting a Milestone, and how it revises.

:class:`MilestoneAcceptanceBundle` is the exact thing an approval is
given to: one revision of one Milestone's acceptance journey, each step
carrying the evidence that demonstrates it, bound to the exact tree the
acceptance would land on. Its :meth:`MilestoneAcceptanceBundle.digest` is
what an approval binds to, so "approved" names a set of bytes rather than
a Milestone whose contents may since have changed.

A bundle is never edited. Every model here is frozen, so the correction
of a bundle is a successor revision, and
:class:`AcceptanceBundleLedger` refuses a chain whose successor does not
carry the predecessor's own digest. That is what makes the immutability
checkable on read instead of promised by whoever wrote the successor: if
an earlier revision were rewritten, its digest would move and the chain
would stop validating.

:class:`EvidenceView` is the read side. It is a frozen mapping from
``EVD-####`` to the row recorded under it and holds no path, no handle
and no session, so looking evidence up is arithmetic over data already in
hand rather than a read that could touch the tree.

The models are pure. Checking an approval against a bundle, and opening
the successor a repair earns, live in
:mod:`eawf.workflow.delivery.acceptance`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.spec.common import EvidenceKind
from eawf.kernel.state.epoch2.base import (
    AcceptanceStepId,
    Epoch2Model,
    NonEmptyStr,
    Sha256DigestStr,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.milestone import validate_evidence_kinds
from eawf.kernel.state.epoch2.urns import BatchUrn, EvidenceUrn, MilestoneUrn
from eawf.kernel.state.epoch2.values import ExactRevisionBinding, TransitionReason
from eawf.kernel.state.types import UtcDatetime


def _validate_evidence_key(value: str) -> str:
    """Admit only a canonical ``EVD-####`` evidence key."""
    return validate_entity_key(EntityKind.EVIDENCE, value)


#: An ``EVD-####`` evidence key. The grammar has one home in the identity
#: package, so the alias delegates rather than re-spelling it.
EvidenceKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_evidence_key),
]

#: The first revision every acceptance bundle starts at.
FIRST_BUNDLE_REVISION = 1


class _FrozenModel(Epoch2Model):
    """Strict and immutable: a bundle edited after approval is not the approved one."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptanceStepOutcome(_FrozenModel):
    """What one step of the acceptance journey actually showed.

    The evidence references are required on a step that passed: a step
    claiming success with nothing behind it is the claim the bundle
    exists to replace. A step that did not pass may carry evidence too,
    because a recorded failure is worth reading.
    """

    step_id: AcceptanceStepId
    passed: StrictBool
    observation: NonEmptyStr
    evidence_kinds: tuple[EvidenceKind, ...]
    evidence_refs: tuple[EvidenceUrn, ...] = ()

    @model_validator(mode="after")
    def _passing_step_shows_its_evidence(self) -> Self:
        """Require distinct evidence kinds, and a reference behind a pass.

        Raises:
            ValueError: The kinds are empty or repeat, a passing step
                names no evidence, or a reference is listed twice.
        """
        validate_evidence_kinds(self.evidence_kinds)
        if self.passed and not self.evidence_refs:
            raise ValueError(f"step {self.step_id} passed and names no evidence")
        keys = [item.entity_key for item in self.evidence_refs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"step {self.step_id} names the same evidence twice")
        return self


class MilestoneAcceptanceBundle(_FrozenModel):
    """One revision of everything an operator reads before accepting.

    ``revision`` and ``supersedes_revision`` make the chain readable from
    one row, and ``supersedes_digest`` makes it checkable: a successor
    names the exact bytes it replaces, so a rewritten predecessor breaks
    the chain rather than silently standing in for what was approved.
    """

    milestone_ref: MilestoneUrn
    revision: StrictPositiveInt
    supersedes_revision: StrictPositiveInt | None = None
    supersedes_digest: Sha256DigestStr | None = None
    repair_reason: TransitionReason | None = None
    accepted_binding: ExactRevisionBinding
    required_batch_refs: tuple[BatchUrn, ...] = ()
    steps: tuple[AcceptanceStepOutcome, ...] = Field(min_length=1)
    sealed_at: UtcDatetime

    @property
    def blocking_step_ids(self) -> tuple[str, ...]:
        """Return the steps that did not pass, sorted."""
        return tuple(sorted(item.step_id for item in self.steps if not item.passed))

    @property
    def evidence_keys(self) -> tuple[str, ...]:
        """Return every ``EVD-####`` the bundle cites, sorted and deduplicated."""
        return tuple(sorted({ref.entity_key for item in self.steps for ref in item.evidence_refs}))

    def digest(self) -> str:
        """Return the ``sha256:`` digest an approval binds to.

        Returns:
            The canonical digest of the whole bundle. Every field feeds
            it, so an approval cannot survive any change to what was
            shown.
        """
        return canonical_digest(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def _revision_chain_is_coherent(self) -> Self:
        """Tie the predecessor fields and the repair reason to the revision.

        Raises:
            ValueError: The first revision claims a predecessor, a later
                revision names none or names one that is not the
                immediately preceding ordinal, or the repair reason and
                the successor status disagree.
        """
        first = self.revision == FIRST_BUNDLE_REVISION
        named = [
            name
            for name in ("supersedes_revision", "supersedes_digest", "repair_reason")
            if getattr(self, name) is not None
        ]
        if first and named:
            raise ValueError(
                f"revision {FIRST_BUNDLE_REVISION} supersedes nothing, so it cannot carry "
                f"{', '.join(named)}"
            )
        if not first and len(named) != 3:
            raise ValueError(
                f"revision {self.revision} must name supersedes_revision, supersedes_digest "
                "and the repair_reason that opened it"
            )
        if self.supersedes_revision is not None and self.supersedes_revision != self.revision - 1:
            raise ValueError(
                f"revision {self.revision} supersedes {self.supersedes_revision}, not the "
                f"revision {self.revision - 1} before it"
            )
        return self

    @model_validator(mode="after")
    def _steps_are_unique(self) -> Self:
        """Require each journey step to be reported at most once.

        Raises:
            ValueError: A step id appears twice, so the bundle would show
                two outcomes for one step.
        """
        ids = [item.step_id for item in self.steps]
        repeated = sorted({item for item in ids if ids.count(item) > 1})
        if repeated:
            raise ValueError(f"steps {', '.join(repeated)} are reported more than once")
        return self

    def open_successor(
        self,
        *,
        reason: TransitionReason,
        steps: Iterable[AcceptanceStepOutcome],
        accepted_binding: ExactRevisionBinding,
        at: UtcDatetime,
    ) -> Self:
        """Return the next revision, leaving this one exactly as it is.

        Nothing about the receiver is touched: the successor is built from
        a dump, carries this revision's digest, and this object keeps the
        digest it already had -- which is what an approval given against
        it stays bound to.

        Args:
            reason: Why a repair was asked for.
            steps: What the repaired journey shows.
            accepted_binding: The exact tree the successor is taken on.
            at: When the successor was sealed.

        Returns:
            The successor bundle at ``revision + 1``.

        Raises:
            ValueError: The successor breaks a bundle rule, such as an
                empty or repeated step list.
        """
        return self.model_validate(
            {
                **self.model_dump(),
                "revision": self.revision + 1,
                "supersedes_revision": self.revision,
                "supersedes_digest": self.digest(),
                "repair_reason": reason.model_dump(),
                "accepted_binding": accepted_binding.model_dump(),
                "steps": [item.model_dump() for item in steps],
                "sealed_at": at,
            }
        )


class AcceptanceBundleLedger(_FrozenModel):
    """One Milestone's bundle revisions, oldest first.

    The chain is checked in both directions: each successor follows the
    ordinal before it and carries that revision's own digest. A rewritten
    earlier revision therefore fails to validate here rather than quietly
    replacing what an approval was given to.
    """

    milestone_ref: MilestoneUrn
    bundles: tuple[MilestoneAcceptanceBundle, ...] = ()

    @property
    def head(self) -> MilestoneAcceptanceBundle | None:
        """Return the current revision, or ``None`` before the first one."""
        return self.bundles[-1] if self.bundles else None

    def at(self, revision: int) -> MilestoneAcceptanceBundle | None:
        """Return the bundle filed at *revision*, or ``None``.

        Args:
            revision: The ordinal to look up.

        Returns:
            The bundle of that revision, or ``None`` when none is filed.
        """
        for item in self.bundles:
            if item.revision == revision:
                return item
        return None

    @model_validator(mode="after")
    def _chain_is_unbroken(self) -> Self:
        """Require one Milestone, ordinals from one, and matching predecessor digests.

        Raises:
            ValueError: A bundle belongs to another Milestone, the
                ordinals do not run from one without gaps, a successor
                predates its predecessor, or a successor's recorded
                predecessor digest is not the predecessor's own.
        """
        previous: MilestoneAcceptanceBundle | None = None
        for index, item in enumerate(self.bundles):
            if item.milestone_ref != self.milestone_ref:
                raise ValueError(f"revision {item.revision} belongs to {item.milestone_ref}")
            expected = FIRST_BUNDLE_REVISION + index
            if item.revision != expected:
                raise ValueError(f"revision {item.revision} is filed where {expected} belongs")
            if previous is not None:
                if item.supersedes_digest != previous.digest():
                    raise ValueError(
                        f"revision {item.revision} records a predecessor digest that is not "
                        f"revision {previous.revision}'s own, so the chain is broken"
                    )
                if item.sealed_at < previous.sealed_at:
                    raise ValueError(f"revision {item.revision} predates {previous.revision}")
            previous = item
        return self


class EvidenceRow(_FrozenModel):
    """One recorded observation, as the evidence view holds it."""

    id: EvidenceKey
    kind: EvidenceKind
    summary: NonEmptyStr
    recorded_at: UtcDatetime


class EvidenceView(_FrozenModel):
    """Evidence already in hand, addressable by ``EVD-####``.

    The view carries no path, no file handle and no session, so a lookup
    has nothing to write through even in principle. That is the whole of
    the read-only property: it is the absence of a write surface rather
    than a rule about how the view is used.
    """

    rows: tuple[EvidenceRow, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        """Return every evidence key the view holds, sorted."""
        return tuple(sorted(item.id for item in self.rows))

    def row(self, evidence_key: str) -> EvidenceRow | None:
        """Return the row recorded under *evidence_key*, or ``None``.

        Args:
            evidence_key: The ``EVD-####`` to look up.

        Returns:
            The row, or ``None`` when the view holds none for that key.
        """
        for item in self.rows:
            if item.id == evidence_key:
                return item
        return None

    def missing(self, evidence_keys: Iterable[str]) -> tuple[str, ...]:
        """Return the requested keys the view holds no row for, sorted.

        Args:
            evidence_keys: The keys a bundle cites.

        Returns:
            The keys with no row, sorted.
        """
        held = set(self.keys)
        return tuple(sorted(set(evidence_keys) - held))

    @model_validator(mode="after")
    def _one_row_per_key(self) -> Self:
        """Require each evidence key to appear once.

        Raises:
            ValueError: Two rows share a key, which would make the
                lookup depend on iteration order.
        """
        ids = [item.id for item in self.rows]
        repeated = sorted({item for item in ids if ids.count(item) > 1})
        if repeated:
            raise ValueError(f"evidence {', '.join(repeated)} is held more than once")
        return self


__all__ = [
    "FIRST_BUNDLE_REVISION",
    "AcceptanceBundleLedger",
    "AcceptanceStepOutcome",
    "EvidenceKey",
    "EvidenceRow",
    "EvidenceView",
    "MilestoneAcceptanceBundle",
]
