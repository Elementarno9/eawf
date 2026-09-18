"""What a Batch's integrated work looks like as a commit, and why one.

History does not get a commit per Run, per Task claim or per integration
generation. The unit is configured: under ``batch`` the daemon squashes
the Batch's sealed candidates into a single delivery commit before
verification and before any push, so a reviewer reads a handful of
thematic commits rather than one per worker; under ``task`` each Task
keeps a delivery commit of its own.

Two trailers carry the link back to the records, because a bare
conventional subject cannot encode identity without becoming unreadable.
``Task:`` names each Task whose work the commit carries -- one line per
Task under either unit, so a squashed Batch stays greppable by Task --
and ``Eawf-Provenance:`` names the delivery manifest. The Task reference
is configurable and may be moved into the subject or suppressed
entirely; the provenance trailer is not configurable and is always
rendered, since a commit whose manifest cannot be found is a commit
nobody can reconstruct the inputs of.

The manifest id is derived from the manifest's own content rather than
minted. Two integrations of the same ordered candidates onto the same
base name the same manifest, so a rematerialization after a lost
workspace produces the same trailer instead of a second identity for one
delivery.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from eawf.kernel.config.schema import IntegrationCommitUnit, TaskReference
from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.runtime.candidate import CandidateBundle, CandidateId
from eawf.kernel.runtime.provider import Digest
from eawf.kernel.state.epoch2.base import ShaStr, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import BatchUrn, TaskUrn
from eawf.runtime.integration.apply import (
    ORDER_KEY_FIELDS,
    IntegrationRefusal,
    IntegrationRefusedError,
    order_key,
)

logger = logging.getLogger(__name__)


#: The trailer key each Task is named under.
TASK_TRAILER_KEY: Final = "Task"

#: The trailer key the delivery manifest is named under.
PROVENANCE_TRAILER_KEY: Final = "Eawf-Provenance"

#: The scheme a provenance trailer addresses its manifest through.
PROVENANCE_SCHEME: Final = "manifest://"

#: How many hex characters of the manifest digest a manifest id carries.
MANIFEST_ID_WIDTH: Final = 32

#: The longest a rendered commit subject may be, matching the width git
#: tooling and ``git log`` assume.
MAX_SUBJECT_LENGTH: Final = 72

#: Version of the apply, order and conflict rules a generation was taken
#: under. It is part of the policy digest, so changing a rule without
#: changing this constant cannot leave two generations claiming one
#: policy.
INTEGRATION_POLICY_VERSION: Final = "1"


def _validate_subject(value: str) -> str:
    """Admit only a single trimmed line that does not end in a period.

    Raises:
        ValueError: The subject spans lines, carries padding, or ends in
            a period -- all three of which read wrong in ``git log``.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("a commit subject is one line")
    if value != value.strip():
        raise ValueError("a commit subject carries no leading or trailing space")
    if value.endswith("."):
        raise ValueError("a commit subject does not end in a period")
    return value


#: One line of subject text, bounded at the width git tooling assumes.
CommitSubject = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_SUBJECT_LENGTH),
    AfterValidator(_validate_subject),
]

#: A ``MFT-`` manifest id, derived from the manifest's own content.
ManifestId = Annotated[str, StringConstraints(strict=True, pattern=r"^MFT-[0-9a-f]{32}$")]


def _validate_trailer(value: str) -> str:
    """Admit only a single ``Key: value`` git trailer line.

    Raises:
        ValueError: The line spans lines or is not ``Key: value``.
    """
    if "\n" in value or "\r" in value:
        raise ValueError("a trailer is one line")
    key, separator, body = value.partition(": ")
    if not separator or not key or not body.strip():
        raise ValueError(f"trailer {value!r} is not a 'Key: value' line")
    return value


#: One rendered git trailer line.
TrailerLine = Annotated[
    str,
    StringConstraints(strict=True, min_length=3, max_length=200),
    AfterValidator(_validate_trailer),
]


