"""Attention-feed reducer -- rank what needs the operator by urgency.

The Home mode's overview band answers one question: "what needs me, most
urgent first?". This module is the **pure** reducer behind it -- it folds
several state-resident attention sources onto one ranked list so the band
is a thin render over a typed, unit-testable value (no Textual mount
needed to assert the ranking).

Sources folded onto one :class:`~eawf.kernel.state.enums.Urgency` scale:

* open ``needs_user`` pauses -- the operator is blocking a paused skill;
  each carries its ``pause_urn`` + question so the row stays actionable
  (selecting it re-opens the pause's modal). Urgency is the pause's own.
* failed waves (:attr:`~eawf.kernel.state.enums.WaveStatus.FAILED`) -- a
  dispatched wave errored and needs a look; always ``URGENT``.
* open incidents (:attr:`~eawf.kernel.state.enums.IncidentStatus.OPEN`) --
  ranked by severity (critical -> urgent down to low -> low).
* blocking open questions
  (:attr:`~eawf.kernel.state.enums.OpenQuestionStatus.BLOCKED`) -- the only
  question kind the balanced-autonomy interrupt raises; carries its own
  urgency.
* ready-to-claim waves
  (:attr:`~eawf.kernel.state.enums.WaveStatus.PENDING` whose every ``deps``
  wave is CLOSED) -- the next move is unblocked; advisory ``NORMAL``.

The ranking reuses the same descending-:class:`Urgency` key the
needs_user inbox uses (:mod:`eawf.surfaces.tui.screens.overlays.needs_user_inbox`),
so a pause and an incident sort on one comparable scale. The sort is
stable, so items sharing a tier keep source order (pauses, then failed
waves, then incidents, then questions, then ready waves -- most-acute
source first).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from eawf.kernel.state.enums import (
    IncidentSeverity,
    IncidentStatus,
    OpenQuestionStatus,
    Urgency,
    WaveStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eawf.kernel.state.models import State
    from eawf.workflow.skills.bodies.user_question import UserQuestion
    from eawf.workflow.skills.needs_user import OpenPause

logger = logging.getLogger(__name__)

#: Honest-empty band text shown when nothing needs the operator.
EMPTY_FEED_TEXT: str = "nothing needs you"


class AttentionKind(StrEnum):
    """The source category of an :class:`AttentionItem`.

    Drives the per-row glyph / label the band renders and lets a caller
    filter the feed by source. The order of declaration is also the
    same-tier tiebreak weight :func:`build_attention_feed` applies (a
    needs_user pause outranks an incident at the same urgency).
    """

    NEEDS_USER = "needs_user"
    FAILED_WAVE = "failed_wave"
    INCIDENT = "incident"
    OPEN_QUESTION = "open_question"
    READY_WAVE = "ready_wave"


#: Severity -> urgency map for open incidents. Critical faults raise to the
#: operator now; a low-severity incident is advisory.
_INCIDENT_URGENCY: dict[IncidentSeverity, Urgency] = {
    IncidentSeverity.CRITICAL: Urgency.URGENT,
    IncidentSeverity.HIGH: Urgency.HIGH,
    IncidentSeverity.MEDIUM: Urgency.NORMAL,
    IncidentSeverity.LOW: Urgency.LOW,
}

#: Same-tier tiebreak weight per kind: a lower rank sorts first within a
#: shared urgency tier (needs_user before failed-wave before incident ...).
_KIND_ORDER: dict[AttentionKind, int] = {kind: index for index, kind in enumerate(AttentionKind)}


@dataclass(frozen=True)
class AttentionItem:
    """One ranked row in the attention feed.

    Attributes:
        urgency: Where the item sits on the shared
            :class:`~eawf.kernel.state.enums.Urgency` ladder -- the primary
            sort key (most-immediate first).
        kind: The source category (:class:`AttentionKind`); drives the row
            glyph and the same-tier tiebreak.
        title: Short scannable label for the row (e.g. the wave id + title,
            or the paused question text).
        detail: Secondary line / id shown muted after the title.
        pause_urn: For a :attr:`AttentionKind.NEEDS_USER` row, the pause the
            row re-opens when selected; ``None`` for every other kind (those
            rows are informational, not directly actionable from the band).
        question: The paused :class:`~eawf.workflow.skills.bodies.user_question.UserQuestion`
            for a needs_user row, passed straight to the modal on select;
            ``None`` otherwise.
    """

    urgency: Urgency
    kind: AttentionKind
    title: str
    detail: str
    pause_urn: str | None = None
    question: UserQuestion | None = None

    @property
    def actionable(self) -> bool:
        """``True`` when selecting the row opens something (a pause modal)."""
        return self.pause_urn is not None and self.question is not None


def _urgency_rank(urgency: Urgency) -> int:
    """Return a descending sort key for *urgency* (most-immediate first).

    Mirrors the needs_user inbox key so the two rankings stay identical:
    the :class:`~eawf.kernel.state.enums.Urgency` members run ascending from
    most-deferrable, so negating the declaration index sorts ``URGENT``
    before ``HIGH`` before ``NORMAL`` before ``LOW`` under a plain ascending
    ``sorted``.

    Args:
        urgency: The urgency to rank.

    Returns:
        A negative integer; lower (more negative) is more urgent.
    """
    return -list(Urgency).index(urgency)


def _pause_items(pauses: Iterable[OpenPause]) -> list[AttentionItem]:
    """Build needs_user attention rows from open pauses.

    Args:
        pauses: The open pauses (any order).

    Returns:
        One actionable :class:`AttentionItem` per pause, carrying the
        ``pause_urn`` + question so the band can re-open the modal.
    """
    items: list[AttentionItem] = []
    for pause in pauses:
        label = pause.session.rsplit(":", 1)[-1] if pause.session else pause.scope_id
        items.append(
            AttentionItem(
                urgency=pause.urgency,
                kind=AttentionKind.NEEDS_USER,
                title=pause.question.question,
                detail=label,
                pause_urn=pause.pause_urn,
                question=pause.question,
            )
        )
    return items


def _failed_wave_items(state: State) -> list[AttentionItem]:
    """Build URGENT rows for every failed wave in *state*."""
    items: list[AttentionItem] = []
    for wave in state.waves.values():
        if wave.status is WaveStatus.FAILED:
            items.append(
                AttentionItem(
                    urgency=Urgency.URGENT,
                    kind=AttentionKind.FAILED_WAVE,
                    title=f"{wave.id} {wave.title}",
                    detail="wave failed",
                )
            )
    return items


def _incident_items(state: State) -> list[AttentionItem]:
    """Build severity-ranked rows for every open incident in *state*."""
    items: list[AttentionItem] = []
    for incident in (state.incidents or {}).values():
        if incident.status is not IncidentStatus.OPEN:
            continue
        items.append(
            AttentionItem(
                urgency=_INCIDENT_URGENCY.get(incident.severity, Urgency.NORMAL),
                kind=AttentionKind.INCIDENT,
                title=f"{incident.id} {incident.title}",
                detail=f"incident {incident.severity.value}",
            )
        )
    return items


def _open_question_items(state: State) -> list[AttentionItem]:
    """Build rows for blocking open questions (the interrupt-worthy kind)."""
    items: list[AttentionItem] = []
    for question in (state.open_questions or {}).values():
        if question.status is not OpenQuestionStatus.BLOCKED:
            continue
        items.append(
            AttentionItem(
                urgency=question.urgency,
                kind=AttentionKind.OPEN_QUESTION,
                title=f"{question.id} {question.title}",
                detail="blocking question",
            )
        )
    return items


def _ready_wave_items(state: State) -> list[AttentionItem]:
    """Build advisory rows for PENDING waves whose deps are all CLOSED.

    A pending wave is *ready to claim* once every wave in its ``deps`` is
    CLOSED -- the next move is unblocked. A pending wave with an unmet dep
    is still waiting, so it is not surfaced (it is not yet actionable).

    Args:
        state: The bound state to scan.

    Returns:
        One ``NORMAL`` :class:`AttentionItem` per ready-to-claim wave.
    """
    items: list[AttentionItem] = []
    for wave in state.waves.values():
        if wave.status is not WaveStatus.PENDING:
            continue
        deps_met = all(
            state.waves[dep].status is WaveStatus.CLOSED for dep in wave.deps if dep in state.waves
        )
        if not deps_met:
            continue
        items.append(
            AttentionItem(
                urgency=Urgency.NORMAL,
                kind=AttentionKind.READY_WAVE,
                title=f"{wave.id} {wave.title}",
                detail="ready to claim",
            )
        )
    return items


def build_attention_feed(
    state: State | None,
    pauses: Iterable[OpenPause] = (),
) -> tuple[AttentionItem, ...]:
    """Rank the operator's open attention items, most-urgent first.

    Folds the needs_user pauses plus the state-resident attention signals
    (failed waves, open incidents, blocking questions, ready-to-claim
    waves) onto one :class:`~eawf.kernel.state.enums.Urgency`-ranked list.
    The sort is by descending urgency with a stable same-tier tiebreak on
    :data:`_KIND_ORDER` (a needs_user pause outranks an incident sharing its
    urgency), so the order is fully deterministic.

    Args:
        state: The bound scope state; ``None`` (the cold-load / portfolio
            window) contributes no state-derived items.
        pauses: The open ``needs_user`` pauses across scopes (the host
            resolves them from
            :func:`~eawf.workflow.skills.needs_user.list_open_pauses`).

    Returns:
        The attention items ordered most-urgent first; empty when nothing
        needs the operator (the honest-empty case the band renders as
        :data:`EMPTY_FEED_TEXT`).
    """
    items: list[AttentionItem] = list(_pause_items(pauses))
    if state is not None:
        items.extend(_failed_wave_items(state))
        items.extend(_incident_items(state))
        items.extend(_open_question_items(state))
        items.extend(_ready_wave_items(state))
    ranked = tuple(
        sorted(items, key=lambda item: (_urgency_rank(item.urgency), _KIND_ORDER[item.kind]))
    )
    logger.debug(f"build_attention_feed count={len(ranked)}")
    return ranked


__all__ = [
    "EMPTY_FEED_TEXT",
    "AttentionItem",
    "AttentionKind",
    "build_attention_feed",
]
