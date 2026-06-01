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
* failed waves (:attr:`~eawf.kernel.state.enums.WaveStatus.FAILED`) under
  the ACTIVE phase + iter -- a dispatched wave errored and needs a look;
  always ``URGENT``. A failed wave under a closed / planned iter is
  historical, not "needs you now", so it is not surfaced.
* open incidents (:attr:`~eawf.kernel.state.enums.IncidentStatus.OPEN`) --
  ranked by severity (critical -> urgent down to low -> low).
* blocking open questions
  (:attr:`~eawf.kernel.state.enums.OpenQuestionStatus.BLOCKED`) -- the only
  question kind the balanced-autonomy interrupt raises; carries its own
  urgency.
* ready-to-claim waves
  (:attr:`~eawf.kernel.state.enums.WaveStatus.PENDING` whose every ``deps``
  wave is CLOSED) under the ACTIVE iter -- the next move is unblocked;
  advisory ``NORMAL``. A ready wave under a planned future iter is not the
  operator's next move, so it is not surfaced.

The wave-derived signals are scoped to the active phase + iter
(:attr:`~eawf.kernel.state.models.CurrentPointers.phase_id` /
:attr:`~eawf.kernel.state.models.CurrentPointers.iter_id`) so a feed never
fills with obsolete failed / ready waves from old phases. Pauses,
incidents, and open questions are point-in-time (an open incident is open
regardless of which phase is active), so they are not iter-scoped.

The ranking reuses the same descending-:class:`Urgency` key the
needs_user inbox uses (:mod:`eawf.surfaces.tui.screens.overlays.needs_user_inbox`),
so a pause and an incident sort on one comparable scale. The sort is
stable, so items sharing a tier keep source order (pauses, then failed
waves, then incidents, then questions, then ready waves -- most-acute
source first).

The portfolio variant (:func:`build_portfolio_attention_feed`) runs the
same per-repo reduction across the explicitly registered repos (resolved
through the W24 registry boundary, never a filesystem scan), tags each
row with its repo code, and merges into one ranked feed -- so the user /
portfolio scope answers "which repo needs me?" rather than rendering an
empty band.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
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
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from eawf.kernel.state.models import State
    from eawf.platform.registry.models import RegistryRepoEntry
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

