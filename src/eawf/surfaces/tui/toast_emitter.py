"""Ambient state-change toast emitter for the Textual dashboard.

The :class:`ToastEmitter` diffs two consecutive :class:`~eawf.kernel.state.models.State`
snapshots and computes the set of ambient, focus-preserving notifications the
dashboard surfaces bottom-right. It is the ``toast_rack`` parallel region of the
tui-richer-views statechart: an ambient region whose events NEVER steal focus or
block input.

Four change classes produce a toast:

* **wave close** — a wave that transitions ``PENDING`` / ``CLAIMED`` /
  ``IN_PROGRESS`` → ``CLOSED`` across the two snapshots.
* **audit verdict** — an audit that gains (or changes) a non-``None``
  :attr:`~eawf.kernel.state.models.Audit.verdict`.
* **needs-user** — the count of open needs_user pauses rose since the previous
  snapshot (sourced from the event-store tail, passed in by the app).
* **failure** — a fleet lane FAILED across the two snapshots: the run's
  :attr:`~eawf.kernel.state.models.FleetCounters.failed` tally rose, so a
  gate-fail / agent-error / dispatch-fail forked a lane rather than closing it
  clean. This is the distinct failure class — it reads ``error`` red and never
  fires for a normal close (a clean close advances ``closed``, not ``failed``).

Two flood guards keep the rack quiet:

* **first load** — there is no previous snapshot to diff, so NOTHING is emitted;
  only subsequent transitions emit.
* **daemon reconnect** — when one revision carries more than
  :data:`FLOOD_THRESHOLD` transitions (the burst that accrues while the daemon
  was unreachable), a single summary toast ("N changes") replaces the per-change
  toasts.

The diff engine (:meth:`ToastEmitter.diff`) is pure: it returns a list of
:class:`ToastNotification` records and touches no Textual state. The app-facing
driver (:meth:`ToastEmitter.emit`) feeds those records to ``app.notify`` and
follows the liveness contract's on_failure rule — a render failure drops the
toast, never crashes the app.

Verbosity is gated by the ``ui.toasts`` config key:

* ``off`` — no toasts at all.
* ``important`` — wave close / audit verdict / needs-user / failure only (the
  default).
* ``all`` — currently the same change set as ``important``; reserved for the
  more verbose future bands (per-check audit progress, dispatch transitions)
  that ride :data:`ToastCategory` so the gate is already in place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.enums import AuditVerdict, WaveStatus

if TYPE_CHECKING:
    from textual.app import App

    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: Verbosity levels for the ``ui.toasts`` config key. ``off`` silences the
#: rack; ``important`` emits the wave-close / audit-verdict / needs-user /
#: failure set; ``all`` is reserved for the more verbose future bands.
ToastVerbosity = Literal["off", "important", "all"]

#: Allowed ``ui.toasts`` values, mirrored from the config registry row so a
#: caller can validate without importing the registry.
TOAST_VERBOSITY_CHOICES: tuple[ToastVerbosity, ...] = ("off", "important", "all")

#: Default verbosity when ``ui.toasts`` is unset — matches the registry default.
DEFAULT_VERBOSITY: ToastVerbosity = "important"

#: Change classes a toast belongs to. ``important``-tier categories emit under
#: both ``important`` and ``all``; ``verbose``-tier categories emit only under
#: ``all`` (none yet — the verbose bands land in later waves). ``failure`` is
#: the distinct fleet-lane-failed class — a gate-fail / agent-error /
#: dispatch-fail forked a lane, read off the run's rising ``failed`` tally.
ToastCategory = Literal["wave_close", "audit_verdict", "needs_user", "failure", "summary"]

#: Textual notification severities the rack uses. Wave close / audit pass read
#: as ``information``; a major / minor audit verdict and the needs-user raise
#: read as ``warning`` so they stand out without the ``error`` red.
ToastSeverity = Literal["information", "warning", "error"]

#: When a single revision carries strictly more than this many emittable
#: transitions, the per-change toasts collapse into one summary toast. The
#: bound is the daemon-reconnect flood guard: a healthy on-event cadence emits
#: one or two changes per revision, so a burst above the threshold signals a
#: reconnect catch-up rather than live work.
FLOOD_THRESHOLD: int = 3

#: Categories surfaced at the ``important`` verbosity tier (and therefore also
#: at ``all``). ``summary`` is always allowed when any change passes the gate.
#: The ``failure`` class rides ``important`` so a forked lane is never silenced
#: below the verbose band.
_IMPORTANT_CATEGORIES: frozenset[ToastCategory] = frozenset(
    {"wave_close", "audit_verdict", "needs_user", "failure", "summary"}
)


class ToastNotification(BaseModel):
    """One ambient notification computed from a state diff.

    Attributes:
        message: Human-readable toast body rendered bottom-right.
        category: The change class that produced the toast.
        severity: Textual notification severity (drives the toast tint).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str
    category: ToastCategory
    severity: ToastSeverity = "information"


