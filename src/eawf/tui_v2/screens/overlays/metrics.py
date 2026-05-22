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

**Deferral seam.** The daemon telemetry-projection RPC
(``client.telemetry_metrics(scope=..., window=...)``) lands with the
telemetry projector + the daemon client; it does not exist on
the read-only :class:`~eawf.tui_v2.state_binding.StateBinding` fallback
this band ships. So this wave lands the **grid + tile chrome + the
5-second refresh seam**: the modal composes the six titled tiles, renders
each tile's "awaiting telemetry projection" placeholder, and arms the
``set_interval(5.0, ...)`` refresh that becomes live once the telemetry
client is wired. The tile-drill sub-overlays (per-wave variance table,
per-cause switchover history) ride the wave that lands the telemetry data.

The tile inventory + grid order are a pure module-level table
(:data:`TILE_SPECS`) so the composition is unit-testable without mounting
Textual, and the modal stays a thin view over it. The window argument
(``7d`` / ``30d`` / ``90d``) and the optional scope filter are parsed from
the ``/metrics`` verb args by :func:`parse_metrics_args` (also pure).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The rolling-window tokens the ``/metrics --window`` flag accepts.
METRIC_WINDOWS: tuple[str, ...] = ("7d", "30d", "90d")

#: Default rolling window when ``--window`` is omitted (7-day weekly cadence).
DEFAULT_WINDOW: str = "7d"

#: Telemetry-projection refresh cadence in seconds (5 s tick).
METRICS_REFRESH_S: float = 5.0

#: Placeholder body each tile shows until the telemetry-projection RPC is
#: wired (the data seam lands with the telemetry projector + daemon client).
_AWAITING: str = "[$text-muted]awaiting telemetry projection[/]"


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

    window: str
    scope_filter: str | None


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
    window = DEFAULT_WINDOW
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
        with Grid(id="metrics-card"):
            yield Static(
                f"Metrics · window {self._args.window}{scope_suffix}",
                classes="metrics-title",
            )
            with Grid(id="metrics-grid"):
                for spec in TILE_SPECS:
                    tile = Static(self._tile_body(spec), id=spec.tile_id, classes="metric-tile")
                    tile.border_title = spec.title
                    yield tile
            yield Static(
                "[ tiles fill once the telemetry projection is wired · Esc to close ]",
                classes="metrics-hint",
            )

    def _tile_body(self, spec: TileSpec) -> str:
        """Render *spec*'s tile body (the placeholder until data is wired).

        Args:
            spec: The tile spec to render the body for.

        Returns:
            The tile's content-markup body string.
        """
        return _AWAITING

    def on_mount(self) -> None:
        """Arm the refresh seam (live once the telemetry RPC is wired).

        The interval is created now so the cadence + the refresh entry
        point are in place; :meth:`_refresh_all` short-circuits while the
        telemetry client is absent so the tiles hold their placeholder.
        """
        self.set_interval(METRICS_REFRESH_S, self._refresh_all)

    def _refresh_all(self) -> None:
        """Refresh every tile from the telemetry projection (seamed).

        The daemon telemetry-projection client is not reachable on the
        read-only state-binding fallback this band ships; until the
        telemetry projector + daemon client land, this is a no-op so the
        tiles keep their placeholder rather than clearing to blank. The
        per-tile ``update_from(metrics)`` fan-out slots in here when the
        client arrives.
        """
        client = self._telemetry_client()
        if client is None:
            return
        logger.info(
            f"metrics_refresh window={self._args.window!r} scope={self._args.scope_filter!r}"
        )

    def _telemetry_client(self) -> object | None:
        """Return the daemon telemetry client, or ``None`` when unwired.

        Read-only probe of the App's state binder for a telemetry client.
        The fallback binder this band ships exposes none, so this returns
        ``None`` and the refresh stays a no-op until the daemon client
        lands.

        Returns:
            The telemetry client when present, else ``None``.
        """
        binding = getattr(self.app, "_binding", None)
        return getattr(binding, "_client", None)

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
]
