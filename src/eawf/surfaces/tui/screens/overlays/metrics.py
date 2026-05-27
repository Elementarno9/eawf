"""``MetricsModal`` — the V7 ``/metrics`` 3x2 dashboard overlay.

The ``/metrics`` palette verb opens a 3x2 grid of metric tiles backed by
the daemon's telemetry projection. The six tiles, in grid order, are:

1. **Variance** (top-left) — estimate-vs-actual EU per effort bucket.
2. **Weekly burn** (top-middle) — actual EU vs the project's weekly target.
3. **Wave elapsed** (top-right) — median + p90 wall-clock per closed wave.
4. **Cache health** (bottom-left) — cache-create vs cache-read token ratio.
5. **Switchover frequency** (bottom-middle) — ``runtime_switched`` counts
   per cause over the rolling window.
6. **Per-runtime tokens** (bottom-right) — token split per runtime.

The modal computes the projection from the current read-only state snapshot
and, when present, the local telemetry DB. State-backed tiles still render
when the telemetry DB is absent; telemetry-backed tiles show a no-data
sentinel until the projector has written rows. The refresh cadence stays
``set_interval(5.0, ...)`` so the same surface can switch to daemon-push
later without changing the visible contract.

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
from typing import TYPE_CHECKING, ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.observability.telemetry.metrics_projection import (
    MetricsProjection,
    MetricsWindow,
    compute_metrics_projection,
)
from eawf.observability.telemetry.store import metrics_db_path, open_store
from eawf.observability.telemetry.store.base import AbstractMetricsStore
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
    """

    tile_id: str
    title: str


#: The 3x2 tile inventory in grid order (row-major: top-left → bottom-
#: right). The grid is ``grid-size: 3 2`` so the first three specs fill
#: the top row and the last three the bottom row.
TILE_SPECS: tuple[TileSpec, ...] = (
    TileSpec("tile-variance", "Variance / bucket"),
    TileSpec("tile-burn", "Weekly burn"),
    TileSpec("tile-elapsed", "Wave elapsed"),
    TileSpec("tile-cache", "Cache health"),
    TileSpec("tile-switchover", "Switchover freq"),
    TileSpec("tile-tokens", "Per-runtime tokens"),
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
    return f"{value:.1f} EU"


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
        lines.append(
            f"{row.bucket.value} {render_variance_markup(row.variance_pct)} n={row.sample_count}"
        )
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
                window = cast(MetricsWindow, candidate)
            index += 2
            continue
        if token == "--scope" and index + 1 < len(tokens):
            scope_filter = tokens[index + 1]
            index += 2
            continue
        index += 1
    return MetricsArgs(window=window, scope_filter=scope_filter)


class MetricsModal(ModalScreen[None]):
    """3x2 grid of metric tiles (Esc to close); dashboard overlay.

    Composes the six :data:`TILE_SPECS` tiles in a ``grid-size: 3 2``
    grid, renders each tile's placeholder until the telemetry-projection
    RPC is wired, and arms the :data:`METRICS_REFRESH_S` refresh seam.
    Built with a pre-parsed :class:`MetricsArgs` so the window / scope
    filter come from the verb args, not from reaching into App state.
    """

    DEFAULT_CSS: ClassVar[str] = """
    MetricsModal {
        align: center middle;
    }
    MetricsModal > #metrics-card {
        width: 90%;
        height: 80%;
        border: solid $accent;
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
        grid-size: 3 2;
        grid-gutter: 1;
        height: 1fr;
    }
    MetricsModal .metric-tile {
        border: solid $accent;
        padding: 0 1;
        height: 1fr;
    }
    MetricsModal .metric-tile.-focused {
        border: solid $primary;
    }
    MetricsModal .metrics-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Esc`` closes the dashboard; the only binding it owns this wave.
    #: The tile-focus arrows + ``Enter`` drill ride the wave that lands
    #: the per-tile sub-overlays (the data they drill into is seamed).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, metrics_args: MetricsArgs | None = None) -> None:
        """Construct the dashboard for the parsed verb args.

        Args:
            metrics_args: The parsed window + scope filter; defaults to
                the :data:`DEFAULT_WINDOW`, current-scope view when the
                palette opens ``/metrics`` with no arguments.
        """
        super().__init__()
        self._args = metrics_args or MetricsArgs(window=DEFAULT_WINDOW, scope_filter=None)

    def compose(self) -> ComposeResult:
        """Yield the titled card, the 3x2 tile grid, and the close hint."""
        scope_suffix = f" · {self._args.scope_filter}" if self._args.scope_filter else ""
        projection = self._current_projection()
        with Grid(id="metrics-card"):
            yield Static(
                f"Metrics · window {self._args.window}{scope_suffix}",
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
                "[ tiles refresh every 5s · Esc to close ]",
                classes="metrics-hint",
            )

    def _tile_body(self, spec: TileSpec, projection: MetricsProjection | None) -> str:
        """Render *spec*'s tile body from *projection*.

        Args:
            spec: The tile spec to render the body for.
            projection: The current six-tile metrics projection, or ``None``
                before a state snapshot is available.

        Returns:
            The tile's content-markup body string.
        """
        return render_projection_tile(projection, spec.tile_id)

    def on_mount(self) -> None:
        """Arm the refresh seam (live once the telemetry RPC is wired).

        The interval is created now so the cadence + the refresh entry
        point are in place; :meth:`_refresh_all` short-circuits while the
        telemetry client is absent so the tiles hold their placeholder.
        """
        self.set_interval(METRICS_REFRESH_S, self._refresh_all)

    def _refresh_all(self) -> None:
        """Refresh every tile from the current projection."""
        projection = self._current_projection()
        for spec in TILE_SPECS:
            tile = self.query_one(f"#{spec.tile_id}", Static)
            tile.update(self._tile_body(spec, projection))
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
        """Return the current six-tile projection, or ``None`` before state loads."""
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

    def action_close(self) -> None:
        """Dismiss the dashboard (``Esc``)."""
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
    "METRICS_REFRESH_S",
    "METRIC_WINDOWS",
    "TILE_SPECS",
    "MetricsArgs",
    "MetricsModal",
    "TileSpec",
    "open_metrics",
    "parse_metrics_args",
    "render_projection_tile",
    "render_wave_elapsed_tile",
]
