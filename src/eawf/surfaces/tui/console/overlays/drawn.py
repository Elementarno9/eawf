"""Draw an overlay or a card from the record it is bound to, when the console holds one.

The frame composer asks here first. A bound overlay or card draws its record; a card whose
id names no held record draws its absence under that id; anything else answers ``None``
and is drawn the way it was before a record was held.
"""

from __future__ import annotations

from eawf.surfaces.tui.console.decisions import (
    ArtifactRecord,
    ClaimRecord,
    DecisionRecords,
    DraftRecord,
    MarkerRecord,
    PauseRecord,
    QuestionRecord,
    StepRecord,
)
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.overlays.bound import (
    CARD_OVERLAYS,
    CARD_ROUTES,
    Rung,
    bound_card,
    bound_overlay,
    card_absent,
)
from eawf.surfaces.tui.console.overlays.cards import (
    absent_card,
    render_artifact,
    render_draft,
    render_marker,
    render_rung,
    render_step,
)
from eawf.surfaces.tui.console.overlays.decision import (
    render_acceptance,
    render_evidence,
    render_pause,
    render_question,
    render_readiness,
)
from eawf.workflow.projection.acceptance import AcceptanceBundleView, ReleaseReadinessView


def draw_overlay(name: str, view: View) -> list[str] | None:
    """Return overlay ``name`` drawn from its bound record, or ``None`` when it has none."""
    s, held = view.session, view.decisions or DecisionRecords()
    record = bound_overlay(name, s, view.projection, view.decisions)
    if isinstance(record, QuestionRecord):
        return render_question(view, record, held)
    if isinstance(record, PauseRecord):
        return render_pause(view, record, held)
    if isinstance(record, ClaimRecord):
        return render_evidence(view, record)
    if isinstance(record, AcceptanceBundleView):
        return render_acceptance(view, record)
    if isinstance(record, ReleaseReadinessView):
        return render_readiness(view, record)
    if isinstance(record, DraftRecord):
        return render_draft(view, record)
    if isinstance(record, MarkerRecord):
        return render_marker(view, record)
    asked = card_absent(s, view.decisions) if name in CARD_OVERLAYS else None
    return absent_card(view, asked) if asked is not None else None


def draw_card(view: View) -> list[str] | None:
    """Return the session's card route drawn from its bound record, or ``None``."""
    s = view.session
    if s.overlay is not None or s.route not in CARD_ROUTES:
        return None
    card = bound_card(s, view.decisions)
    if isinstance(card, ArtifactRecord):
        return render_artifact(view, card)
    if isinstance(card, Rung):
        return render_rung(view, card.claim, card.rung)
    if isinstance(card, StepRecord):
        return render_step(view, card)
    asked = card_absent(s, view.decisions)
    return absent_card(view, asked) if asked is not None else None
