"""``MetricsModal`` — the V7 ``/metrics`` 4x2 dashboard overlay.

The ``/metrics`` palette verb opens a 4x2 grid of metric tiles backed by
the daemon's telemetry projection. The seven tiles, in grid order, are:

1. **Precision** (top row) — estimate-vs-actual EU delta per effort bucket
   (labelled "precision" on the surface; "variance" misread as statistical).
2. **Weekly burn** (top row) — actual EU vs the project's weekly target.
3. **Wave elapsed** (top row) — median + p90 wall-clock per closed wave.
4. **Cost** (top row) — summed priced session cost over the window; the
   dashboard mirror of the wave-detail ``$`` tab's aggregate.
5. **Cache health** (bottom row) — cache-create vs cache-read token ratio.
6. **Switchover frequency** (bottom row) — ``runtime_switched`` counts
   per cause over the rolling window.
7. **Role calibration** (bottom row) — per-agent-role bucket fit grid.

The modal computes the projection from the current read-only state snapshot
and, when present, the local telemetry DB. State-backed tiles still render
when the telemetry DB is absent; telemetry-backed tiles (including Cost)
show an honest no-data sentinel until the projector has written rows. The
refresh cadence stays ``set_interval(5.0, ...)`` so the same surface can
switch to daemon-push later without changing the visible contract.

The tile inventory + grid order are a pure module-level table
(:data:`TILE_SPECS`) so the composition is unit-testable without mounting
Textual, and the modal stays a thin view over it. The window argument
(``7d`` / ``30d`` / ``90d``) and the optional scope filter are parsed from
the ``/metrics`` verb args by :func:`parse_metrics_args` (also pure).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.observability.telemetry.metrics_projection import (
    MetricsProjection,
    MetricsWindow,
    RoleCalibrationProjection,
    compute_metrics_projection,
)
from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.store import metrics_db_path, open_store
from eawf.observability.telemetry.store.base import AbstractMetricsStore
from eawf.surfaces.tui.screens.overlays.detail_cost import (
    aggregate_session_cost,
    render_cost_tile,
)
from eawf.surfaces.tui.widgets.calibration_table import (
    render_role_calibration_drilldown,
    render_role_calibration_tile,
)
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.sigils import chrome
from eawf.surfaces.tui.widgets.variance_tile import render_variance_markup
from eawf.workflow.estimation.metrics import compute_wave_elapsed

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The rolling-window tokens the ``/metrics --window`` flag accepts.
METRIC_WINDOWS: tuple[MetricsWindow, ...] = ("7d", "30d", "90d")

#: Default rolling window when ``--window`` is omitted (7-day weekly cadence).
DEFAULT_WINDOW: MetricsWindow = "7d"

#: Telemetry-projection refresh cadence in seconds (5 s tick).
METRICS_REFRESH_S: float = 5.0

#: The frozen honest-negative footer the dashboard pins until EU capture lands
#: (I04). The middle dot (U+00B7) is string DATA -- it separates the
#: honest-negative banner from its "lights up after EU capture" promise.
#: Phrased so the empty dashboard reads as "not measured yet", never a
#: fabricated metric: the outer eawf harness does not yet instrument EU, so
#: every telemetry-backed tile is honestly dark until the capture wave lands.
METRICS_HONEST_NEGATIVE: str = "honest-negative · lights up after EU capture"

#: Footer shown once at least one wave has captured runtime EU: the
#: telemetry tiles are now backed by measured data, so the dashboard drops
#: the honest-negative banner rather than pinning it forever.
METRICS_CAPTURE_LIVE: str = "EU capture live · telemetry tiles measured"


def _eu_capture_landed(state: State | None) -> bool:
    """Return whether any wave has captured a positive runtime ``elapsed_eu``.

    The dashboard is honestly dark until the SessionEnd hook feeds the close
    path real runtime; once any wave's
    :class:`~eawf.kernel.state.models.ActualSummary` records a positive
    ``elapsed_eu`` the tiles are measured, so the footer flips from the
    honest-negative banner to the capture-live affirmation.

    Args:
        state: The bound scope state, or ``None`` before it loads.

    Returns:
        ``True`` when at least one actual records ``elapsed_eu > 0``.
    """
    if state is None:
        return False
    return any(actual.elapsed_eu > 0.0 for actual in (state.actuals or {}).values())


#: Placeholder body when no state snapshot is available yet.
_AWAITING: str = "[$text-muted]awaiting telemetry projection[/]"

#: Empty-state body when the projection exists but a telemetry family has no rows.
_NO_DATA: str = "[$text-muted]no data[/]"


@dataclass(frozen=True)
class TileSpec:
    """One metric tile's static spec (title + stable widget id).

    Attributes:
        tile_id: The tile widget id (``tile-<slug>``), also the grid-order
            anchor and the drill-target key.
        title: The tile heading rendered at the top of the cell.
        drill: Optional drilldown key opened by ``Enter``.
    """

    tile_id: str
    title: str
    drill: str | None = None


#: The 4x2 tile inventory in grid order (row-major: top-left → bottom-
#: right). The grid is ``grid-size: 4 2`` so the first four specs fill the
#: top row and the next three the bottom row (the eighth cell is empty).
#: The Cost tile (``tile-cost``) carries the wave-detail ``$`` tab's
#: aggregate so the dashboard and the per-wave cost view agree.
TILE_SPECS: tuple[TileSpec, ...] = (
    TileSpec("tile-variance", "Precision / bucket", drill="variance"),
    TileSpec("tile-burn", "Weekly burn"),
    TileSpec("tile-elapsed", "Wave elapsed"),
    TileSpec("tile-cost", "Cost"),
    TileSpec("tile-cache", "Cache health"),
    TileSpec("tile-switchover", "Switchover freq"),
    TileSpec("tile-role-calibration", "Role calibration", drill="role-calibration"),
)


@dataclass(frozen=True)
class MetricsArgs:
    """Parsed ``/metrics`` verb arguments.

    Attributes:
        window: The rolling window — one of :data:`METRIC_WINDOWS`.
        scope_filter: An optional scope urn to filter the tiles to, or
            ``None`` for the current scope.
    """

    window: MetricsWindow
    scope_filter: str | None


def _format_minutes(value: float) -> str:
    """Return a compact minute value for metric tiles."""
    return f"{value:.1f}m"


def _format_eu(value: float) -> str:
    """Return a compact EU value for metric tiles."""
    return f"{value:.2f} EU"


def _format_ratio(value: float) -> str:
    """Return a compact percentage for ratios."""
    return f"{value * 100.0:.0f}%"


def _format_tokens(value: int) -> str:
    """Return a compact token count."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def render_wave_elapsed_tile(state: State | None) -> str:
    """Render the elapsed-wave tile from :func:`compute_wave_elapsed`."""
    if state is None:
        return _AWAITING
    metric = compute_wave_elapsed(state)
    return "\n".join(
        [
            f"median {_format_minutes(metric.median_minutes)}",
            f"mean {_format_minutes(metric.mean_minutes)}",
            f"max {_format_minutes(metric.max_minutes)}",
            f"samples {metric.sample_count}",
        ]
    )


