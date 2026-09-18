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

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    known_field,
    status_and,
    unknown_field,
    unstated,
)
from eawf.kernel.projection.truth import TruthField, TruthState
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

#: Why a readiness signal of a release candidate is silent. The gates that would state
#: it observe a published candidate, and nothing in this tree has published one.
RC_GATE_REASON: Final = "no producer observes a release-candidate gate in this tree yet"

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

#: The parts a report carries, in render order, each with whether it is included and why.
#: Secrets are the one part no cursor and no permission can include.
REPORT_PARTS: Final[tuple[tuple[str, bool, str], ...]] = (
    ("rows", True, "every record the route rendered at this cursor, in render order"),
    ("counts", True, "the count of each register the route binds, derived from the rows"),
    ("unstated", True, "the declared columns no producer states, so the gaps are readable"),
    ("secrets", False, "policy redacts these; no export of any view can carry them"),
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
    """

    step_id: str
    passed: bool
    observation: str
    evidence_keys: tuple[str, ...]


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
        )
        for step in bundle.steps
    )


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


def readiness_signals(
    model: RouteReadModel,
    *,
    bundle: MilestoneAcceptanceBundle | None,
    approval: ApprovalBinding | None,
) -> tuple[ReadinessSignal, ...]:
    """Return the candidate's readiness, stated where the records in hand state it.

    Args:
        model: The release route's read model, whose rows the signals rest on.
        bundle: The sealed bundle of the Milestone the candidate is held on, when one is
            held; its blocking steps are what acceptance is not complete on.
        approval: The approval given to that bundle, when one is held.

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
        _signal(
            name=APPROVAL_SIGNAL,
            model=model,
            value=None if approval is None else READINESS_MET,
            evidence="" if approval is None else f"head {approval.head_sha}",
            reason="no approval is held for this candidate",
        ),
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


def _part_size(name: str, model: RouteReadModel) -> str:
    """Return how much of one part there is, counted off ``model`` rather than estimated.

    A part nothing counts is the redacted one: policy keeps it out of every export, so
    its size is the reason it is absent rather than a number.
    """
    counted: Mapping[str, tuple[int, str]] = {
        "rows": (len(model.rows), "row"),
        "counts": (len(model.counts), "register"),
        "unstated": (len(model.unproduced()), "column"),
    }
    found = counted.get(name)
    if found is None:
        return REDACTED_SIZE
    total, unit = found
    return f"{total} {unit}" + ("" if total == 1 else "s")


def report_parts(model: RouteReadModel) -> tuple[ReportPart, ...]:
    """Return what a report of ``model`` would carry, sized off the view itself.

    A size is counted rather than estimated: the rows, the registers and the silent
    columns are all in hand, so the card states how much there is instead of promising
    a figure the export would have to discover.
    """
    return tuple(
        ReportPart(name=name, included=included, size=_part_size(name, model), why=why)
        for name, included, why in REPORT_PARTS
    )


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
) -> RouteReadModel:
    """Return the read model one acceptance route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        bundle: The sealed acceptance bundle the Milestone frame draws, when one is held.
        approval: The approval given to a bundle digest, when one is held. It is shown
            only against the bundle whose digest it names.
        receipts: The proof receipts a card may open, in record order.

    Returns:
        An :class:`AcceptanceBundleView` for the Milestone, a
        :class:`ReleaseReadinessView` for the Release, a :class:`ReceiptCardView` for the
        receipt card and a :class:`RunReportPlanView` for the export card, each with an
        empty record run when the caller holds none.

    Raises:
        ValueError: The projection is for a route this module states no read model for.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=ACCEPTANCE_FIELDS)
    bound = _approval_for(bundle, None if approval is None else approval_binding(approval))
    if projection.route == MILESTONE_ROUTE:
        return _as(
            model,
            AcceptanceBundleView,
            bundle_revision=None if bundle is None else bundle.revision,
            bundle_digest=None if bundle is None else bundle.digest(),
            sealed_at=None if bundle is None else bundle.sealed_at,
            criteria=() if bundle is None else criteria_rows(bundle),
            approval=bound,
        )
    if projection.route == RELEASE_ROUTE:
        return _as(
            model,
            ReleaseReadinessView,
            signals=readiness_signals(model, bundle=bundle, approval=bound),
            approval=bound,
        )
    if projection.route == RECEIPT_ROUTE:
        return _as(model, ReceiptCardView, cards=receipt_cards(receipts))
    return _as(model, RunReportPlanView, parts=report_parts(model))


__all__ = [
    "ACCEPTANCE_FIELDS",
    "ACCEPTANCE_ROUTES",
    "ACCEPTANCE_SIGNAL",
    "APPROVAL_SIGNAL",
    "EXPORT_ROUTE",
    "FAMILY",
    "GATE_SIGNALS",
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
    "readiness_signals",
    "receipt_cards",
    "report_parts",
]