class _FrozenModel(BaseModel):
    """Strict and immutable: a manifest edited in place is a forged one."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DeliveryEntry(_FrozenModel):
    """One sealed candidate's row of the delivery manifest.

    Attributes:
        candidate_ref: The candidate's derived identity.
        task_ref: The Task whose delivery it is.
        resulting_tree_digest: The tree the work produced.
        subject: The one line that Task's work is described by.
    """

    candidate_ref: CandidateId
    task_ref: TaskUrn
    resulting_tree_digest: Digest
    subject: CommitSubject


class DeliveryManifest(_FrozenModel):
    """Everything one delivery commit carries, in the order it was applied.

    Attributes:
        batch_ref: The Batch being delivered.
        generation: The ordinal this delivery would select.
        base_commit: The commit every candidate was produced from.
        entries: The candidates, in integration order.
    """

    batch_ref: BatchUrn
    generation: StrictPositiveInt
    base_commit: ShaStr
    entries: tuple[DeliveryEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _entries_are_in_integration_order(self) -> Self:
        """Require the rows to be sorted by the integration order key.

        Raises:
            ValueError: A row is out of order or repeats a Task, either
                of which would make the manifest digest depend on who
                assembled it rather than on what was integrated.
        """
        keys = [
            order_key(
                task_ref=str(entry.task_ref),
                resulting_tree_digest=entry.resulting_tree_digest,
                candidate_ref=entry.candidate_ref,
            )
            for entry in self.entries
        ]
        if keys != sorted(keys):
            raise ValueError("manifest entries are not in integration order")
        tasks = [str(entry.task_ref) for entry in self.entries]
        if len(set(tasks)) != len(tasks):
            raise ValueError("manifest entries name one Task twice")
        return self

    @property
    def digest(self) -> str:
        """Return the ``sha256:`` digest of this manifest's content."""
        return canonical_digest(self.model_dump(mode="json"))

    @property
    def manifest_id(self) -> str:
        """Return the derived id the provenance trailer addresses."""
        body = self.digest.removeprefix("sha256:")
        return f"MFT-{body[:MANIFEST_ID_WIDTH]}"


def build_manifest(
    ordered: Sequence[CandidateBundle],
    *,
    batch_ref: BatchUrn,
    generation: int,
    subjects: dict[str, str],
) -> DeliveryManifest:
    """Return the manifest of one ordered run of candidates.

    Args:
        ordered: The candidates in integration order.
        batch_ref: The Batch being delivered.
        generation: The ordinal this delivery would select.
        subjects: One subject line per candidate, keyed by candidate ref.

    Returns:
        The manifest, whose id the provenance trailer names.

    Raises:
        IntegrationRefusedError: Nothing was offered.
        KeyError: A candidate has no subject, so its row could not say
            what the Task did.
        pydantic.ValidationError: A subject is not one bounded line, or
            the candidates are not in integration order.
    """
    if not ordered:
        raise IntegrationRefusedError(
            IntegrationRefusal.CANDIDATES_ABSENT,
            "no sealed candidate was offered, so there is no manifest to build",
        )
    entries = tuple(
        DeliveryEntry(
            candidate_ref=bundle.candidate_ref,
            task_ref=bundle.task_ref,
            resulting_tree_digest=bundle.resulting_tree_digest,
            subject=subjects[bundle.candidate_ref],
        )
        for bundle in ordered
    )
    return DeliveryManifest(
        batch_ref=batch_ref,
        generation=generation,
        base_commit=ordered[0].base_commit,
        entries=entries,
    )