def _closed_wave_ids(prev: State, current: State) -> list[str]:
    """Return wave ids that transitioned to CLOSED between the snapshots.

    A wave counts as newly closed when it is present in both snapshots, was
    not CLOSED before, and is CLOSED now. Ids absent from *prev* (a wave that
    first appears already CLOSED) are ignored — there is no transition to
    announce. Order follows the current snapshot's wave map for determinism.

    Args:
        prev: The previous state snapshot.
        current: The current state snapshot.
    """
    closed: list[str] = []
    for wave_id, wave in current.waves.items():
        if wave.status is not WaveStatus.CLOSED:
            continue
        previous = prev.waves.get(wave_id)
        if previous is None or previous.status is WaveStatus.CLOSED:
            continue
        closed.append(wave_id)
    return closed


def _new_audit_verdicts(prev: State, current: State) -> list[tuple[str, AuditVerdict]]:
    """Return audit ids that gained or changed a verdict between snapshots.

    An audit counts when its current verdict is non-``None`` and differs from
    its previous verdict (``None`` or a different value). Order follows the
    current snapshot's audit map for determinism.

    Args:
        prev: The previous state snapshot.
        current: The current state snapshot.
    """
    verdicts: list[tuple[str, AuditVerdict]] = []
    prev_audits = prev.audits or {}
    for audit_id, audit in (current.audits or {}).items():
        if audit.verdict is None:
            continue
        previous = prev_audits.get(audit_id)
        prev_verdict = previous.verdict if previous is not None else None
        if prev_verdict == audit.verdict:
            continue
        verdicts.append((audit_id, audit.verdict))
    return verdicts


def _audit_severity(verdict: AuditVerdict) -> ToastSeverity:
    """Map an audit verdict onto a toast severity.

    A clean ``pass`` reads as ``information``; ``minor`` / ``major`` read as
    ``warning`` so they draw the eye without the error red.

    Args:
        verdict: The audit verdict to map.
    """
    if verdict is AuditVerdict.PASS:
        return "information"
    return "warning"


def _new_failure_count(prev: State, current: State) -> int:
    """Return how many fleet lanes newly FAILED between the snapshots.

    Reads the persisted :attr:`~eawf.kernel.state.models.FleetRun.counters`
    ``failed`` tally off each snapshot's :attr:`~eawf.kernel.state.models.State.fleet_run`
    and returns the rise. A lane that the watcher reported as a genuine fork
    (a gate-fail / agent-error / dispatch-fail that did not close clean) bumps
    ``failed``; a clean close bumps ``closed`` instead, so a normal close never
    registers here. A run absent from either snapshot, or a falling tally (a
    fresh run armed over a finished one), reads as zero new failures.

    Args:
        prev: The previous state snapshot.
        current: The current state snapshot.

    Returns:
        The number of lanes that newly failed, clamped at ``0``.
    """
    prev_run = prev.fleet_run
    current_run = current.fleet_run
    if prev_run is None or current_run is None:
        return 0
    return max(0, current_run.counters.failed - prev_run.counters.failed)