def render_projection_tile(projection: MetricsProjection | None, tile_id: str) -> str:
    """Render one dashboard tile from a computed projection."""
    if projection is None:
        return _AWAITING
    if tile_id == "tile-variance":
        return _render_variance_projection(projection)
    if tile_id == "tile-burn":
        return _render_weekly_burn_projection(projection)
    if tile_id == "tile-elapsed":
        return _render_elapsed_projection(projection)
    if tile_id == "tile-cache":
        return _render_cache_projection(projection)
    if tile_id == "tile-switchover":
        return _render_switchover_projection(projection)
    if tile_id == "tile-role-calibration":
        return _render_role_calibration_projection(projection)
    if tile_id == "tile-tokens":
        return _render_tokens_projection(projection)
    return _NO_DATA


def _render_variance_projection(projection: MetricsProjection) -> str:
    """Render the estimate-actual variance tile."""
    metric = projection.variance
    if metric.sample_count == 0:
        return _NO_DATA
    lines = [
        f"delta {render_variance_markup(metric.variance_pct)}",
        f"actual {_format_eu(metric.actual_eu)}",
        f"planned {_format_eu(metric.planned_eu)}",
    ]
    for row in projection.variance_by_bucket[:2]:
        # Signed precision % per bucket, no sample-count suffix (the wave count
        # is already surfaced elsewhere; n= here read as statistical noise).
        lines.append(f"{row.bucket.value} {render_variance_markup(row.variance_pct)}")
    return "\n".join(lines)


