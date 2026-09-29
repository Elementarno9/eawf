"""Which record a decision overlay or an Enter-opened card is bound to, if any.

An overlay or card is bound to the one record its subject names, read from what the
console holds: the decision records that arrive beside the projection, or the route's own
read model where the projection carries the record (the Milestone's sealed bundle, the
Release's readiness). A bound overlay draws that record and nothing else; an unbound one
falls back to the prototype registers the golden contract replays, or to the chassis'
unheld frame. A card whose subject names no record while the records are held is
*absent*: it states that absence under the id it was asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.spine import SpineView
from eawf.surfaces.tui.console.decisions import (
    ArtifactRecord,
    ClaimRecord,
    DecisionRecords,
    DraftRecord,
    DraftState,
    MarkerRecord,
    PauseRecord,
    QuestionRecord,
    RungRecord,
    StepRecord,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import TRUTH
from eawf.workflow.projection.acceptance import AcceptanceBundleView, ReleaseReadinessView

#: The overlays that are cards: Enter-opened, about one record captured at open.
CARD_OVERLAYS: Final = frozenset({"draft", "marker"})
#: The sub-surface routes that are cards, each about one record its subject names.
CARD_ROUTES: Final = frozenset({"campaign.artifact", "evidence.digest", "campaign.step"})


@dataclass(frozen=True, slots=True)
class Rung:
    """One rung of a claim, which the rung card is bound to with the claim it belongs to.

    Attributes:
        claim: The claim.
        rung: The rung of it.
    """

    claim: ClaimRecord
    rung: RungRecord


#: What a card route is bound to: an artifact, a claim's rung, or a campaign step.
type CardRecord = ArtifactRecord | Rung | StepRecord

type Bound = (
    QuestionRecord
    | PauseRecord
    | ClaimRecord
    | AcceptanceBundleView
    | ReleaseReadinessView
    | DraftRecord
    | MarkerRecord
    | ArtifactRecord
    | Rung
    | StepRecord
)


def bound_overlay(
    name: str | None,
    session: Session,
    projection: SpineView | RouteReadModel | None,
    decisions: DecisionRecords | None,
) -> Bound | None:
    """Return the record overlay ``name`` is bound to, or ``None`` when none is held.

    Args:
        name: The open overlay.
        session: The session, whose captured subject names the record.
        projection: The route's read model, which carries a Milestone's bundle and a
            Release's readiness.
        decisions: The records held beside the projection.
    """
    if name == "acceptance" and isinstance(projection, AcceptanceBundleView):
        return projection
    if name == "readiness" and isinstance(projection, ReleaseReadinessView):
        return projection
    subject = session.ov_subject
    if name == "draft" and isinstance(projection, SpineView):
        held = decisions.draft(subject) if decisions is not None else None
        return held or draft_of(projection, subject)
    if decisions is None:
        return None
    if name == "question":
        return decisions.question(subject)
    if name == "pause":
        return decisions.pause(subject)
    if name == "evidence":
        return decisions.claim(subject)
    if name == "draft":
        return decisions.draft(subject)
    if name == "marker":
        return decisions.marker(subject)
    return None


def draft_of(spine: SpineView, key: str | None) -> DraftRecord | None:
    """Return the Backlog row ``key`` names as the record its draft card draws.

    The Task register states the row's title, its status, the scope it is due in, how
    many criteria it holds and the Batch it is filed under; it states no owner, so the
    card shows that field unanswered rather than inventing one.

    Returns:
        The record, or ``None`` when the register holds no draft or deferred row ``key``.
    """
    found = spine.index_of(key)
    if found is None:
        return None
    row = spine.rows[found]
    status = row.field("status").value
    if status not in {state.value for state in DraftState}:
        return None
    facts = row.facts
    criteria = facts.get("criteria")
    return DraftRecord(
        id=row.key,
        title=row.title or row.key,
        state=DraftState(status),
        due_scope=facts.get("due") or f"{TRUTH['unavailable'].unicode} no due scope is stated",
        due="undated",
        criteria=f"{criteria} stated" if criteria else None,
        batch=row.parent_key,
    )


def bound_card(session: Session, decisions: DecisionRecords | None) -> CardRecord | None:
    """Return the record the session's card route is bound to, or ``None``.

    The artifact card's subject is the artifact's key, the rung card's the claim with the
    session's rung, and the step card's the campaign with the session's step.
    """
    if decisions is None or session.overlay is not None:
        return None
    route, subject = session.route, session.subj_id
    if route == "campaign.artifact":
        return decisions.artifact(subject)
    if route == "evidence.digest":
        claim = decisions.claim(subject)
        rung = claim.rung(session.rung + 1) if claim is not None else None
        return Rung(claim, rung) if claim is not None and rung is not None else None
    if route == "campaign.step":
        return decisions.step(subject, session.cam_step + 1)
    return None


def card_absent(session: Session, decisions: DecisionRecords | None) -> str | None:
    """Return the id an open card was asked for when the held records have none for it.

    Returns:
        The id, qualified by its rung or step where the card names one; ``None`` when the
        open surface is no card, the records are not held, or the record exists.
    """
    if decisions is None:
        return None
    overlay = session.overlay
    if overlay in CARD_OVERLAYS:
        found = bound_overlay(overlay, session, None, decisions)
        return None if found is not None else session.ov_subject or "∅ no id"
    if overlay is None and session.route in CARD_ROUTES:
        if bound_card(session, decisions) is not None:
            return None
        subject = session.subj_id or "∅ no id"
        if session.route == "evidence.digest":
            return f"{subject} · rung {session.rung + 1}"
        if session.route == "campaign.step":
            return f"{subject} · step {session.cam_step + 1}"
        return subject
    return None
