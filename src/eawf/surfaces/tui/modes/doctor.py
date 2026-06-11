"""``DoctorModeScreen`` -- the Doctor-mode health view (P29-I02-W21, TUI-3).

The Doctor mode (digit ``5``) folds the same health signals ``eawf doctor``
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

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup, style_labeled_line
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph, tint

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

#: The render-mode label threaded into the sigil helpers when the host App
#: exposes no ``render_mode`` (a bare standalone render of the pure lines).
#: The unicode column is the default surface; ``"ascii"`` only when the App
#: resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"


def _status_mark(status: HealthStatus, *, mode: str) -> str:
    """Return the tinted status-sigil content-markup span for *status*.

    The shape resolves through the single
    :mod:`~eawf.surfaces.tui.widgets.sigils` home so the pane invents no
    glyph: ``ok`` wears the CLOSED lifecycle sigil (filled circle),
    ``fail`` wears the FAILED sigil (multiplication cross), and ``warn``
    wears the ``attention`` chrome triangle -- shape-distinct from the
    PENDING ring so a degraded check never reads as a not-yet-run one. The
    ok/fail tints come from the sigil's own Wong hex; warn wears the band's
    ``$warning`` palette var.

    Args:
        status: The row's status (drives both the glyph and its colour).
        mode: The App's resolved render-mode label (``"ascii"`` or unicode).

    Returns:
        The single-cell tinted glyph content-markup span for *status*.
    """
    if status == "ok":
        mark = glyph(Sigil.CLOSED, mode=mode)
        hex_tint = tint(Sigil.CLOSED)
        return f"[{hex_tint}]{mark}[/]" if hex_tint else mark
    if status == "fail":
        mark = glyph(Sigil.FAILED, mode=mode)
        hex_tint = tint(Sigil.FAILED)
        return f"[{hex_tint}]{mark}[/]" if hex_tint else mark
    mark = chrome("attention", mode=mode)
    return f"[$warning]{mark}[/]"


#: The human rollup word for each overall status, shown in the pane title.
_ROLLUP_WORD: dict[HealthStatus, str] = {
    "ok": "healthy",
    "warn": "degraded",
    "fail": "unhealthy",
}

#: The middle-dot (U+00B7) joiner between the check count and the warn count
#: in the rollup summary -- the reskin's pinned ``N checks · M warn`` form (the
#: same middle-dot the metrics / events / audit overlays separate inline facts
#: with), not the bare comma the pre-reskin header carried.
_SUMMARY_SEP: str = " · "

#: Per-row section assignment for the install / state / drift grouping.
#: Every signal the pane renders maps to exactly one section so the body
#: reads top-to-bottom as install health, then state health, then drift.
#: The drift section carries the manifest / MCP / git-state-drift rows (the
#: three reconciler signals); the events tail rides under state.
_SECTION_OF: dict[str, str] = {
    # install -- the toolchain + config + render round-trip probes
    "tools_available": "install",
    "config_resolves": "install",
    "render_output_roundtrip": "install",
    # state -- the state.json presence + scale + recent-events signals
    "state_present": "state",
    "state_scale_ceiling": "state",
    "recent_events": "state",
    # drift -- the manifest / MCP / git-state reconciler signals
    "manifest_in_sync": "drift",
    "mcp_drift": "drift",
    "git_state_drift": "drift",
}

#: The section render order and the header each section block carries. A
#: row whose name is unknown to :data:`_SECTION_OF` falls into ``install``
#: so a future doctor check still renders rather than vanishing.
_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("install", "Install"),
    ("state", "State"),
    ("drift", "Drift"),
)

#: Cap on the per-wave drift rows surfaced in the pane body; the full list
#: is recoverable via ``eawf --json doctor``. Keeps the body bounded when a
#: stale branch carries many unreconciled closed waves.
_DRIFT_ROW_CAP: int = 8

#: How many recent events the pane samples from the on-disk event store
#: tail when summarising the error count. Matches the ``/events`` overlay
#: ring so both surfaces read the same window.
_EVENT_SAMPLE: int = 50

#: Placeholder painted the instant the pane mounts, before the health
#: gather worker resolves. The gather runs blocking subprocesses (the
#: doctor tool-probes and a per-wave ``git log`` drift scan), so it is
#: offloaded to a worker; this honest line shows immediately so the pane
#: never appears frozen while the worker runs off the UI thread.
_GATHERING_PLACEHOLDER: str = "[$accent]Health:[/] gathering health..."

#: Worker group for the health gather, so a force-refresh re-kick cancels
#: any in-flight gather (``exclusive=True``) rather than stacking workers.
_GATHER_GROUP: str = "doctor-health"

#: Footer hints for the Doctor mode -- the always-live chassis affordances
#: (scope switch, palette, help, quit). The pane is read-only this wave (no
#: in-pane keys beyond the chassis), so no extra hints. The mode digits are
#: surfaced by the always-visible mode row, not duplicated in the hint strip.
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_DOCTOR_HINTS: tuple[str, ...] = (
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
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


def _status_glyph_line(name: str, status: HealthStatus, detail: str, *, mode: str) -> str:
    """Render one health row as a ``<sigil>  name  detail`` line.

    The leading mark is the status sigil from the shared sigils home
    (ok=closed circle, warn=attention triangle, fail=failed cross),
    tinted by status via a content-markup span; the name + the
    (markup-escaped) detail render plain so an event summary or a wave id in
    the detail never parses as a style tag.

    Args:
        name: The row's machine identifier.
        status: The row's status (drives the sigil + its colour).
        detail: The row's human detail (markup-escaped).
        mode: The App's resolved render-mode label (``"ascii"`` or unicode).

    Returns:
        A content-markup line for a single :class:`~textual.widgets.Static`.
    """
    mark = _status_mark(status, mode=mode)
    body = f"{name:<26}  {escape_markup(detail)}" if detail else f"{name}"
    return f"  {mark} {body}"


def _group_rows(rows: list[HealthRow]) -> dict[str, list[HealthRow]]:
    """Partition health rows into the install / state / drift sections.

    Every row maps to exactly one section via :data:`_SECTION_OF`; an
    unknown row name falls into ``install`` so a future doctor check still
    renders rather than vanishing. Within each section the rows keep their
    incoming render order.

    Args:
        rows: The folded health rows, in render order.

    Returns:
        A section-keyed mapping of the rows belonging to each section.
    """
    grouped: dict[str, list[HealthRow]] = {section: [] for section, _ in _SECTION_ORDER}
    for row in rows:
        section = _SECTION_OF.get(row.name, "install")
        grouped[section].append(row)
    return grouped


def render_health_lines(health: DoctorHealth, *, mode: str = _DEFAULT_RENDER_MODE) -> list[str]:
    """Render a :class:`DoctorHealth` into the pane's content-markup lines.

    Pure helper so the rendered text is unit-testable without mounting the
    screen. The body opens with a rollup title (``Health: <word>``) carrying
    a ``N checks, M warn`` summary, then the signal rows grouped under the
    install / state / drift section headers (each section drawing only its
    own rows). When the reconciler surfaced any drift, a DRIFT block names
    the count + the distinct kinds (capped at :data:`_DRIFT_ROW_CAP`). An
    all-ok / honest-empty health renders the rollup + the sections with no
    DRIFT block. Each row's leading mark is the status sigil from the shared
    :mod:`~eawf.surfaces.tui.widgets.sigils` home, resolved in *mode*.

    Args:
        health: The folded health view.
        mode: The App's resolved render-mode label (``"ascii"`` or unicode)
            threaded into the sigil helpers; defaults to the unicode column.

    Returns:
        The ordered content-markup lines (one per
        :class:`~textual.widgets.Static`).
    """
    word = _ROLLUP_WORD[health.overall]
    colour = {"ok": "$success", "warn": "$warning", "fail": "$error"}[health.overall]
    check_count = len(health.rows)
    warn_count = sum(1 for row in health.rows if row.status == "warn")
    summary = f"[$text-muted]{check_count} checks{_SUMMARY_SEP}{warn_count} warn[/]"
    lines: list[str] = [f"[$accent]Health:[/] [{colour}]{word}[/]  {summary}", ""]

    grouped = _group_rows(health.rows)
    for section, title in _SECTION_ORDER:
        section_rows = grouped[section]
        if not section_rows:
            continue
        lines.append(f"[$accent]{title}[/]")
        lines.extend(
            _status_glyph_line(row.name, row.status, row.detail, mode=mode) for row in section_rows
        )
        lines.append("")
    # Drop the trailing separator the last section appended so the optional
    # DRIFT block (or the body end) abuts the rows cleanly.
    if lines and lines[-1] == "":
        lines.pop()

    if health.drift_count:
        lines.append("")
        lines.append(style_labeled_line(f"drift:  {health.drift_count} wave(s)"))
        kinds = health.drift_kinds[:_DRIFT_ROW_CAP]
        lines.append(style_labeled_line(f"kinds:  {', '.join(kinds)}"))
    return lines


def render_health_rollup(health: DoctorHealth) -> str:
    """Render the ``Health: <word>  N checks, M warn`` rollup line."""
    word = _ROLLUP_WORD[health.overall]
    colour = {"ok": "$success", "warn": "$warning", "fail": "$error"}[health.overall]
    warn_count = sum(1 for row in health.rows if row.status == "warn")
    summary = f"[$text-muted]{len(health.rows)} checks{_SUMMARY_SEP}{warn_count} warn[/]"
    return f"[$accent]Health:[/] [{colour}]{word}[/]  {summary}"


def render_health_section_lines(
    health: DoctorHealth, section: str, *, mode: str = _DEFAULT_RENDER_MODE
) -> list[str]:
    """Render one install / state / drift section's rows (no header line).

    The section header is the host card's ``border_title``, so this returns only
    the status-glyph rows; the ``drift`` section additionally appends the
    drift-count + kinds block. An empty section renders an honest muted line.
    """
    rows = _group_rows(health.rows).get(section, [])
    lines = [_status_glyph_line(row.name, row.status, row.detail, mode=mode) for row in rows]
    if section == "drift" and health.drift_count:
        if lines:
            lines.append("")
        lines.append(style_labeled_line(f"drift:  {health.drift_count} wave(s)"))
        kinds = health.drift_kinds[:_DRIFT_ROW_CAP]
        lines.append(style_labeled_line(f"kinds:  {', '.join(kinds)}"))
    return lines or ["[$text-muted]no checks[/]"]


class DoctorModeScreen(ScopeScreen):
    """Doctor-mode screen: the folded install / state / drift health view.

    Composes a single scrollable health pane inside the shared chassis
    (Header + Footer inherited from :class:`ScopeScreen`). On mount it
    paints an immediate ``gathering health...`` placeholder, then kicks a
    worker that gathers every doctor signal -- the doctor check library,
    the git/state drift reconciler, and the recent event-store tail -- and
    repaints with the folded :class:`DoctorHealth` when the worker returns.

    The gather is offloaded to a worker (mirroring
    :class:`~eawf.surfaces.tui.widgets.git_pane.GitPane`) because
    :func:`gather_doctor_health` runs blocking subprocesses: the doctor
    tool-probes and a per-wave ``git log`` drift scan that, over a state
    with hundreds of closed waves, would otherwise freeze the whole event
    loop. The worker keeps the UI responsive while the gather runs.

    Read-only this wave: the pane reflects the health at mount; a
    force-refresh re-gathers via :meth:`refresh_health`. The worker is
    ``exclusive`` within :data:`_GATHER_GROUP`, so a force-refresh cancels
    any in-flight gather and re-kicks rather than stacking workers.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _DOCTOR_HINTS

    DEFAULT_CSS: ClassVar[str] = """
    DoctorModeScreen #doctor-health {
        height: 1fr;
        padding: 1 2;
    }
    DoctorModeScreen .doctor-rollup {
        height: auto;
        margin-bottom: 1;
    }
    DoctorModeScreen .doctor-cards {
        height: auto;
        layout: horizontal;
    }
    DoctorModeScreen .doctor-card {
        border: round $accent;
        padding: 0 1;
        height: auto;
        width: 1fr;
        margin-right: 1;
        margin-bottom: 1;
    }
    """

    def compose_body(self) -> ComposeResult:
        """Yield the rollup + install|state two-column cards + drift card."""
        with VerticalScroll(id="doctor-health"):
            yield Static("", id="doctor-rollup", classes="doctor-rollup")
            with Horizontal(classes="doctor-cards"):
                for section, title in _SECTION_ORDER:
                    if section == "drift":
                        continue
                    card = Static("", id=f"doctor-{section}", classes="doctor-card")
                    card.border_title = title.upper()
                    yield card
            drift_card = Static("", id="doctor-drift", classes="doctor-card")
            drift_card.border_title = "DRIFT"
            yield drift_card

    def on_mount(self) -> None:
        """Apply the footer hints, paint a placeholder, then kick the gather."""
        super().on_mount()
        self._paint_placeholder()
        self.refresh_health()

    def refresh_health(self) -> None:
        """Kick the health gather off the UI thread and repaint when it returns.

        :func:`gather_doctor_health` runs blocking subprocesses (the doctor
        tool-probes and a per-wave ``git log`` drift scan), so running it on
        the event loop would freeze the whole app over a many-wave state.
        Running it in a worker keeps the UI responsive; the pane repaints
        with the folded health when the worker resolves. ``exclusive``
        within :data:`_GATHER_GROUP` drops any in-flight gather so a
        force-refresh re-kick coalesces rather than stacking workers.
        """
        self.run_worker(self._gather_health(), group=_GATHER_GROUP, exclusive=True)

    async def _gather_health(self) -> None:
        """Worker body: gather the health off-thread, then repaint on the loop.

        :func:`gather_doctor_health` is a synchronous blocking call, so it
        runs inside :func:`asyncio.to_thread` (the same mechanism
        :class:`~eawf.surfaces.tui.widgets.git_pane.GitPane` uses to wrap
        its sync ``git`` probe) -- the event loop is never blocked. The
        repaint runs after the ``await`` on the worker's own coroutine,
        which lives on the event loop, so the pane mutation is loop-safe
        without an explicit ``call_from_thread``.
        """
        state_path = getattr(self.app, "_state_path", None)
        workspace = state_path.parent.parent if state_path is not None else None
        health = await asyncio.to_thread(
            gather_doctor_health, workspace=workspace, state_path=state_path
        )
        self._paint_health(health)

    def _paint_placeholder(self) -> None:
        """Paint the instant ``gathering health...`` line before the worker resolves.

        Shows the pane immediately on mount so it never appears frozen
        while the gather worker runs off the UI thread; the real folded
        health replaces it via :meth:`_paint_health` when the worker
        returns.
        """
        self.query_one("#doctor-rollup", Static).update(_GATHERING_PLACEHOLDER)

    def _paint_health(self, health: DoctorHealth) -> None:
        """Update the pane text from a folded :class:`DoctorHealth`.

        Named to avoid shadowing Textual's internal ``Screen._render`` (a
        zero-arg compositor hook); this is the pane's own repaint entry. The
        active render mode is threaded into the sigil helpers so a
        unicode <-> ASCII flip paints the right glyph column; a bare test
        harness whose App carries no ``render_mode`` falls back to unicode.
        """
        mode = getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)
        self.query_one("#doctor-rollup", Static).update(render_health_rollup(health))
        for section, _title in _SECTION_ORDER:
            card = self.query_one(f"#doctor-{section}", Static)
            card.update("\n".join(render_health_section_lines(health, section, mode=mode)))

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
