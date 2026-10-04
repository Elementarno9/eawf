"""The acceptance read models: the Milestone, the Release, one receipt and an export.

Four routes answer what was accepted, what a candidate still owes, what one proof
recorded, and what a report of any of them would carry. Each draws the rows a projection
carried at one committed cursor, and each takes beside that projection the process
records the document does not hold: the sealed bundle an approval was given to, the
approval itself, and the proof receipts a card opens.

Three rules shape the models.

The approval names an exact head. :class:`ApprovalBinding` carries the digest the
approval was given to and the commit and tree that digest was sealed over, so the frame
can say which bytes were approved rather than that the Milestone was. An approval whose
digest is not the held bundle's own is not shown as this bundle's approval: it approved
different bytes, and drawing it here would let a later revision inherit an earlier
consent.

A readiness signal is stated or it is silent. Acceptance and the approval are read off
the records in hand; the release-candidate gates have no producer at this checkpoint, so
those signals carry the unknown truth token naming why instead of a cell an operator
would read as passing.

An opened receipt is immutable. :class:`ReceiptCard` is frozen and is built by copying
out of a frozen :class:`~eawf.kernel.delivery.receipts.ProofReceipt`, so a card holds no
reference through which the record behind it could be edited, and reading one twice at
one cursor yields the same bytes.

Nothing here reads the workspace, a lock or a clock. The inputs are one validated
:class:`~eawf.kernel.projection.compute.RouteProjection` and already-validated records,
so every view is a pure function of both, and :func:`export_report` is a pure function of
a view.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.delivery.acceptance import EvidenceRow, MilestoneAcceptanceBundle
from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    invalidated_field,
    known_field,
    status_and,
    unknown_field,
    unstated,
)
from eawf.kernel.projection.truth import TruthField, TruthState
from eawf.kernel.state.epoch2.base import NonEmptyStr
from eawf.kernel.state.epoch2.milestone import AcceptanceStep
from eawf.kernel.state.epoch2.values import ExactRevisionBinding
from eawf.observability.reflect.run_report import DEFAULT_PARTS, ReportPartName
from eawf.workflow.delivery.acceptance import AcceptanceApproval

logger = logging.getLogger(__name__)

#: The family name a refusal from this module names.
FAMILY: Final = "acceptance"

#: The route that renders one Milestone's acceptance bundle.
MILESTONE_ROUTE: Final = "milestone"

#: The route that renders a candidate's membership over its readiness.
RELEASE_ROUTE: Final = "release"

#: The route that opens one proof receipt as a card.
RECEIPT_ROUTE: Final = "receipt"

#: The route that renders what a report of the open view would carry.
EXPORT_ROUTE: Final = "export"

#: The console routes this module states a read model for. Every one is bound by
#: :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`, so every one is served by
#: ``projection.<route>.read`` and by ``projection.<route>.reconnect``.
ACCEPTANCE_ROUTES: Final[tuple[str, ...]] = (
    MILESTONE_ROUTE,
    RELEASE_ROUTE,
    RECEIPT_ROUTE,
    EXPORT_ROUTE,
)

#: The verb one Milestone's sealed bundle and bound approval are read through. The
#: Milestone frame is about one record, so its process records are read per subject
#: rather than carried for the whole console.
MILESTONE_ACCEPTANCE_METHOD: Final = "projection.milestone.acceptance"

#: Why a readiness signal of a release candidate is silent. The gates that would state
#: it observe a published candidate, and nothing in this tree has published one.
RC_GATE_REASON: Final = "nothing in this tree checks a release-candidate gate yet"

#: What a readiness signal says when the records in hand state it.
READINESS_MET: Final = "met"

#: What a readiness signal says when the records in hand state it is not met.
READINESS_UNMET: Final = "not met"

#: The signal naming whether the acceptance journey is complete.
ACCEPTANCE_SIGNAL: Final = "acceptance"

#: The signal naming whether an approval binds the candidate's own bytes.
APPROVAL_SIGNAL: Final = "approval"

#: The readiness signals the records in hand state, in render order.
STATED_SIGNALS: Final[tuple[str, ...]] = (ACCEPTANCE_SIGNAL, APPROVAL_SIGNAL)

#: The readiness signals a release candidate owes and nothing in this tree observes.
GATE_SIGNALS: Final[tuple[str, ...]] = ("policy gate", "artifact build")

#: The signal names a candidate's readiness is stated over, in render order.
READINESS_SIGNALS: Final[tuple[str, ...]] = (*STATED_SIGNALS, *GATE_SIGNALS)

#: The size cell of the one report part policy keeps out of every export. It is a reason
#: rather than a number, because a count of what is never carried would be misread.
REDACTED_SIZE: Final = "redacted by policy"

#: The size cell of a part the Run report counts off the Run's event lines and captured
#: runtime as it is taken. The export route reads the Run's record and not those, so the
#: card states no number it does not hold.
TAKEN_SIZE: Final = "? counted when taken"

#: The size cell of a part the report carries only when ``--parts`` names it.
UNASKED_SIZE: Final = "not asked"

#: Why the Run report carries each of its parts, in the order ``eawf run report`` writes
#: them. Secrets are the one part no request and no permission can include.
REPORT_PARTS: Final[Mapping[ReportPartName, str]] = MappingProxyType(
    {
        ReportPartName.TIMELINE: "every event the Run recorded, in order; a purged range "
        "reads as purged",
        ReportPartName.USAGE_AND_COST: "what the Run's captured runtime states, one counter "
        "per line",
        ReportPartName.TRANSCRIPT: "what the runner said, one line per event, scrubbed",
        ReportPartName.SECRETS: (  # pragma: allowlist secret
            "policy redacts these; no report of any Run can carry them"
        ),
        ReportPartName.SANDBOX_DECISIONS: "left out unless --parts names it; every "
        "authorisation the gateway decided for the Run's calls, allowed and denied",
    }
)

#: What each acceptance route renders per row, in column order. The first field of every
#: route is the status the document states. The bundle, the approval, the receipts and
#: the report parts are process records rather than document columns, so the columns that
#: would come from them are declared silent rather than drawn from a guess.
ACCEPTANCE_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        MILESTONE_ROUTE: status_and(unstated("acceptance"), unstated("evidence")),
        RELEASE_ROUTE: status_and(unstated("track"), unstated("accepted")),
        RECEIPT_ROUTE: status_and(unstated("result"), unstated("kept")),
        EXPORT_ROUTE: status_and(unstated("included"), unstated("size")),
    }
)


check_field_tables(family=FAMILY, routes=ACCEPTANCE_ROUTES, fields=ACCEPTANCE_FIELDS)


class MilestoneAcceptanceRecord(BaseModel):
    """What the daemon holds of one Milestone's acceptance, as one read answers it.

    Attributes:
        milestone_key: The Milestone the answer is about.
        bundle: The sealed bundle the Milestone was accepted at, else its latest sealed
            revision; ``None`` when none was ever sealed.
        approval: The sealed approval given to that bundle's own digest; ``None`` when
            nobody approved those bytes.
        journey: The acceptance journey the Milestone record declares, in step order.
        evidence: The evidence rows the store holds for the keys the bundle cites.
        waiting_approval_urn: The acceptance question asked of the Milestone and not yet
            answered; ``None`` when none waits.
        accepted_binding: The exact revision the Milestone record states it was accepted
            at; ``None`` until it is accepted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    milestone_key: NonEmptyStr
    bundle: MilestoneAcceptanceBundle | None = None
    approval: AcceptanceApproval | None = None
    journey: tuple[AcceptanceStep, ...] = ()
    evidence: tuple[EvidenceRow, ...] = ()
    waiting_approval_urn: NonEmptyStr | None = None
    accepted_binding: ExactRevisionBinding | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalBinding:
    """The exact bytes, and the exact head, one approval was given to.

    Attributes:
        milestone_key: The Milestone the approval was given for.
        bundle_revision: The bundle revision the approval names.
        approved_digest: The bundle digest the approval binds to.
        head_sha: The commit the approved bundle was sealed over.
        tree_sha: The tree that commit carries, which is the content identity.
        resolved_by: The immutable key of the person who approved.
        approved_at: When the approval was given.
    """

    milestone_key: str
    bundle_revision: int
    approved_digest: str
    head_sha: str
    tree_sha: str
    resolved_by: str
    approved_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CriterionRow:
    """One step of the acceptance journey, as the Milestone frame draws it.

    Attributes:
        step_id: The journey step the outcome is filed under.
        passed: Whether the step passed.
        observation: What the step actually showed.
        evidence_keys: The ``EVD-####`` keys the step cites, in record order.
        evidence_kinds: The kinds of evidence the step shows, in record order.
    """

    step_id: str
    passed: bool
    observation: str
    evidence_keys: tuple[str, ...]
    evidence_kinds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class JourneyStepRow:
    """One declared step of the acceptance journey beside what the sealed bundle found.

    Attributes:
        step_id: The step's ``AS-##`` id.
        actor: Who performs the step, the operator or the system.
        action: What the actor does, as the record states it.
        expected_observation: What the step must show to pass.
        evidence_kinds: The kinds of evidence the step asks for, in record order.
        required: Whether the step gates acceptance.
        outcome: What the held bundle recorded for the step; ``None`` when no bundle is
            held or it records nothing for this step.
        evidence: The cited evidence rows the store holds, in citation order.
        unheld_keys: The cited ``EVD-####`` keys the store holds no row for.
    """

    step_id: str
    actor: str
    action: str
    expected_observation: str
    evidence_kinds: tuple[str, ...]
    required: bool
    outcome: CriterionRow | None
    evidence: tuple[EvidenceRow, ...]
    unheld_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadinessSignal:
    """One thing a candidate owes, and whether anything states it is met.

    Attributes:
        name: The signal's console name.
        state: Whether it is met, as a truth field, so a signal nothing observes renders
            the unknown token naming why rather than an unmet cell.
        evidence: What the answer rests on; empty for a signal nothing states.
    """

    name: str
    state: TruthField[str]
    evidence: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptCard:
    """One proof receipt, copied out of the record so the card cannot reach it.

    Attributes:
        key: The receipt's ``RCP-######`` key.
        gate_id: The gate the receipt records one run of.
        criterion_ids: The criteria that run answered, in record order.
        result: The terminal result the run reached.
        started_at: When the run started.
        ended_at: When it ended.
        freshness_key: The digest of the inputs the receipt is bound to.
        head_sha: The commit those inputs were taken against.
        supersedes_key: The receipt this one replaces, or ``None``.
    """

    key: str
    gate_id: str
    criterion_ids: tuple[str, ...]
    result: str
    started_at: datetime
    ended_at: datetime
    freshness_key: str
    head_sha: str
    supersedes_key: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportPart:
    """One part of a report, whether it is carried and what it costs.

    Attributes:
        name: The part's console name.
        included: Whether a report carries it.
        size: How much of it there is, taken off the view rather than estimated.
        why: Why it is carried or left out.
    """

    name: str
    included: bool
    size: str
    why: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceBundleView(RouteReadModel):
    """The Milestone's read model: its rows, the sealed bundle, and the approval."""

    bundle_revision: int | None = None
    bundle_digest: str | None = None
    sealed_at: datetime | None = None
    criteria: tuple[CriterionRow, ...] = ()
    approval: ApprovalBinding | None = None
    journey: tuple[JourneyStepRow, ...] = ()
    supersedes_revision: int | None = None
    repair_reason: str | None = None
    waiting_approval_urn: str | None = None
    accepted_binding: ExactRevisionBinding | None = None

    def blocking(self) -> tuple[CriterionRow, ...]:
        """Return the criteria that did not pass, in render order."""
        return tuple(row for row in self.criteria if not row.passed)

    def proven(self) -> int:
        """Return how many criteria passed."""
        return sum(1 for row in self.criteria if row.passed)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseReadinessView(RouteReadModel):
    """The Release's read model: its membership rows, its signals, and the approval."""

    signals: tuple[ReadinessSignal, ...] = ()
    approval: ApprovalBinding | None = None

    def signal(self, name: str) -> ReadinessSignal | None:
        """Return the named signal, or ``None`` when the candidate states none."""
        return next((item for item in self.signals if item.name == name), None)

    def unmet(self) -> tuple[ReadinessSignal, ...]:
        """Return every signal nothing has stated as met, in render order."""
        return tuple(
            item
            for item in self.signals
            if item.state.state is not TruthState.KNOWN or item.state.value != READINESS_MET
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptCardView(RouteReadModel):
    """The receipt route's read model: the cards the held receipts open as."""

    cards: tuple[ReceiptCard, ...] = ()

    def card(self, key: str | None) -> ReceiptCard | None:
        """Return the card ``key`` opens, or ``None`` when no receipt is held for it."""
        if key is None:
            return None
        return next((item for item in self.cards if item.key == key), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunReportPlanView(RouteReadModel):
    """The export route's read model: what a report of the open view would carry."""

    parts: tuple[ReportPart, ...] = ()

    def included(self) -> tuple[ReportPart, ...]:
        """Return the parts a report carries, in render order."""
        return tuple(part for part in self.parts if part.included)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportReport:
    """One rendered report, and the digest of the view it was taken at.

    Attributes:
        digest: The projection digest the view was read through. Two exports of one
            cursor carry one digest, which is what makes them comparable.
        source_cursor: The committed ordinal the rows were read at.
        lines: The report, one fact per line.
    """

    digest: str
    source_cursor: str
    lines: tuple[str, ...]


def approval_binding(approval: AcceptanceApproval) -> ApprovalBinding:
    """Return the exact bytes and head one approval was given to.

    Args:
        approval: The sealed approval, already validated.

    Returns:
        The binding the frame names, with the commit and tree the approved digest was
        sealed over.
    """
    binding = approval.accepted_binding
    return ApprovalBinding(
        milestone_key=approval.milestone_ref.entity_key,
        bundle_revision=approval.bundle_revision,
        approved_digest=approval.approved_digest,
        head_sha=binding.head_sha,
        tree_sha=binding.tree_sha,
        resolved_by=approval.resolved_by.principal_id,
        approved_at=approval.approved_at,
    )


def criteria_rows(bundle: MilestoneAcceptanceBundle) -> tuple[CriterionRow, ...]:
    """Return one row per journey step of ``bundle``, in the bundle's own order."""
    return tuple(
        CriterionRow(
            step_id=step.step_id,
            passed=step.passed,
            observation=step.observation,
            evidence_keys=tuple(ref.entity_key for ref in step.evidence_refs),
            evidence_kinds=tuple(step.evidence_kinds),
        )
        for step in bundle.steps
    )


def journey_rows(
    journey: Sequence[AcceptanceStep],
    criteria: Sequence[CriterionRow],
    evidence: Sequence[EvidenceRow],
) -> tuple[JourneyStepRow, ...]:
    """Return each declared step beside its sealed outcome and the evidence it cites.

    Args:
        journey: The steps the Milestone record declares, in step order.
        criteria: The outcomes the held bundle recorded, one per step it covers.
        evidence: The evidence rows the store holds.

    Returns:
        One row per declared step, in the record's order.
    """
    outcomes = {row.step_id: row for row in criteria}
    held = {row.id: row for row in evidence}
    rows: list[JourneyStepRow] = []
    for step in journey:
        outcome = outcomes.get(step.step_id)
        cited = outcome.evidence_keys if outcome is not None else ()
        rows.append(
            JourneyStepRow(
                step_id=step.step_id,
                actor=step.actor,
                action=step.action,
                expected_observation=step.expected_observation,
                evidence_kinds=tuple(step.evidence_kinds),
                required=step.required,
                outcome=outcome,
                evidence=tuple(held[key] for key in cited if key in held),
                unheld_keys=tuple(key for key in cited if key not in held),
            )
        )
    return tuple(rows)


def _anchor(model: RouteReadModel) -> tuple[str, int]:
    """Return what a derived signal of ``model`` rests on, and the revision it was read at.

    A signal is derived over the whole read model rather than over one record, so it
    rests on the first row the route rendered when there is one. A route whose registers
    are empty has no record to name, so the projection's own digest stands in: it
    addresses the exact rows the signal was derived from, which a blank would not.
    """
    if model.rows:
        return model.rows[0].urn, model.rows[0].revision
    return model.digest, 1


def _signal(
    *, name: str, model: RouteReadModel, value: str | None, evidence: str, reason: str
) -> ReadinessSignal:
    """Return one readiness signal, stated when ``value`` is known and silent otherwise."""
    urn, revision = _anchor(model)
    state = (
        known_field(value=value, urn=urn, revision=revision)
        if value is not None
        else unknown_field(urn=urn, revision=revision, reason=reason)
    )
    return ReadinessSignal(name=name, state=state, evidence=evidence)


def _approval_signal(
    model: RouteReadModel,
    *,
    bundle: MilestoneAcceptanceBundle | None,
    approval: ApprovalBinding | None,
    superseded: ApprovalBinding | None,
) -> ReadinessSignal:
    """Return the approval signal: met, voided by a bundle it was not given to, or not held."""
    if approval is None and superseded is not None and bundle is not None:
        urn, revision = _anchor(model)
        reason = (
            f"the approval bound digest {superseded.approved_digest}; the bundle held now is "
            f"revision {bundle.revision} at digest {bundle.digest()}"
        )
        return ReadinessSignal(
            name=APPROVAL_SIGNAL,
            state=invalidated_field(urn=urn, revision=revision, reason=reason),
            evidence=f"head {superseded.head_sha}",
        )
    return _signal(
        name=APPROVAL_SIGNAL,
        model=model,
        value=None if approval is None else READINESS_MET,
        evidence="" if approval is None else f"head {approval.head_sha}",
        reason="no approval is held for this candidate",
    )


def readiness_signals(
    model: RouteReadModel,
    *,
    bundle: MilestoneAcceptanceBundle | None,
    approval: ApprovalBinding | None,
    superseded: ApprovalBinding | None = None,
) -> tuple[ReadinessSignal, ...]:
    """Return the candidate's readiness, stated where the records in hand state it.

    Args:
        model: The release route's read model, whose rows the signals rest on.
        bundle: The sealed bundle of the Milestone the candidate is held on, when one is
            held; its blocking steps are what acceptance is not complete on.
        approval: The approval given to that bundle, when one is held.
        superseded: An approval held for the Milestone whose digest the held bundle does
            not carry; the approval signal then says it was voided.

    Returns:
        One signal per name in :data:`READINESS_SIGNALS`, in that order. Acceptance and
        the approval are stated from the records; the candidate gates carry the unknown
        token naming why, because nothing in this tree observes them.
    """
    blocking = bundle.blocking_step_ids if bundle is not None else ()
    acceptance = None if bundle is None else (READINESS_MET if not blocking else READINESS_UNMET)
    signals = (
        _signal(
            name=ACCEPTANCE_SIGNAL,
            model=model,
            value=acceptance,
            evidence="" if bundle is None else f"revision {bundle.revision}",
            reason="no acceptance bundle is held for this candidate",
        ),
        _approval_signal(model, bundle=bundle, approval=approval, superseded=superseded),
        *(
            _signal(name=name, model=model, value=None, evidence="", reason=RC_GATE_REASON)
            for name in GATE_SIGNALS
        ),
    )
    logger.debug(f"readiness_signals signals={len(signals)} blocking={len(blocking)}")
    return signals


def receipt_cards(receipts: Sequence[ProofReceipt]) -> tuple[ReceiptCard, ...]:
    """Return one immutable card per held receipt, in record order.

    Every value is copied out of the record, so the card holds no reference a later
    reader could reach the receipt through; a corrected proof is a new receipt that
    supersedes this one by reference and never a rewrite of it.
    """
    return tuple(
        ReceiptCard(
            key=receipt.id,
            gate_id=receipt.gate_id,
            criterion_ids=tuple(receipt.criterion_ids),
            result=receipt.result.value,
            started_at=receipt.started_at,
            ended_at=receipt.ended_at,
            freshness_key=receipt.freshness_key,
            head_sha=receipt.freshness.revision_binding.head_sha,
            supersedes_key=receipt.supersedes_id,
        )
        for receipt in receipts
    )


def report_parts() -> tuple[ReportPart, ...]:
    """Return the parts the Run report carries, planned as ``eawf run report`` plans them.

    A part is included when the report carries it by default; secrets never are. A size
    the report counts off the Run's event lines is not guessed here, because the export
    route reads the Run's record and not its lines.
    """
    parts: list[ReportPart] = []
    for name, why in REPORT_PARTS.items():
        included = name in DEFAULT_PARTS
        if name is ReportPartName.SECRETS:
            size = REDACTED_SIZE
        else:
            size = TAKEN_SIZE if included else UNASKED_SIZE
        parts.append(ReportPart(name=name.value, included=included, size=size, why=why))
    return tuple(parts)


def export_report(model: RouteReadModel) -> ExportReport:
    """Return the report of one read model, at the digest it was read through.

    The report is a pure function of the view: it opens nothing, writes nothing into the
    tree and allocates no ordinal, so taking one leaves the workspace exactly as it was.
    Two reports of one cursor are byte-identical and carry the same digest, which is what
    lets a terminal and a plain-text reader compare what they were shown.

    Args:
        model: The read model to report, of any acceptance route.

    Returns:
        The rendered report and the digest of the view it was taken at.
    """
    lines = [
        f"route {model.route}",
        f"scope {model.scope_id}",
        f"cursor {model.source_cursor}",
        f"digest {model.digest}",
        f"complete {'yes' if model.complete else 'no'}",
    ]
    lines += [f"register {name} {count}" for name, count in model.counts.items()]
    for row in model.rows:
        status = row.field("status")
        stated = status.value if status.state is TruthState.KNOWN and status.value else "unknown"
        lines.append(f"row {row.key} {row.collection.value} {stated}")
    lines += [f"unstated {spec.name} {spec.missing_reason()}" for spec in model.unproduced()]
    logger.debug(f"export_report route={model.route} lines={len(lines)}")
    return ExportReport(digest=model.digest, source_cursor=model.source_cursor, lines=tuple(lines))


def _as[T: RouteReadModel](model: RouteReadModel, kind: type[T], **extra: Any) -> T:
    """Return ``model``'s own columns under ``kind``, with the family's records added."""
    return kind(
        route=model.route,
        read_model=model.read_model,
        scope_id=model.scope_id,
        source_cursor=model.source_cursor,
        digest=model.digest,
        complete=model.complete,
        rows=model.rows,
        counts=model.counts,
        specs=model.specs,
        **extra,
    )


def _approval_for(
    bundle: MilestoneAcceptanceBundle | None, approval: ApprovalBinding | None
) -> ApprovalBinding | None:
    """Return the approval that binds ``bundle``'s own bytes, and never another's.

    An approval names the digest it was given to. A held bundle whose digest is not that
    one was sealed after the consent, so the consent is not shown against it: inheriting
    it would be the console saying a later revision was approved. With no bundle in hand
    there is nothing to disagree with, so the approval stands as the record states it.
    """
    if approval is None:
        return None
    if bundle is None:
        return approval
    return approval if approval.approved_digest == bundle.digest() else None


def build_acceptance_view(
    projection: RouteProjection,
    *,
    bundle: MilestoneAcceptanceBundle | None = None,
    approval: AcceptanceApproval | None = None,
    receipts: Sequence[ProofReceipt] = (),
    journey: Sequence[AcceptanceStep] = (),
    evidence: Sequence[EvidenceRow] = (),
    waiting_approval_urn: str | None = None,
    accepted_binding: ExactRevisionBinding | None = None,
) -> RouteReadModel:
    """Return the read model one acceptance route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        bundle: The sealed acceptance bundle the Milestone frame draws, when one is held.
        approval: The approval given to a bundle digest, when one is held. It is shown
            only against the bundle whose digest it names.
        receipts: The proof receipts a card may open, in record order.
        journey: The acceptance journey the Milestone record declares.
        evidence: The evidence rows the store holds for the bundle's citations.
        waiting_approval_urn: The acceptance question asked and not yet answered.
        accepted_binding: The exact revision the Milestone states it was accepted at.

    Returns:
        An :class:`AcceptanceBundleView` for the Milestone, a
        :class:`ReleaseReadinessView` for the Release, a :class:`ReceiptCardView` for the
        receipt card and a :class:`RunReportPlanView` for the export card, each with an
        empty record run when the caller holds none.

    Raises:
        ValueError: The projection is for a route this module states no read model for.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=ACCEPTANCE_FIELDS)
    given = None if approval is None else approval_binding(approval)
    bound = _approval_for(bundle, given)
    if projection.route == MILESTONE_ROUTE:
        criteria = () if bundle is None else criteria_rows(bundle)
        reason = None if bundle is None or bundle.repair_reason is None else bundle.repair_reason
        return _as(
            model,
            AcceptanceBundleView,
            bundle_revision=None if bundle is None else bundle.revision,
            bundle_digest=None if bundle is None else bundle.digest(),
            sealed_at=None if bundle is None else bundle.sealed_at,
            criteria=criteria,
            approval=bound,
            journey=journey_rows(journey, criteria, evidence),
            supersedes_revision=None if bundle is None else bundle.supersedes_revision,
            repair_reason=None if reason is None else reason.message,
            waiting_approval_urn=waiting_approval_urn,
            accepted_binding=accepted_binding,
        )
    if projection.route == RELEASE_ROUTE:
        return _as(
            model,
            ReleaseReadinessView,
            signals=readiness_signals(
                model, bundle=bundle, approval=bound, superseded=given if bound is None else None
            ),
            approval=bound,
        )
    if projection.route == RECEIPT_ROUTE:
        return _as(model, ReceiptCardView, cards=receipt_cards(receipts))
    return _as(model, RunReportPlanView, parts=report_parts())


__all__ = [
    "ACCEPTANCE_FIELDS",
    "ACCEPTANCE_ROUTES",
    "ACCEPTANCE_SIGNAL",
    "APPROVAL_SIGNAL",
    "EXPORT_ROUTE",
    "FAMILY",
    "GATE_SIGNALS",
    "MILESTONE_ACCEPTANCE_METHOD",
    "MILESTONE_ROUTE",
    "RC_GATE_REASON",
    "READINESS_MET",
    "READINESS_SIGNALS",
    "READINESS_UNMET",
    "RECEIPT_ROUTE",
    "REDACTED_SIZE",
    "RELEASE_ROUTE",
    "REPORT_PARTS",
    "STATED_SIGNALS",
    "AcceptanceBundleView",
    "ApprovalBinding",
    "CriterionRow",
    "ExportReport",
    "JourneyStepRow",
    "MilestoneAcceptanceRecord",
    "ReadinessSignal",
    "ReceiptCard",
    "ReceiptCardView",
    "ReleaseReadinessView",
    "ReportPart",
    "RunReportPlanView",
    "approval_binding",
    "build_acceptance_view",
    "criteria_rows",
    "export_report",
    "journey_rows",
    "readiness_signals",
    "receipt_cards",
    "report_parts",
]