#: Seconds-per-unit thresholds for :func:`format_time_ago`, coarsest last.
_MINUTE_S: float = 60.0
_HOUR_S: float = 3600.0
_DAY_S: float = 86400.0


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
        occurred_at: When the underlying signal happened -- a pause's
            raise-event timestamp, a failed wave's work-start (claim) time,
            an incident's open time. Renders as a relative ``time-ago`` on
            the row (:func:`format_time_ago`); ``None`` when no source clock
            exists (e.g. a ready wave with no claim yet), which renders blank.
        repo_tag: For a portfolio (cross-repo) feed, the repo code the row
            belongs to so the operator sees which repo needs them; ``None``
            for a single-repo feed where every row shares the bound scope.
    """

    urgency: Urgency
    kind: AttentionKind
    title: str
    detail: str
    pause_urn: str | None = None
    question: UserQuestion | None = None
    occurred_at: datetime | None = None
    repo_tag: str | None = None

    @property
    def actionable(self) -> bool:
        """``True`` when selecting the row opens something (a pause modal)."""
        return self.pause_urn is not None and self.question is not None

    @property
    def dismiss_key(self) -> str:
        """Return a stable, hashable identity for session-level dismiss.

        A dismissed-set keys on this so the same logical row stays hidden
        across reduces without depending on the (unhashable) ``question``.
        A pause keys on its stable ``pause_urn``; every other kind keys on
        its ``title``, which leads with the entity id (wave id / incident
        id / question id), so the key survives an unrelated state revision.
        The ``repo_tag`` namespaces the key so the same wave id under two
        repos in a portfolio feed dismisses independently.

        Returns:
            A ``"<repo>:<kind>:<id>"`` string unique to the logical row.
        """
        anchor = self.pause_urn if self.pause_urn is not None else self.title
        return f"{self.repo_tag or ''}:{self.kind.value}:{anchor}"


def format_time_ago(occurred_at: datetime | None, now: datetime) -> str:
    """Return a compact relative ``time-ago`` label for *occurred_at*.

    The coarsest unit that fits is chosen: seconds render ``"<unit>m ago"``
    only once a full minute elapses (sub-minute is ``"now"``); minutes
    render whole (``"15m ago"``); hours render with one decimal
    (``"1.2h ago"``); a day or more renders whole days (``"3d ago"``). A
    ``None`` timestamp (no source clock) renders the empty string so the
    row simply omits the column.

    The formatter is pure -- it takes *now* explicitly rather than reading
    the wall clock -- so the band / inbox stay unit-testable and a snapshot
    fixture can pin a fixed *now* for deterministic goldens.

    Args:
        occurred_at: The instant the signal happened, or ``None``.
        now: The reference instant to measure back from.

    Returns:
        A short label (``"now"`` / ``"15m ago"`` / ``"1.2h ago"`` /
        ``"3d ago"``), or ``""`` when *occurred_at* is ``None``.
    """
    if occurred_at is None:
        return ""
    elapsed_s = (now - occurred_at).total_seconds()
    if elapsed_s < _MINUTE_S:
        return "now"
    if elapsed_s < _HOUR_S:
        return f"{int(elapsed_s // _MINUTE_S)}m ago"
    if elapsed_s < _DAY_S:
        return f"{elapsed_s / _HOUR_S:.1f}h ago"
    return f"{int(elapsed_s // _DAY_S)}d ago"


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
        ``pause_urn`` + question so the band can re-open the modal, plus the
        pause-raise ``occurred_at`` for the row's time-ago.
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
                occurred_at=pause.occurred_at,
            )
        )
    return items


def _is_active_wave(state: State, wave: object) -> bool:
    """Return whether *wave* sits under the active phase + iter.

    The wave-derived attention signals (failed + ready-to-claim) are
    "needs you now" only for the work the operator is currently driving,
    so a wave is in scope iff its ``iter_id`` is the active iter
    (:attr:`~eawf.kernel.state.models.CurrentPointers.iter_id`) and that
    iter sits under the active phase
    (:attr:`~eawf.kernel.state.models.CurrentPointers.phase_id`). When no
    iter is active (cold state / between phases) nothing is in scope.

    Args:
        state: The bound state whose ``current`` pointers fix the scope.
        wave: The wave to test (typed ``object`` to avoid importing the
            model at runtime; ``iter_id`` is read off it).

    Returns:
        ``True`` when the wave is under the active phase + iter.
    """
    active_iter = state.current.iter_id
    active_phase = state.current.phase_id
    if active_iter is None or active_phase is None:
        return False
    wave_iter = getattr(wave, "iter_id", None)
    if wave_iter != active_iter:
        return False
    iter_row = (state.iters or {}).get(active_iter)
    if iter_row is None:
        return False
    return iter_row.phase_id == active_phase


def _failed_wave_items(state: State) -> list[AttentionItem]:
    """Build URGENT rows for every failed wave under the active phase + iter.

    A failed wave from a closed / planned iter is historical -- it will
    never clear, so surfacing it would wedge the feed -- and is dropped;
    only a failure under the iter the operator is currently driving is
    "needs you now".
    """
    items: list[AttentionItem] = []
    for wave in state.waves.values():
        if wave.status is not WaveStatus.FAILED:
            continue
        if not _is_active_wave(state, wave):
            continue
        items.append(
            AttentionItem(
                urgency=Urgency.URGENT,
                kind=AttentionKind.FAILED_WAVE,
                title=f"{wave.id} {wave.title}",
                detail="wave failed",
                occurred_at=wave.claimed_at,
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
                occurred_at=incident.opened_at,
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
                occurred_at=question.created_at,
            )
        )
    return items


def _ready_wave_items(state: State) -> list[AttentionItem]:
    """Build advisory rows for active-iter PENDING waves whose deps are CLOSED.

    A pending wave is *ready to claim* once every wave in its ``deps`` is
    CLOSED -- the next move is unblocked. A pending wave with an unmet dep
    is still waiting, so it is not surfaced (it is not yet actionable). A
    ready wave under a planned future iter is not the operator's current
    next move either, so the scan is scoped to the active iter.

    Args:
        state: The bound state to scan.

    Returns:
        One ``NORMAL`` :class:`AttentionItem` per active-iter ready-to-claim
        wave.
    """
    items: list[AttentionItem] = []
    for wave in state.waves.values():
        if wave.status is not WaveStatus.PENDING:
            continue
        if not _is_active_wave(state, wave):
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
                occurred_at=wave.claimed_at,
            )
        )
    return items


def _rank(items: Iterable[AttentionItem]) -> tuple[AttentionItem, ...]:
    """Sort *items* most-urgent first with the stable per-kind tiebreak."""
    return tuple(
        sorted(items, key=lambda item: (_urgency_rank(item.urgency), _KIND_ORDER[item.kind]))
    )


def build_attention_feed(
    state: State | None,
    pauses: Iterable[OpenPause] = (),
    *,
    dismissed: frozenset[str] = frozenset(),
) -> tuple[AttentionItem, ...]:
    """Rank the operator's open attention items, most-urgent first.

    Folds the needs_user pauses plus the state-resident attention signals
    (failed waves + ready-to-claim waves under the active phase / iter,
    open incidents, blocking questions) onto one
    :class:`~eawf.kernel.state.enums.Urgency`-ranked list. The sort is by
    descending urgency with a stable same-tier tiebreak on
    :data:`_KIND_ORDER` (a needs_user pause outranks an incident sharing its
    urgency), so the order is fully deterministic.

    The feed is a live reducer: an item is present only while its source is
    unresolved, so it auto-clears the moment the source resolves (a pause is
    answered, a wave is claimed / closed, an incident is closed). The
    *dismissed* set adds an explicit session-level acknowledge on top: a row
    whose :attr:`AttentionItem.dismiss_key` is in *dismissed* is filtered out
    even while its source is still live.

    Args:
        state: The bound scope state; ``None`` (the cold-load window)
            contributes no state-derived items.
        pauses: The open ``needs_user`` pauses across scopes (the host
            resolves them from
            :func:`~eawf.workflow.skills.needs_user.list_open_pauses`).
        dismissed: Stable :attr:`AttentionItem.dismiss_key` values the
            operator has acknowledged this session; matching rows are
            dropped from the output.

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
    if dismissed:
        items = [item for item in items if item.dismiss_key not in dismissed]
    ranked = _rank(items)
    logger.debug(f"build_attention_feed count={len(ranked)} dismissed={len(dismissed)}")
    return ranked


