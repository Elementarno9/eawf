"""``DoctorModeScreen`` -- the Doctor-mode health view (P29-I02-W21, TUI-3).

The Doctor mode (digit ``3``) folds the same health signals ``eawf doctor``
reports into ONE in-TUI health view, so an operator reads install / state
health without dropping to the CLI. It is a **renderer over the existing
doctor check library** -- it reuses the check functions verbatim and never
reimplements a check:

* the doctor check set (:func:`eawf.observability.doctor.checks.run_all`)
  -- tools, state presence, config merge, manifest sync, MCP drift, scale
  ceiling, render round-trip;
* git/state commit drift
  (:func:`eawf.workflow.lifecycle.wave_sha.detect_git_state_drift`) -- the
  same reconciler the ``git_state_drift`` doctor row consumes;
* recent event-store signals
  (:func:`eawf.surfaces.tui.screens.overlays.events.load_recent_events`) --
  the tail of ``<state_dir>/store/event.jsonl``, the same rows the
  ``/events`` overlay shows, with the error count surfaced.

The fold rolls every signal up to one overall health
(``ok`` / ``warn`` / ``fail``) via the doctor library's own
:func:`eawf.observability.doctor.report.overall_status`, so the in-TUI
rollup matches the CLI's exit-code logic exactly. The drift rows count
toward the rollup as a ``warn`` (matching the CLI's additive
``git_state_drift`` row); recent event errors are surfaced for the
operator but do not flip the rollup (an old error in the log is not a live
install fault -- the same stance the CLI takes by not folding the event
store into its checks).

The aggregation lives in pure module functions
(:func:`build_doctor_health`, :func:`render_health_lines`) so the rendered
text is unit-testable by feeding a :class:`DoctorHealth` value directly,
without mounting Textual or shelling out. The screen subclasses
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` so it inherits the exact
Header + Footer chrome the scope screens use; only :meth:`compose_body`
differs, composing the health pane. Renders honest-empty / all-ok cleanly:
a clean install shows every row ``ok`` and a ``healthy`` rollup; a missing
state.json degrades each state-reading check to ``ok`` with an
explanatory note (the doctor library's own no-double-flip stance).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.markup import escape_markup, style_labeled_line

if TYPE_CHECKING:
    from eawf.observability.doctor.checks import CheckResult
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.screens.overlays.events import EventRow
    from eawf.workflow.lifecycle.wave_sha import Drift

logger = logging.getLogger(__name__)

#: Health status rolled up across every doctor signal. Mirrors the
#: :data:`eawf.observability.doctor.checks.CheckStatus` triple so the TUI
#: rollup and the CLI exit-code logic share one vocabulary.
HealthStatus = Literal["ok", "warn", "fail"]

#: Per-status glyph for a check / drift / events row. Plain ASCII tokens so
#: the pane renders identically under any terminal font (no Braille
#: dependence) and the golden stays byte-stable.
_STATUS_GLYPH: dict[HealthStatus, str] = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}

#: The human rollup word for each overall status, shown in the pane title.
_ROLLUP_WORD: dict[HealthStatus, str] = {
    "ok": "healthy",
    "warn": "degraded",
    "fail": "unhealthy",
}

#: Cap on the per-wave drift rows surfaced in the pane body; the full list
#: is recoverable via ``eawf --json doctor``. Keeps the body bounded when a
#: stale branch carries many unreconciled closed waves.
_DRIFT_ROW_CAP: int = 8

#: How many recent events the pane samples from the on-disk event store
#: tail when summarising the error count. Matches the ``/events`` overlay
#: ring so both surfaces read the same window.
_EVENT_SAMPLE: int = 50

#: Footer hints for the Doctor mode -- the always-live chassis affordances
#: (mode digits, scope switch, palette, help, quit). The pane is read-only
#: this wave (no in-pane keys beyond the chassis), so no extra hints.
_DOCTOR_HINTS: tuple[str, ...] = (
    "1-6 mode",
    "w/r/u scope",
    "/ palette",
    "? help",
    "q quit",
)


@dataclass(frozen=True)
class HealthRow:
    """One rendered health row -- a single check / drift / events signal.

    Attributes:
        name: Stable machine identifier of the signal (the doctor check
            ``name``, or a synthetic name like ``git_state_drift`` /
            ``recent_events`` for the folded-in signals).
        status: The row's rolled-up status (``ok`` / ``warn`` / ``fail``).
        detail: Short human message describing the row's finding.
    """

    name: str
    status: HealthStatus
    detail: str


@dataclass(frozen=True)
class DoctorHealth:
    """The folded health view -- every doctor signal plus a rollup.

    Built by :func:`build_doctor_health` from the doctor check library, the
    git/state drift reconciler, and the recent event-store tail; rendered
    by :func:`render_health_lines`.

    Attributes:
        rows: One :class:`HealthRow` per signal, in render order (the
            doctor checks first, then the git/state drift summary, then the
            recent-events summary).
        overall: The rolled-up health across every row, computed by the
            doctor library's own
            :func:`eawf.observability.doctor.report.overall_status` so the
            TUI rollup matches the CLI exit-code logic.
        drift_count: The number of git/state drift rows the reconciler
            surfaced (``0`` when every closed wave reconciles).
        drift_kinds: The distinct drift-kind labels present, sorted, so the
            summary names *which* mismatch shapes were hit without dumping
            every per-wave row.
        event_error_count: The number of recent events whose status was not
            healthy (surfaced for the operator; does not flip ``overall``).
    """

    rows: list[HealthRow]
    overall: HealthStatus
    drift_count: int = 0
    drift_kinds: list[str] = field(default_factory=list)
    event_error_count: int = 0


def _drift_summary_row(drifts: list[Drift]) -> HealthRow:
    """Fold the git/state drift reconciler output into one health row.

    Mirrors the CLI ``git_state_drift`` doctor row: ``ok`` when every
    closed wave reconciles, else ``warn`` with a count + the distinct
    kinds. The per-wave rows themselves render separately in the body; this
    summary drives the rollup.

    Args:
        drifts: The rows from
            :func:`~eawf.workflow.lifecycle.wave_sha.detect_git_state_drift`.

    Returns:
        The ``git_state_drift`` summary :class:`HealthRow`.
    """
    if not drifts:
        return HealthRow(
            name="git_state_drift",
            status="ok",
            detail="all closed waves reconcile with git",
        )
    kinds = sorted({d.kind for d in drifts})
    return HealthRow(
        name="git_state_drift",
        status="warn",
        detail=f"{len(drifts)} drift(s); kinds: {', '.join(kinds)}",
    )


def _events_summary_row(events: tuple[EventRow, ...]) -> HealthRow:
    """Fold the recent event-store tail into one health row.

    The events signal is informational: it surfaces the error count in the
    recent window but stays ``ok`` so an old error already on disk never
    flips the live install rollup (the same stance the CLI takes by not
    folding the event store into its checks). An empty / unreadable store
    renders an honest "no recent events" note.

    Args:
        events: The most-recent rows from
            :func:`~eawf.surfaces.tui.screens.overlays.events.load_recent_events`.

    Returns:
        The ``recent_events`` summary :class:`HealthRow`.
    """
    if not events:
        return HealthRow(name="recent_events", status="ok", detail="no recent events")
    errors = sum(1 for row in events if row.is_error)
    if errors:
        return HealthRow(
            name="recent_events",
            status="ok",
            detail=f"{len(events)} recent event(s); {errors} error(s) in window",
        )
    return HealthRow(
        name="recent_events",
        status="ok",
        detail=f"{len(events)} recent event(s); none in error",
    )


def build_doctor_health(
    checks: list[CheckResult],
    drifts: list[Drift],
    events: tuple[EventRow, ...],
) -> DoctorHealth:
    """Fold the three doctor signals into one :class:`DoctorHealth`.

    Pure aggregation: takes the already-resolved doctor check results, the
    git/state drift rows, and the recent event-store tail, and folds them
    into the unified view. The rollup reuses the doctor library's own
    :func:`eawf.observability.doctor.report.overall_status` over the doctor
    checks **and** the drift summary, so the in-TUI rollup matches the CLI
    exit-code logic (a drift is a ``warn``-level signal there too). The
    events row is informational and does not enter the rollup.

    The doctor check results are reused verbatim -- this function reads the
    ``CheckResult`` rows produced by
    :func:`eawf.observability.doctor.checks.run_all`; it does NOT re-run or
    reimplement any check.

    Args:
        checks: The doctor check results from
            :func:`eawf.observability.doctor.checks.run_all`.
        drifts: The git/state drift rows from
            :func:`~eawf.workflow.lifecycle.wave_sha.detect_git_state_drift`.
        events: The recent event-store tail from
            :func:`~eawf.surfaces.tui.screens.overlays.events.load_recent_events`.

    Returns:
        The folded :class:`DoctorHealth`, rows in render order, with the
        rollup and the drift / event summary counts populated.
    """
    from eawf.observability.doctor.checks import CheckResult
    from eawf.observability.doctor.report import overall_status

    rows: list[HealthRow] = [
        HealthRow(name=c.name, status=c.status, detail=c.detail or "") for c in checks
    ]
    drift_row = _drift_summary_row(drifts)
    events_row = _events_summary_row(events)
    rows.append(drift_row)
    rows.append(events_row)

    # Roll up via the doctor library's own status reducer so the TUI verdict
    # matches the CLI exit-code logic. Feed it the doctor checks plus the
    # drift summary (re-expressed as a CheckResult so the reducer's
    # highest-severity rule applies uniformly); the informational events row
    # is excluded so an old log error never flips the live install verdict.
    rollup_inputs = [*checks, CheckResult(name=drift_row.name, status=drift_row.status)]
    overall: HealthStatus = overall_status(rollup_inputs)  # type: ignore[assignment]

    drift_kinds = sorted({str(d.kind) for d in drifts})
    event_error_count = sum(1 for row in events if row.is_error)
    logger.info(
        f"build_doctor_health checks={len(checks)} drifts={len(drifts)} "
        f"events={len(events)} overall={overall}"
    )
    return DoctorHealth(
        rows=rows,
        overall=overall,
        drift_count=len(drifts),
        drift_kinds=drift_kinds,
        event_error_count=event_error_count,
    )


def _resolve_event_path(state_path: Path | None) -> Path | None:
    """Resolve the scope's event-store path from its ``state.json`` path.

    Returns ``None`` when no scope state is resolved (the user scope / a
    fresh workspace), so the events fold degrades to an honest "no recent
    events" rather than raising.

    Args:
        state_path: The scope's ``state.json`` path, or ``None``.

    Returns:
        The ``<state_dir>/store/event.jsonl`` path, or ``None``.
    """
    if state_path is None:
        return None
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.paths import store_path

    return store_path(state_path, StoreKind.EVENT)


def gather_doctor_health(*, workspace: Path | None, state_path: Path | None) -> DoctorHealth:
    """Resolve every doctor signal for *workspace* and fold them.

    The single impure entry point: it runs the doctor check library, the
    git/state drift reconciler, and the recent event-store read, then hands
    the resolved values to the pure :func:`build_doctor_health`. Each source
    is total -- the check library degrades a missing / unparseable state to
    ``ok`` rows, the drift reconciler returns an empty list when git is
    unavailable or state is absent, and the events read returns an empty
    tuple for a missing store -- so the gather never raises out of the
    render loop.

    Args:
        workspace: The workspace anchor the doctor checks resolve against
            (``app._state_path.parent.parent`` for a repo scope, or
            ``None``).
        state_path: The scope's ``state.json`` path, used to locate the
            event store and the drift reconciler's ``repo_root``.

    Returns:
        The folded :class:`DoctorHealth` for the resolved scope.
    """
    from eawf.observability.doctor import checks as doctor_checks
    from eawf.surfaces.tui.screens.overlays.events import load_recent_events

    try:
        check_results = doctor_checks.run_all(workspace=workspace)
    except Exception as exc:
        # The hard-probe path raises UserError (kind="InstrumentMissing")
        # in the CLI to drive exit code 6; in the read-only TUI we never
        # abort the render loop -- surface the hard miss as a single FAIL
        # row so the pane still paints.
        logger.warning(f"gather_doctor_health status=checks-failed error={exc!r}")
        from eawf.observability.doctor.checks import CheckResult

        check_results = [
            CheckResult(name="tools_available", status="fail", detail=f"probe failed: {exc}")
        ]

    drifts = _gather_drifts(state_path)

    event_path = _resolve_event_path(state_path)
    events = load_recent_events(event_path, limit=_EVENT_SAMPLE)

    return build_doctor_health(check_results, drifts, events)


def _gather_drifts(state_path: Path | None) -> list[Drift]:
    """Resolve the git/state drift rows for the scope's ``state.json``.

    Loads + validates the state at *state_path* and runs
    :func:`~eawf.workflow.lifecycle.wave_sha.detect_git_state_drift` against
    it, scoped to the repo root (the state file's grandparent, mirroring
    the CLI ``_run_git_state_drift_check``). Total: a missing / unparseable
    state yields an empty list so the drift fold degrades to ``ok``.

    Args:
        state_path: The scope's ``state.json`` path, or ``None``.

    Returns:
        The drift rows, or an empty list when state is absent / unreadable.
    """
    if state_path is None or not state_path.is_file():
        return []
    import orjson
    from pydantic import ValidationError

    from eawf.kernel.state.models import State
    from eawf.workflow.lifecycle.wave_sha import detect_git_state_drift

    try:
        raw = orjson.loads(state_path.read_bytes())
        state = State.model_validate(raw)
    except (orjson.JSONDecodeError, ValidationError, OSError) as exc:
        logger.debug(f"_gather_drifts status=state-unreadable error={exc!r}")
        return []
    repo_root = state_path.parent.parent
    return detect_git_state_drift(state, repo_root=repo_root)


def _status_glyph_line(name: str, status: HealthStatus, detail: str) -> str:
    """Render one health row as a ``<GLYPH>  name  detail`` line.

    The glyph is tinted by status via a content-markup span; the name + the
    (markup-escaped) detail render plain so an event summary or a wave id in
    the detail never parses as a style tag.

    Args:
        name: The row's machine identifier.
        status: The row's status (drives the glyph + its colour).
        detail: The row's human detail (markup-escaped).

    Returns:
        A content-markup line for a single :class:`~textual.widgets.Static`.
    """
    glyph = _STATUS_GLYPH[status]
    colour = {"ok": "$success", "warn": "$warning", "fail": "$error"}[status]
    body = f"{name:<26}  {escape_markup(detail)}" if detail else f"{name}"
    return f"[{colour}]{glyph:<4}[/] {body}"


def render_health_lines(health: DoctorHealth) -> list[str]:
    """Render a :class:`DoctorHealth` into the pane's content-markup lines.

    Pure helper so the rendered text is unit-testable without mounting the
    screen. The body opens with a rollup title (``Health: <word>``), then
    one glyph line per signal row, then -- when the reconciler surfaced any
    drift -- a DRIFT block naming the count + the distinct kinds (capped at
    :data:`_DRIFT_ROW_CAP`). An all-ok / honest-empty health renders the
    rollup + the rows with no DRIFT block.

    Args:
        health: The folded health view.

    Returns:
        The ordered content-markup lines (one per
        :class:`~textual.widgets.Static`).
    """
    word = _ROLLUP_WORD[health.overall]
    colour = {"ok": "$success", "warn": "$warning", "fail": "$error"}[health.overall]
    lines: list[str] = [f"[$accent]Health:[/] [{colour}]{word}[/]", ""]
    lines.extend(_status_glyph_line(row.name, row.status, row.detail) for row in health.rows)
    if health.drift_count:
        lines.append("")
        lines.append(style_labeled_line(f"drift:  {health.drift_count} wave(s)"))
        kinds = health.drift_kinds[:_DRIFT_ROW_CAP]
        lines.append(style_labeled_line(f"kinds:  {', '.join(kinds)}"))
    return lines


class DoctorModeScreen(ScopeScreen):
    """Doctor-mode screen: the folded install / state / drift health view.

    Composes a single scrollable health pane inside the shared chassis
    (Header + Footer inherited from :class:`ScopeScreen`). On mount it
    gathers every doctor signal -- the doctor check library, the git/state
    drift reconciler, and the recent event-store tail -- and renders the
    folded :class:`DoctorHealth`. Read-only this wave: the pane reflects the
    health at mount; a force-refresh re-gathers via :meth:`refresh_health`.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _DOCTOR_HINTS

    DEFAULT_CSS: ClassVar[str] = """
    DoctorModeScreen #doctor-health {
        height: 1fr;
        padding: 1 2;
    }
    DoctorModeScreen .doctor-health-body {
        height: auto;
    }
    """

    def compose_body(self) -> ComposeResult:
        """Yield the scrollable health pane body."""
        with VerticalScroll(id="doctor-health"), Vertical(classes="doctor-health-body"):
            yield Static("", id="doctor-health-text")

    def on_mount(self) -> None:
        """Apply the footer hints, then gather + render the health view."""
        super().on_mount()
        self.refresh_health()

    def refresh_health(self) -> None:
        """Re-gather every doctor signal and repaint the pane.

        Resolves the workspace + state path off the host app, gathers the
        folded health via :func:`gather_doctor_health`, and updates the
        pane text. Total -- the gather never raises -- so a force-refresh
        cannot tear the render loop.
        """
        state_path = getattr(self.app, "_state_path", None)
        workspace = state_path.parent.parent if state_path is not None else None
        health = gather_doctor_health(workspace=workspace, state_path=state_path)
        self._paint_health(health)

    def _paint_health(self, health: DoctorHealth) -> None:
        """Update the pane text from a folded :class:`DoctorHealth`.

        Named to avoid shadowing Textual's internal ``Screen._render`` (a
        zero-arg compositor hook); this is the pane's own repaint entry.
        """
        text = self.query_one("#doctor-health-text", Static)
        text.update("\n".join(render_health_lines(health)))

    def action_force_refresh(self) -> None:
        """Re-gather the health on ``F5`` (overrides the chassis heartbeat ack)."""
        super().action_force_refresh()
        self.refresh_health()


def doctor_mode_factory(_app: EaApp) -> DoctorModeScreen:
    """Build the Doctor-mode screen (the registry factory).

    The mode is scope-independent -- it renders the same folded health
    regardless of the active scope -- so the app argument is ignored.

    Args:
        _app: The live app (unused; the pane resolves state off ``self.app``
            at mount time).

    Returns:
        A fresh :class:`DoctorModeScreen`.
    """
    return DoctorModeScreen()


__all__ = [
    "DoctorHealth",
    "DoctorModeScreen",
    "HealthRow",
    "HealthStatus",
    "build_doctor_health",
    "doctor_mode_factory",
    "gather_doctor_health",
    "render_health_lines",
]
