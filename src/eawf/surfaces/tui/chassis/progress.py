"""``ProgressBar`` -- a determinate completion bar over the unified cell-bar.

A fixed-width leaf :class:`~textual.widgets.Static` that paints the unified
block cell-bar (:func:`~eawf.surfaces.tui.widgets.eu_bar.render_completion_bar`)
for its bound ``done / total`` completion pair. The bar is the same glyph run
the roadmap tree, status pane, and metrics modal use -- a ``done`` run of
:data:`~eawf.surfaces.tui.widgets.eu_bar.COMPLETION_FULL` over a
:data:`~eawf.surfaces.tui.widgets.eu_bar.COMPLETION_REMAINDER` track with a
right-aligned ``done/total`` counter, ASCII-falling-back to ``#``/``-`` -- so
every completion surface reads one bar.

The pair is two reactive ints; assigning either repaints the bar so a live
completion signal (an iter's closed-wave share, a download fraction) tracks
the bound values. A ``total <= 0`` pair surfaces the
:data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel rather than a
fabricated empty bar -- the "surface now, data later" contract the unified
bar enforces.

The bar carries no colour and no state-schema coupling -- the caller maps
state onto the ``done`` / ``total`` pair -- so it composes inline in any pane
and stands alone in a test. The glyph set follows
:attr:`eawf.surfaces.tui.app.EaApp.render_mode`: the widget seeds its own
:attr:`render_mode` reactive from the app on mount and repaints on a flip, so
a single unicode <-> ASCII flip rerenders every bar.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.reactive import reactive
from textual.widgets import Static

from eawf.surfaces.render.bars import DEFAULT_WIDTH
from eawf.surfaces.tui.widgets.eu_bar import RenderMode, render_completion_bar

logger = logging.getLogger(__name__)


class ProgressBar(Static):
    """A determinate completion bar bound to a ``done / total`` pair.

    Set the completion via :meth:`set_progress` (or the reactive
    :attr:`done` / :attr:`total` attributes directly in tests) and the bar
    repaints to the unified cell-bar fill for that pair. The fill width is
    fixed at :attr:`bar_width` cells. A ``total <= 0`` pair paints the
    :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel, not a
    fabricated bar.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ProgressBar {
        height: 1;
        width: auto;
    }
    """

    #: Completed child count (e.g. closed waves). Watched so assignment
    #: repaints. Negative inputs clamp inside the unified bar renderer.
    done: reactive[int] = reactive(0)

    #: Total child count. ``<= 0`` surfaces the empty-state sentinel.
    #: Watched so assignment repaints.
    total: reactive[int] = reactive(0)

    #: Active fill mode. Seeded from :attr:`eawf.surfaces.tui.app.EaApp.render_mode`
    #: on mount; the widget mirrors an app-level flip onto this reactive so a
    #: single unicode <-> ASCII flip repaints the bar. Watched so the mirrored
    #: flip rerenders in the other glyph set.
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, *, width: int = DEFAULT_WIDTH, id: str | None = None) -> None:
        """Initialise the bar with a fixed cell width.

        Args:
            width: Bar cell count (> 0). Defaults to
                :data:`~eawf.surfaces.render.bars.DEFAULT_WIDTH`.
            id: Optional widget id forwarded to :class:`Static`.

        Raises:
            ValueError: When *width* is not positive.
        """
        if width <= 0:
            raise ValueError(f"width must be positive: {width!r}")
        super().__init__(id=id)
        self.bar_width = width

    def set_progress(self, done: int, total: int) -> None:
        """Set both completion values in one shot and repaint.

        Args:
            done: Completed child count (e.g. closed waves). Negative
                inputs clamp inside the unified bar renderer.
            total: Total child count. ``<= 0`` surfaces the empty-state
                sentinel rather than a fabricated bar.
        """
        self.done = done
        self.total = total

    def watch_done(self) -> None:
        """Repaint when the completed count changes."""
        self._repaint()

    def watch_total(self) -> None:
        """Repaint when the total count changes."""
        self._repaint()

    def watch_render_mode(self) -> None:
        """Repaint when the fill mode flips (unicode <-> ASCII)."""
        self._repaint()

    def on_mount(self) -> None:
        """Seed the render mode from the app, watch it, and paint the bar."""
        app_mode = getattr(self.app, "render_mode", None)
        if app_mode is not None:
            self.render_mode = app_mode
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_app_render_mode)
        self._repaint()

    def _on_app_render_mode(self, mode: RenderMode) -> None:
        """Mirror an app-level render-mode flip onto this widget's reactive."""
        self.render_mode = mode

    def _repaint(self) -> None:
        """Re-render the unified cell-bar from the current done / total pair."""
        self.update(
            render_completion_bar(
                self.done, self.total, width=self.bar_width, mode=self.render_mode
            )
        )


__all__ = ["ProgressBar"]