def build_portfolio_attention_feed(
    repos: Iterable[RegistryRepoEntry],
    *,
    load_state: Callable[[Path], State | None],
    dismissed: frozenset[str] = frozenset(),
) -> tuple[AttentionItem, ...]:
    """Aggregate open attentions across the registered repos, ranked.

    The user / portfolio scope has no single bound ``state.json`` -- it
    spans the explicitly registered repos. This runs the same per-repo
    state-derived reduction (failed / ready waves under each repo's active
    iter, open incidents, blocking questions; pauses are surfaced through
    the cross-scope global inbox, not duplicated here) over each repo and
    merges the rows into one ranked feed, tagging each row with its repo
    code so the operator sees which repo needs them.

    The repo set comes from the explicit registry (the W24
    :func:`~eawf.platform.registry.models.read_registry` boundary, never a
    filesystem scan), and each repo degrades independently: a repo whose
    ``state.json`` is missing or unreadable (``load_state`` returns
    ``None``) is skipped, not fatal, so one broken repo never blanks the
    whole portfolio band.

    Args:
        repos: The registered repo entries (each carries its on-disk
            ``path`` repo root), resolved by the caller through the registry
            boundary.
        load_state: Reader that resolves a repo's ``state.json`` from its
            repo-root :class:`~pathlib.Path` to a
            :class:`~eawf.kernel.state.models.State`, returning ``None`` when
            the repo state is missing / unreadable (the per-repo degrade
            seam; also the test seam).
        dismissed: Stable :attr:`AttentionItem.dismiss_key` values the
            operator has acknowledged this session; matching rows are
            dropped.

    Returns:
        The merged attention items across every readable repo, ordered
        most-urgent first; empty when nothing across the portfolio needs the
        operator (the honest-empty case).
    """
    from pathlib import Path

    items: list[AttentionItem] = []
    for entry in repos:
        try:
            repo_state = load_state(Path(entry.path))
        except OSError as exc:
            logger.debug(f"build_portfolio_attention_feed skip repo={entry.code!r} cause={exc!r}")
            continue
        if repo_state is None:
            logger.debug(f"build_portfolio_attention_feed skip repo={entry.code!r} cause=no_state")
            continue
        tagged = [
            _retag(item, entry.code)
            for item in (
                *_failed_wave_items(repo_state),
                *_incident_items(repo_state),
                *_open_question_items(repo_state),
                *_ready_wave_items(repo_state),
            )
        ]
        items.extend(tagged)
    if dismissed:
        items = [item for item in items if item.dismiss_key not in dismissed]
    ranked = _rank(items)
    logger.debug(f"build_portfolio_attention_feed count={len(ranked)} dismissed={len(dismissed)}")
    return ranked


def _retag(item: AttentionItem, repo_code: str) -> AttentionItem:
    """Return a copy of *item* tagged with its owning repo code."""
    from dataclasses import replace

    return replace(item, repo_tag=repo_code)


__all__ = [
    "EMPTY_FEED_TEXT",
    "AttentionItem",
    "AttentionKind",
    "build_attention_feed",
    "build_portfolio_attention_feed",
    "format_time_ago",
]