def _render_weekly_burn_projection(projection: MetricsProjection) -> str:
    """Render the weekly-burn tile."""
    metric = projection.weekly_burn
    lines = [
        f"consumed {_format_eu(metric.consumed_eu)}",
        f"window {metric.window_days}d",
    ]
    if metric.target_eu is None:
        lines.append("target unset")
    else:
        ratio = metric.consumed_eu / metric.target_eu if metric.target_eu > 0 else 0.0
        lines.append(f"target {_format_eu(metric.target_eu)}")
        lines.append(f"used {_format_ratio(ratio)}")
    return "\n".join(lines)


def _render_elapsed_projection(projection: MetricsProjection) -> str:
    """Render the wave-elapsed tile."""
    metric = projection.wave_elapsed
    return "\n".join(
        [
            f"median {_format_minutes(metric.median_minutes)}",
            f"mean {_format_minutes(metric.mean_minutes)}",
            f"max {_format_minutes(metric.max_minutes)}",
            f"samples {metric.sample_count}",
        ]
    )


def _render_cache_projection(projection: MetricsProjection) -> str:
    """Render the cache-health tile."""
    if not projection.cache_health:
        return _NO_DATA
    lines: list[str] = []
    for row in projection.cache_health[:3]:
        lines.append(
            f"{row.runtime} {_format_ratio(row.hit_ratio)} "
            f"r:{_format_tokens(row.cache_read_tokens)} "
            f"c:{_format_tokens(row.cache_create_tokens)}"
        )
    return "\n".join(lines)


def _render_switchover_projection(projection: MetricsProjection) -> str:
    """Render the switchover-frequency tile."""
    if not projection.switchover_frequency:
        return _NO_DATA
    lines = [f"{row.cause.value} {row.count}" for row in projection.switchover_frequency[:4]]
    return "\n".join(lines)


def _render_tokens_projection(projection: MetricsProjection) -> str:
    """Render the per-runtime-token tile."""
    if not projection.per_runtime_tokens:
        return _NO_DATA
    lines: list[str] = []
    for row in projection.per_runtime_tokens[:3]:
        lines.append(f"{row.runtime} {_format_tokens(row.total_tokens)} tok")
    return "\n".join(lines)


def _render_role_calibration_projection(projection: MetricsProjection) -> str:
    """Render the per-agent-role bucket fit grid."""
    return render_role_calibration_tile(projection.per_role_calibration)


def render_variance_drilldown(projection: MetricsProjection | None) -> str:
    """Render bucket-level variance drilldown rows."""
    if projection is None:
        return _AWAITING
    if not projection.variance_by_bucket:
        return _NO_DATA
    lines: list[str] = []
    for bucket in projection.variance_by_bucket:
        lines.append(
            f"{bucket.bucket.value} {render_variance_markup(bucket.variance_pct)} "
            f"n={bucket.sample_count} planned={_format_eu(bucket.planned_eu)} "
            f"actual={_format_eu(bucket.actual_eu)}"
        )
        for wave in bucket.waves[:5]:
            lines.append(
                f"  {wave.wave_id} {render_variance_markup(wave.variance_pct)} "
                f"planned={_format_eu(wave.planned_eu)} actual={_format_eu(wave.actual_eu)} "
                f"{wave.title}"
            )
        if len(bucket.waves) > 5:
            lines.append(f"  +{len(bucket.waves) - 5} more")
    return "\n".join(lines)


