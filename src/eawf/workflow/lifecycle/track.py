"""Pure-functional Track promotion gate + out-of-scope wave containment.

The Track promotion gate (:func:`promote_track`) is the guardian a Track passes
before its lifecycle advances: a Track promotes only when every Outcome
reachable from it (via the ``Track -> Goal -> Outcome`` containment chain) holds
:attr:`~eawf.kernel.state.enums.OutcomeStatus.MET` *and* has held it for at
least :data:`MIN_PROMOTION_PERIOD`. An attested ``--force`` overrides the refuse
with a recorded reason, so a forced promotion still leaves an auditable trail of
*why* the gate was bypassed.

The out-of-scope check (:func:`wave_scope_violations`) is the containment
backstop: a Track declares its file scope as the glob patterns on
:attr:`~eawf.kernel.state.models.Track.scope_globs`, and a wave whose
:attr:`~eawf.kernel.state.models.Wave.file_scopes` fall outside that declared
scope is *flagged* rather than silently assumed in-scope. Containment is
CHECKED, not trusted.

Both helpers are pure -- they read the typed :class:`State` and the relevant
rows and return a typed result; neither mutates state. The promotion *write*
(advancing :attr:`Track.status`) is a separate mutator concern, gated on the
:class:`TrackPromotionGate` this module produces.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import NamedTuple

from eawf.kernel.state.enums import OutcomeStatus, TrackStatus
from eawf.kernel.state.models import Goal, Outcome, State, Track, Wave
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)

#: Minimum duration a Track's outcomes must hold ``MET`` before the Track may
#: promote without a force. Anchored on each Outcome's ``updated_at`` so a
#: just-flipped outcome cannot promote a Track on the same tick it cleared its
#: threshold -- the metric has to *hold* across the period, not merely touch it.
MIN_PROMOTION_PERIOD: timedelta = timedelta(days=7)


class TrackPromotionGate(NamedTuple):
    """Typed verdict of the Track promotion gate.

    Attributes:
        track_id: Id of the Track the gate was run against.
        promotable: ``True`` iff the Track may promote without a force --
            every reachable Outcome holds ``MET`` over
            :data:`MIN_PROMOTION_PERIOD`.
        outcomes_met: ``True`` iff every reachable Outcome holds ``MET``
            (independent of the hold period).
        period_held: ``True`` iff every reachable Outcome has held its status
            for at least :data:`MIN_PROMOTION_PERIOD`.
        blocking_outcome_ids: Ids of the Outcomes that block promotion -- the
            ones that are not ``MET`` or have not held the period -- in
            containment order. Empty when :attr:`promotable` is ``True``.
        forced: ``True`` when the gate was overridden by an attested force.
        force_reason: The recorded reason for a forced override, else ``None``.
    """

    track_id: str
    promotable: bool
    outcomes_met: bool
    period_held: bool
    blocking_outcome_ids: list[str]
    forced: bool
    force_reason: str | None


def _track_outcomes(state: State, track: Track) -> list[Outcome]:
    """Return every :class:`Outcome` reachable from *track* in containment order.

    Walks the ``Track -> Goal -> Outcome`` chain (:attr:`Track.goal_ids` ->
    :attr:`Goal.outcome_ids`). An id whose Goal or Outcome row is absent is
    skipped so a partially-linked Track does not crash the gate; duplicate
    outcome ids across goals are de-duplicated preserving first-seen order.
    """
    goals: dict[str, Goal] = state.goals or {}
    outcomes: dict[str, Outcome] = state.outcomes or {}
    seen: set[str] = set()
    ordered: list[Outcome] = []
    for goal_id in track.goal_ids:
        goal = goals.get(goal_id)
        if goal is None:
            continue
        for outcome_id in goal.outcome_ids:
            if outcome_id in seen:
                continue
            outcome = outcomes.get(outcome_id)
            if outcome is None:
                continue
            seen.add(outcome_id)
            ordered.append(outcome)
    return ordered


def evaluate_track_promotion(
    state: State,
    *,
    track_id: str,
    force_reason: str | None = None,
    now: datetime | None = None,
) -> TrackPromotionGate:
    """Evaluate the Track promotion gate without mutating state.

    The Track promotes (its lifecycle may advance) only when every Outcome
    reachable from it holds :attr:`~eawf.kernel.state.enums.OutcomeStatus.MET`
    *and* has held it for at least :data:`MIN_PROMOTION_PERIOD` (anchored on each
    Outcome's :attr:`Outcome.updated_at`). A Track with no reachable Outcome is
    *not* promotable on the merits -- there is no evidence the workstream
    delivered -- so an empty outcome set blocks promotion just like an unmet one.

    A non-``None`` *force_reason* records an attested override: the returned gate
    is :attr:`TrackPromotionGate.forced` with the reason carried through, and
    :attr:`TrackPromotionGate.promotable` is forced ``True`` so the caller may
    advance the Track while the merits verdict (``outcomes_met`` / ``period_held``
    / ``blocking_outcome_ids``) stays visible on the same record for audit.

    Args:
        state: The state holding the Track and its outcome graph (read-only).
        track_id: Id of the Track to evaluate.
        force_reason: Attested reason for a forced override, or ``None`` for a
            merits-only evaluation. A whitespace-only string is rejected.
        now: Reference instant the hold period is measured against; defaults to
            :func:`datetime.now` in UTC.

    Returns:
        The :class:`TrackPromotionGate` verdict.

    Raises:
        LifecycleError: When *track_id* is unknown, or *force_reason* is a
            blank (whitespace-only) string -- an attested force must record a
            real reason.
    """
    tracks: dict[str, Track] = state.tracks or {}
    track = tracks.get(track_id)
    if track is None:
        raise LifecycleError(f"unknown track {track_id!r}")
    if force_reason is not None and not force_reason.strip():
        raise LifecycleError(
            f"track {track_id!r} force-promotion needs an attested reason"
        )

    reference = now if now is not None else datetime.now(UTC)
    outcomes = _track_outcomes(state, track)

    blocking: list[str] = []
    period_held = True
    for outcome in outcomes:
        is_met = outcome.status is OutcomeStatus.MET
        held = reference - outcome.updated_at >= MIN_PROMOTION_PERIOD
        if not held:
            period_held = False
        if not is_met or not held:
            blocking.append(outcome.id)

    outcomes_met = bool(outcomes) and not any(
        outcome.status is not OutcomeStatus.MET for outcome in outcomes
    )
    if not outcomes:
        period_held = False
    on_merits = outcomes_met and period_held and not blocking
    forced = force_reason is not None
    promotable = on_merits or forced

    logger.info(
        f"evaluate_track_promotion track={track_id!r} "
        f"outcomes_met={outcomes_met} period_held={period_held} "
        f"forced={forced} promotable={promotable}"
    )
    return TrackPromotionGate(
        track_id=track_id,
        promotable=promotable,
        outcomes_met=outcomes_met,
        period_held=period_held,
        blocking_outcome_ids=blocking,
        forced=forced,
        force_reason=force_reason,
    )


def promote_track(
    state: State,
    *,
    track_id: str,
    new_status: TrackStatus,
    force_reason: str | None = None,
    now: datetime | None = None,
) -> TrackPromotionGate:
    """Advance a Track's lifecycle through the promotion gate, mutating in place.

    Runs :func:`evaluate_track_promotion` and REFUSES the promotion (raising
    :class:`LifecycleError`) unless the gate is promotable -- which on the merits
    means every reachable Outcome holds ``MET`` over
    :data:`MIN_PROMOTION_PERIOD`. An attested *force_reason* overrides the refuse
    with the reason recorded on the returned gate, so a forced promotion still
    advances the Track but leaves the *why* auditable.

    On a passing (or forced) gate the Track's :attr:`Track.status` is set to
    *new_status* and the gate verdict is returned. The merits fields on the gate
    stay populated even for a forced promotion, so a reviewer can see what was
    bypassed.

    Args:
        state: State to mutate in place when the gate passes.
        track_id: Id of the Track to promote.
        new_status: The :class:`~eawf.kernel.state.enums.TrackStatus` the Track
            advances to when the gate passes.
        force_reason: Attested reason for a forced override, or ``None`` for a
            merits-only promotion.
        now: Reference instant the hold period is measured against; defaults to
            :func:`datetime.now` in UTC.

    Returns:
        The :class:`TrackPromotionGate` verdict that ratified the promotion.

    Raises:
        LifecycleError: When *track_id* is unknown, *force_reason* is blank, or
            the merits gate refuses and no attested force was supplied.
    """
    gate = evaluate_track_promotion(
        state, track_id=track_id, force_reason=force_reason, now=now
    )
    if not gate.promotable:
        blockers = ", ".join(gate.blocking_outcome_ids) or "no outcomes defined"
        raise LifecycleError(
            f"track {track_id!r} not promotable: outcomes not met over period "
            f"({blockers})"
        )

    track = (state.tracks or {})[track_id]
    track.status = new_status
    logger.info(
        f"promote_track track={track_id!r} status={new_status.value!r} "
        f"forced={gate.forced}"
    )
    return gate


def _scope_in_declared(file_scope: str, declared: list[str]) -> bool:
    """Return whether *file_scope* is contained by any *declared* glob.

    A scope is contained when it equals a declared glob, matches one as an
    :func:`fnmatch.fnmatch` pattern (so ``src/foo/**`` covers ``src/foo/bar.py``),
    or sits under a declared directory prefix. The trailing ``**`` of a declared
    glob is normalised to a prefix so a directory pattern covers nested files
    that ``fnmatch`` alone (which does not treat ``/`` specially) would miss.
    """
    for pattern in declared:
        if file_scope == pattern or fnmatch(file_scope, pattern):
            return True
        prefix = pattern
        for suffix in ("/**", "/*", "**", "*"):
            if prefix.endswith(suffix):
                prefix = prefix[: -len(suffix)]
                break
        prefix = prefix.rstrip("/")
        if prefix and (file_scope == prefix or file_scope.startswith(prefix + "/")):
            return True
    return False


def wave_scope_violations(track: Track, wave: Wave) -> list[str]:
    """Return the *wave* file scopes that fall outside *track*'s declared scope.

    Containment is CHECKED against :attr:`Track.scope_globs`: each entry in
    :attr:`Wave.file_scopes` must be covered by at least one declared glob, else
    it is an out-of-scope edit and is returned in the violation list (in the
    wave's declared order). An empty result means the wave is fully contained.

    A Track that declares no scope (:attr:`Track.scope_globs` empty) cannot
    enforce containment -- there is nothing to check against -- so every wave is
    treated as in-scope and the result is empty. A Track that declares a scope
    flags any wave file outside it.

    Args:
        track: The Track whose declared scope bounds the wave.
        wave: The wave whose file scopes are checked for containment.

    Returns:
        The wave file scopes outside the Track's declared scope, in the wave's
        declared order. Empty when the wave is contained or the Track declares
        no scope.
    """
    if not track.scope_globs:
        return []
    violations = [
        file_scope
        for file_scope in wave.file_scopes
        if not _scope_in_declared(file_scope, track.scope_globs)
    ]
    if violations:
        logger.info(
            f"wave_scope_violations track={track.id!r} wave={wave.id!r} "
            f"out_of_scope={len(violations)}"
        )
    return violations