class ToastEmitter:
    """Pure-ish diff engine that turns consecutive state snapshots into toasts.

    The emitter is constructed with a verbosity gate and a flood threshold; it
    holds no snapshot of its own — the app passes the previous and current
    snapshots on each call so the engine stays pure and unit-testable.

    Attributes:
        verbosity: The ``ui.toasts`` gate — ``off`` / ``important`` / ``all``.
        flood_threshold: The per-revision transition count above which the
            per-change toasts collapse into one summary toast.
    """

    def __init__(
        self,
        verbosity: ToastVerbosity = DEFAULT_VERBOSITY,
        *,
        flood_threshold: int = FLOOD_THRESHOLD,
    ) -> None:
        """Construct the emitter.

        Args:
            verbosity: The ``ui.toasts`` verbosity gate.
            flood_threshold: Per-revision transition count above which a
                single summary toast replaces the per-change toasts.

        Raises:
            ValueError: When *verbosity* is not a recognised level or
                *flood_threshold* is below 1.
        """
        if verbosity not in TOAST_VERBOSITY_CHOICES:
            raise ValueError(f"unknown toast verbosity: {verbosity!r}")
        if flood_threshold < 1:
            raise ValueError(f"flood_threshold must be >= 1: {flood_threshold!r}")
        self.verbosity: ToastVerbosity = verbosity
        self.flood_threshold = flood_threshold

    def _category_allowed(self, category: ToastCategory) -> bool:
        """Return whether *category* passes the active verbosity gate."""
        if self.verbosity == "off":
            return False
        if self.verbosity == "important":
            return category in _IMPORTANT_CATEGORIES
        return True  # "all"

    def diff(
        self,
        prev: State | None,
        current: State,
        *,
        prev_open_pause_count: int = 0,
        open_pause_count: int = 0,
    ) -> list[ToastNotification]:
        """Compute the ambient notifications for a state revision.

        The engine is pure: it reads only its arguments and returns a fresh
        list. It NEVER touches focus, scroll, or any Textual state — the app
        decides how to surface the returned notifications.

        First-load guard: when *prev* is ``None`` (the first snapshot bound at
        mount has no predecessor to diff) the result is always empty, so a
        launch against an already-populated state fires no toasts.

        Flood guard: when the number of emittable transitions exceeds
        :attr:`flood_threshold` (the burst that accrues while the daemon was
        unreachable), the per-change toasts collapse into a single summary
        toast.

        Args:
            prev: The previous state snapshot, or ``None`` on first load.
            current: The current state snapshot.
            prev_open_pause_count: Count of open needs_user pauses at the
                previous snapshot (from the event-store tail).
            open_pause_count: Count of open needs_user pauses at the current
                snapshot (from the event-store tail).

        Returns:
            The ambient notifications to surface, in the order they should be
            emitted. Empty on first load, when verbosity is ``off``, or when
            nothing changed.

        Raises:
            ValueError: When a pause count is negative.
        """
        if prev_open_pause_count < 0 or open_pause_count < 0:
            raise ValueError(
                f"pause counts must be >= 0: prev={prev_open_pause_count!r} "
                f"current={open_pause_count!r}"
            )
        if self.verbosity == "off":
            return []
        if prev is None:
            # First load: no predecessor to diff against. Only subsequent
            # transitions emit, so a launch against a populated state is quiet.
            return []

        candidates = self._collect(
            prev,
            current,
            prev_open_pause_count=prev_open_pause_count,
            open_pause_count=open_pause_count,
        )
        if not candidates:
            return []
        if len(candidates) > self.flood_threshold:
            logger.info(
                f"diff flood_guard count={len(candidates)} threshold={self.flood_threshold}"
            )
            return [
                ToastNotification(
                    message=f"{len(candidates)} changes",
                    category="summary",
                    severity="information",
                )
            ]
        return candidates

    def _collect(
        self,
        prev: State,
        current: State,
        *,
        prev_open_pause_count: int,
        open_pause_count: int,
    ) -> list[ToastNotification]:
        """Build the gated per-change notifications for a non-first revision.

        Args:
            prev: The previous state snapshot.
            current: The current state snapshot.
            prev_open_pause_count: Open pauses at the previous snapshot.
            open_pause_count: Open pauses at the current snapshot.
        """
        notifications: list[ToastNotification] = []

        if self._category_allowed("wave_close"):
            for wave_id in _closed_wave_ids(prev, current):
                notifications.append(
                    ToastNotification(
                        message=f"✓ {wave_id} closed",
                        category="wave_close",
                        severity="information",
                    )
                )

        if self._category_allowed("audit_verdict"):
            for audit_id, verdict in _new_audit_verdicts(prev, current):
                notifications.append(
                    ToastNotification(
                        message=f"{audit_id} {verdict.value}",
                        category="audit_verdict",
                        severity=_audit_severity(verdict),
                    )
                )

        if self._category_allowed("needs_user") and open_pause_count > prev_open_pause_count:
            raised = open_pause_count - prev_open_pause_count
            suffix = "s" if raised != 1 else ""
            notifications.append(
                ToastNotification(
                    message=f"{raised} question{suffix} need your input",
                    category="needs_user",
                    severity="warning",
                )
            )

        if self._category_allowed("failure"):
            failed = _new_failure_count(prev, current)
            if failed > 0:
                suffix = "s" if failed != 1 else ""
                notifications.append(
                    ToastNotification(
                        message=f"{failed} lane{suffix} failed",
                        category="failure",
                        severity="error",
                    )
                )

        return notifications

    def emit(
        self,
        app: App[object],
        prev: State | None,
        current: State,
        *,
        prev_open_pause_count: int = 0,
        open_pause_count: int = 0,
    ) -> list[ToastNotification]:
        """Diff the snapshots and surface each notification via ``app.notify``.

        Ambient + focus-preserving: ``app.notify`` stacks a toast bottom-right
        and auto-dismisses it on its own timer without moving focus or scroll.
        Per the toast_rack liveness contract, a render failure drops the toast
        (logged at debug) and never crashes the app.

        Args:
            app: The Textual app whose ``notify`` surfaces the toast.
            prev: The previous state snapshot, or ``None`` on first load.
            current: The current state snapshot.
            prev_open_pause_count: Open needs_user pauses at the previous
                snapshot.
            open_pause_count: Open needs_user pauses at the current snapshot.

        Returns:
            The notifications that were computed (and attempted). The return
            value lets callers / tests inspect the diff without scraping the
            Textual toast rack.
        """
        notifications = self.diff(
            prev,
            current,
            prev_open_pause_count=prev_open_pause_count,
            open_pause_count=open_pause_count,
        )
        for note in notifications:
            try:
                app.notify(note.message, severity=note.severity)
            except Exception as exc:
                logger.debug(f"emit dropped toast category={note.category} cause={exc!r}")
        return notifications


__all__ = [
    "DEFAULT_VERBOSITY",
    "FLOOD_THRESHOLD",
    "TOAST_VERBOSITY_CHOICES",
    "ToastCategory",
    "ToastEmitter",
    "ToastNotification",
    "ToastSeverity",
    "ToastVerbosity",
]