def parse_metrics_args(args: str) -> MetricsArgs:
    """Parse the raw ``/metrics`` arg string into a typed :class:`MetricsArgs`.

    Recognises ``--window 7d|30d|90d`` (defaulting to :data:`DEFAULT_WINDOW`
    and ignoring an unrecognised value) and ``--scope <urn>`` (defaulting
    to ``None`` = the current scope). Unknown flags are ignored so a typo
    degrades to the defaults rather than raising at the palette boundary.

    Args:
        args: The raw argument string typed after ``/metrics``.

    Returns:
        The parsed window + optional scope filter.
    """
    tokens = args.split()
    window: MetricsWindow = DEFAULT_WINDOW
    scope_filter: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--window" and index + 1 < len(tokens):
            candidate = tokens[index + 1]
            if candidate in METRIC_WINDOWS:
                window = candidate
            index += 2
            continue
        if token == "--scope" and index + 1 < len(tokens):
            scope_filter = tokens[index + 1]
            index += 2
            continue
        index += 1
    return MetricsArgs(window=window, scope_filter=scope_filter)


class MetricsModal(ModalScreen[None]):
    """4x2 grid of metric tiles (Esc to close); dashboard overlay.

    Composes the seven :data:`TILE_SPECS` tiles in a ``grid-size: 4 2``
    grid, renders each tile from the current telemetry projection (the Cost
    tile reads its aggregate from the priced sessions), and arms the
    :data:`METRICS_REFRESH_S` refresh seam. Built with a pre-parsed
    :class:`MetricsArgs` so the window / scope filter come from the verb
    args, not from reaching into App state.
    """

    DEFAULT_CSS: ClassVar[str] = """
    MetricsModal {
        align: center middle;
    }
    MetricsModal > #metrics-card {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    MetricsModal .metrics-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    MetricsModal #metrics-grid {
        layout: grid;
        grid-size: 4 2;
        grid-gutter: 1;
        height: 1fr;
    }
    MetricsModal .metric-tile {
        border: round $accent;
        padding: 0 1;
        height: 1fr;
    }
    MetricsModal .metric-tile.-focused {
        border: round $primary;
    }
    MetricsModal .metrics-honest {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    MetricsModal .metrics-hint {
        color: $text-muted;
        height: 1;
    }
    """

    #: ``Esc`` closes the dashboard; arrows move tile focus; ``Enter`` opens
    #: the selected tile's drilldown when the tile exposes one.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "move(-1)", "left", show=False),
        Binding("right", "move(1)", "right", show=False),
        Binding("up", "move(-4)", "up", show=False),
        Binding("down", "move(4)", "down", show=False),
        Binding("h", "move(-1)", "left", show=False),
        Binding("l", "move(1)", "right", show=False),
        Binding("k", "move(-4)", "up", show=False),
        Binding("j", "move(4)", "down", show=False),
        Binding("enter", "drill", "drill", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index of the focused tile; starts on the sole drillable tile.
    selected: reactive[int] = reactive(5)

    def __init__(self, metrics_args: MetricsArgs | None = None) -> None:
        """Construct the dashboard for the parsed verb args.

        Args:
            metrics_args: The parsed window + scope filter; defaults to
                the :data:`DEFAULT_WINDOW`, current-scope view when the
                palette opens ``/metrics`` with no arguments.
        """
        super().__init__()
        self._args = metrics_args or MetricsArgs(window=DEFAULT_WINDOW, scope_filter=None)
        self.selected = next(
            (index for index, spec in enumerate(TILE_SPECS) if spec.drill == "role-calibration"),
            0,
        )

    def compose(self) -> ComposeResult:
        """Yield the titled card, the 4x2 tile grid, and the close hint."""
        scope_suffix = f" · {self._args.scope_filter}" if self._args.scope_filter else ""
        mode = self._render_mode()
        overview = chrome("overview", mode=mode)
        projection = self._current_projection()
        with Grid(id="metrics-card"):
            yield Static(
                f"[$accent]{overview}[/] Metrics · window {self._args.window}{scope_suffix}",
                classes="metrics-title",
            )
            with Grid(id="metrics-grid"):
                for spec in TILE_SPECS:
                    tile = Static(
                        self._tile_body(spec, projection),
                        id=spec.tile_id,
                        classes="metric-tile",
                    )
                    tile.border_title = spec.title
                    yield tile
            yield Static(
                f"[$text-muted]{self._honest_footer()}[/]",
                id="metrics-honest",
                classes="metrics-honest",
            )
            yield Static(
                "[ arrows select · Enter drill · tiles refresh every 5s · Esc close ]",
                classes="metrics-hint",
            )

    def _render_mode(self) -> RenderMode:
        """Resolve the host App's render mode, defaulting under a bare harness.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the
        sigil helpers so the chrome glyphs resolve their ASCII / unicode
        column. Falls back to :data:`DEFAULT_RENDER_MODE` under a bare test
        harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The App's ``render_mode`` when present, else
            :data:`DEFAULT_RENDER_MODE`.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _tile_body(self, spec: TileSpec, projection: MetricsProjection | None) -> str:
        """Render *spec*'s tile body from *projection*.

        The Cost tile reads its aggregate from the priced telemetry sessions
        (the same ``cost_usd`` source the wave-detail ``$`` tab quotes) rather
        than the projection, since the six-field projection carries no cost
        figure; every other tile renders from the projection.

        Args:
            spec: The tile spec to render the body for.
            projection: The current metrics projection, or ``None`` before a
                state snapshot is available.

        Returns:
            The tile's content-markup body string.
        """
        if spec.tile_id == "tile-cost":
            return self._cost_tile_body()
        return render_projection_tile(projection, spec.tile_id)

    def _cost_tile_body(self) -> str:
        """Render the Cost tile body from the metered telemetry sessions.

        Sums the priced
        :attr:`~eawf.observability.telemetry.models.TelemetrySession.total_cost_usd`
        across the stored metered sessions and routes the aggregate through
        the shared :func:`~eawf.surfaces.tui.screens.overlays.detail_cost.render_cost_tile`
        renderer -- so the tile and the wave-detail ``$`` tab agree on the
        cost figure (DRY: one aggregation home). A missing telemetry DB (or a
        read failure) yields the honest "no metered sessions yet" absence
        line rather than a fabricated ``$0``.

        Returns:
            The Cost tile's body string.
        """
        total, count = aggregate_session_cost(self._cost_sessions())
        return render_cost_tile(total, sample_count=count)

    def _cost_sessions(self) -> list[TelemetrySession]:
        """Load the metered telemetry session rows from the local store.

        Returns:
            The stored metered sessions, or an empty list when no telemetry
            DB is reachable or the read failed (the honest absence the Cost
            tile folds onto its no-data line).
        """
        store = self._metrics_store()
        if store is None:
            return []
        try:
            rows = store.fetch_all("telemetry_sessions", TelemetrySession)
        except Exception as exc:
            logger.debug(f"_cost_sessions fallback cause={exc!r}")
            return []
        finally:
            store.close()
        return [row for row in rows if isinstance(row, TelemetrySession)]

    def on_mount(self) -> None:
        """Arm the refresh seam (live once the telemetry RPC is wired).

        The interval is created now so the cadence + the refresh entry
        point are in place; :meth:`_refresh_all` short-circuits while the
        telemetry client is absent so the tiles hold their placeholder.
        """
        self.set_interval(METRICS_REFRESH_S, self._refresh_all)
        self._repaint_selection()

    def watch_selected(self) -> None:
        """Repaint the focused tile when selection changes."""
        if self.is_mounted:
            self._repaint_selection()

    def _repaint_selection(self) -> None:
        """Toggle the ``-focused`` class onto the selected tile."""
        for index, spec in enumerate(TILE_SPECS):
            tile = self.query_one(f"#{spec.tile_id}", Static)
            tile.set_class(index == self.selected, "-focused")

    def _honest_footer(self) -> str:
        """Return the footer text: honest-negative until EU capture lands.

        Flips to :data:`METRICS_CAPTURE_LIVE` once any wave has captured a
        positive runtime ``elapsed_eu`` so the dashboard stops pinning the
        dark banner after real telemetry arrives.
        """
        landed = _eu_capture_landed(self._current_state())
        return METRICS_CAPTURE_LIVE if landed else METRICS_HONEST_NEGATIVE

    def _refresh_all(self) -> None:
        """Refresh every tile from the current projection."""
        projection = self._current_projection()
        for spec in TILE_SPECS:
            tile = self.query_one(f"#{spec.tile_id}", Static)
            tile.update(self._tile_body(spec, projection))
        self.query_one("#metrics-honest", Static).update(f"[$text-muted]{self._honest_footer()}[/]")
        logger.info(
            f"metrics_refresh window={self._args.window!r} scope={self._args.scope_filter!r}"
        )

    def _current_state(self) -> State | None:
        """Return the host app's current state, if mounted and loaded."""
        try:
            state = getattr(self.app, "state", None)
        except RuntimeError:
            return None
        return state if isinstance(state, State) else None

    def _current_projection(self) -> MetricsProjection | None:
        """Return the current metrics projection, or ``None`` before state loads."""
        state = self._current_state()
        if state is None:
            return None
        store = self._metrics_store()
        try:
            return compute_metrics_projection(
                state,
                store=store,
                scope=self._args.scope_filter,
                window=self._args.window,
            )
        except Exception as exc:
            if store is None:
                raise
            logger.debug(f"_current_projection telemetry_fallback cause={exc!r}")
            return compute_metrics_projection(
                state,
                store=None,
                scope=self._args.scope_filter,
                window=self._args.window,
            )
        finally:
            if store is not None:
                store.close()

    def _metrics_store(self) -> AbstractMetricsStore | None:
        """Open the local telemetry store read-only when it already exists."""
        try:
            state_path = getattr(self.app, "_state_path", None)
        except RuntimeError:
            return None
        if not isinstance(state_path, Path):
            return None
        db_path = metrics_db_path(state_path)
        if not db_path.is_file():
            return None
        try:
            return open_store("sqlite", db_path)
        except Exception as exc:
            logger.debug(f"_metrics_store unavailable path={str(db_path)!r} cause={exc!r}")
            return None

    def action_move(self, delta: int) -> None:
        """Move tile focus by *delta*, clamped to the seven-tile grid."""
        self.selected = max(0, min(self.selected + delta, len(TILE_SPECS) - 1))

    def action_drill(self) -> None:
        """Open the selected tile's drilldown modal."""
        if not (0 <= self.selected < len(TILE_SPECS)):
            return
        spec = TILE_SPECS[self.selected]
        if spec.drill not in {"role-calibration", "variance"}:
            return
        projection = self._current_projection()
        if spec.drill == "variance":
            modal: ModalScreen[None] = VarianceDrillModal(projection, metrics_args=self._args)
        else:
            modal = CalibrationDrillModal(
                projection.per_role_calibration if projection is not None else (),
                metrics_args=self._args,
            )
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            pushed = bool(push_modal(modal))
        else:
            self.app.push_screen(modal)
            pushed = True
        logger.info(f"metrics_drill_open drill={spec.drill!r} pushed={pushed}")

    def action_close(self) -> None:
        """Dismiss the dashboard (``Esc``)."""
        self.dismiss(None)


class CalibrationDrillModal(ModalScreen[None]):
    """Full per-role calibration drilldown opened from the metrics dashboard."""

    DEFAULT_CSS: ClassVar[str] = """
    CalibrationDrillModal {
        align: center middle;
    }
    CalibrationDrillModal > #calibration-card {
        width: 80%;
        max-width: 120;
        height: 75%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    CalibrationDrillModal .calibration-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    CalibrationDrillModal .calibration-body {
        height: auto;
    }
    CalibrationDrillModal .calibration-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(
        self,
        rows: tuple[RoleCalibrationProjection, ...],
        *,
        metrics_args: MetricsArgs,
    ) -> None:
        """Construct the drilldown for precomputed role calibration rows."""
        super().__init__()
        self._rows = rows
        self._args = metrics_args

    def compose(self) -> ComposeResult:
        """Yield the title, calibration grid, details, and close hint."""
        scope_suffix = f" · {self._args.scope_filter}" if self._args.scope_filter else ""
        mode: RenderMode = getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)
        overview = chrome("overview", mode=mode)
        with VerticalScroll(id="calibration-card"):
            yield Static(
                f"[$accent]{overview}[/] Role calibration · window "
                f"{self._args.window}{scope_suffix}",
                classes="calibration-title",
            )
            yield Static(
                render_role_calibration_drilldown(self._rows, mode=mode),
                classes="calibration-body",
            )
            yield Static("[ Esc close ]", classes="calibration-hint")

    def action_close(self) -> None:
        """Dismiss the drilldown overlay."""
        self.dismiss(None)


class VarianceDrillModal(ModalScreen[None]):
    """Bucket-level estimate/actual variance drilldown."""

    DEFAULT_CSS: ClassVar[str] = """
    VarianceDrillModal {
        align: center middle;
    }
    VarianceDrillModal > #variance-card {
        width: 84%;
        max-width: 120;
        height: 75%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    VarianceDrillModal .variance-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    VarianceDrillModal .variance-body {
        height: auto;
    }
    VarianceDrillModal .variance-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(
        self,
        projection: MetricsProjection | None,
        *,
        metrics_args: MetricsArgs,
    ) -> None:
        """Construct variance drilldown for precomputed projection rows."""
        super().__init__()
        self._projection = projection
        self._args = metrics_args

    def compose(self) -> ComposeResult:
        """Yield title, variance rows, and close hint."""
        scope_suffix = f" · {self._args.scope_filter}" if self._args.scope_filter else ""
        overview = chrome("overview", mode=getattr(self.app, "render_mode", DEFAULT_RENDER_MODE))
        with VerticalScroll(id="variance-card"):
            yield Static(
                f"[$accent]{overview}[/] Variance by bucket · window "
                f"{self._args.window}{scope_suffix}",
                classes="variance-title",
            )
            yield Static(
                render_variance_drilldown(self._projection),
                classes="variance-body",
            )
            yield Static("[ Esc close ]", classes="variance-hint")

    def action_close(self) -> None:
        """Dismiss the drilldown overlay."""
        self.dismiss(None)


def open_metrics(app: App[None], metrics_args: MetricsArgs | None = None) -> bool:
    """Push the metrics dashboard onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        metrics_args: The parsed window + scope filter (defaults applied
            by :class:`MetricsModal` when ``None``).

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = MetricsModal(metrics_args)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


__all__ = [
    "DEFAULT_WINDOW",
    "METRICS_HONEST_NEGATIVE",
    "METRICS_REFRESH_S",
    "METRIC_WINDOWS",
    "TILE_SPECS",
    "CalibrationDrillModal",
    "MetricsArgs",
    "MetricsModal",
    "TileSpec",
    "VarianceDrillModal",
    "open_metrics",
    "parse_metrics_args",
    "render_projection_tile",
    "render_variance_drilldown",
    "render_wave_elapsed_tile",
]