class DeliveryCommit(_FrozenModel):
    """One commit the daemon authors for an integrated delivery.

    Attributes:
        subject: The one-line summary.
        bullets: The body lines, one per Task the commit carries. Empty
            under the per-Task unit, where the subject already says what
            the single Task did.
        task_refs: The Tasks this commit delivers, in integration order.
        manifest_id: The manifest the provenance trailer addresses.
        trailers: Every rendered trailer line, in render order.
    """

    subject: CommitSubject
    bullets: tuple[CommitSubject, ...] = ()
    task_refs: tuple[TaskUrn, ...] = Field(min_length=1)
    manifest_id: ManifestId
    trailers: tuple[TrailerLine, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _provenance_is_always_rendered(self) -> Self:
        """Require exactly one provenance trailer naming this manifest.

        Raises:
            ValueError: The provenance trailer is missing, duplicated, or
                names another manifest than the commit does.
        """
        expected = f"{PROVENANCE_TRAILER_KEY}: {PROVENANCE_SCHEME}{self.manifest_id}"
        found = [line for line in self.trailers if line.startswith(f"{PROVENANCE_TRAILER_KEY}: ")]
        if found != [expected]:
            raise ValueError(f"trailers carry exactly one {expected!r}, found {found}")
        return self

    @property
    def message(self) -> str:
        """Return the full commit message, body and trailers included."""
        blocks = [self.subject]
        if self.bullets:
            blocks.append("\n".join(f"- {bullet}" for bullet in self.bullets))
        blocks.append("\n".join(self.trailers))
        return "\n\n".join(blocks)


def _subject_with_reference(subject: str, task_refs: Sequence[TaskUrn]) -> str:
    """Return *subject* with the Task keys restored in front of it.

    Raises:
        IntegrationRefusedError: The prefixed subject no longer fits the
            subject width, so the reference would truncate the summary.
    """
    keys = ",".join(ref.entity_key for ref in task_refs)
    prefixed = f"[{keys}] {subject}"
    if len(prefixed) > MAX_SUBJECT_LENGTH:
        raise IntegrationRefusedError(
            IntegrationRefusal.SUBJECT_OVERFLOW,
            f"the subject with {len(task_refs)} Task references is {len(prefixed)} characters, "
            f"past the {MAX_SUBJECT_LENGTH} a subject may be",
        )
    return prefixed


def _trailers(
    task_refs: Sequence[TaskUrn], *, manifest_id: str, task_reference: TaskReference
) -> tuple[str, ...]:
    """Return the trailer lines one commit carries, in render order.

    The Task trailers come first and the provenance trailer last, so the
    provenance line is the one a reader's eye lands on however many Tasks
    a squashed Batch carries.
    """
    lines = [f"{TASK_TRAILER_KEY}: {ref.entity_key}" for ref in task_refs]
    rendered = lines if task_reference == "trailer" else []
    return (*rendered, f"{PROVENANCE_TRAILER_KEY}: {PROVENANCE_SCHEME}{manifest_id}")


def render_delivery_commits(
    manifest: DeliveryManifest,
    *,
    unit: IntegrationCommitUnit,
    task_reference: TaskReference,
    batch_subject: str,
) -> tuple[DeliveryCommit, ...]:
    """Return the commits one manifest delivers under the configured unit.

    Args:
        manifest: The ordered delivery manifest.
        unit: ``batch`` squashes the whole manifest into one commit;
            ``task`` gives each entry its own.
        task_reference: Where the Task identity goes -- a trailer per
            Task, the Task keys in the subject, or nowhere.
        batch_subject: The one-line summary of the squashed commit. It is
            unused under the per-Task unit, where each entry's own
            subject is the commit's.

    Returns:
        One commit under ``batch``; one per entry, in integration order,
        under ``task``.

    Raises:
        IntegrationRefusedError: The subject with its Task references
            does not fit the subject width.
        pydantic.ValidationError: A subject is not one bounded line.
    """
    manifest_id = manifest.manifest_id
    if unit == "batch":
        task_refs = tuple(entry.task_ref for entry in manifest.entries)
        return (
            _commit(
                subject=batch_subject,
                bullets=tuple(entry.subject for entry in manifest.entries),
                task_refs=task_refs,
                manifest_id=manifest_id,
                task_reference=task_reference,
            ),
        )
    return tuple(
        _commit(
            subject=entry.subject,
            bullets=(),
            task_refs=(entry.task_ref,),
            manifest_id=manifest_id,
            task_reference=task_reference,
        )
        for entry in manifest.entries
    )


def _commit(
    *,
    subject: str,
    bullets: tuple[str, ...],
    task_refs: tuple[TaskUrn, ...],
    manifest_id: str,
    task_reference: TaskReference,
) -> DeliveryCommit:
    """Build one delivery commit under the configured Task reference."""
    in_subject = task_reference == "subject"
    rendered = _subject_with_reference(subject, task_refs) if in_subject else subject
    return DeliveryCommit(
        subject=rendered,
        bullets=bullets,
        task_refs=task_refs,
        manifest_id=manifest_id,
        trailers=_trailers(task_refs, manifest_id=manifest_id, task_reference=task_reference),
    )


def integration_policy_digest(*, unit: IntegrationCommitUnit, task_reference: TaskReference) -> str:
    """Return the digest of the rules one generation was integrated under.

    Args:
        unit: The configured commit unit.
        task_reference: The configured Task reference placement.

    Returns:
        The ``sha256:`` digest of the policy version, the order key and
        the two configured leaves, so a generation taken under other
        rules cannot claim this one's digest.
    """
    return canonical_digest(
        {
            "version": INTEGRATION_POLICY_VERSION,
            "order_key_fields": list(ORDER_KEY_FIELDS),
            "integration_commit_unit": unit,
            "task_reference": task_reference,
        }
    )


__all__ = [
    "INTEGRATION_POLICY_VERSION",
    "MANIFEST_ID_WIDTH",
    "MAX_SUBJECT_LENGTH",
    "PROVENANCE_SCHEME",
    "PROVENANCE_TRAILER_KEY",
    "TASK_TRAILER_KEY",
    "CommitSubject",
    "DeliveryCommit",
    "DeliveryEntry",
    "DeliveryManifest",
    "ManifestId",
    "TrailerLine",
    "build_manifest",
    "integration_policy_digest",
    "render_delivery_commits",
]
