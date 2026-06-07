"""``ProgressBar`` -- a determinate block-eighths completion bar widget.

A fixed-width leaf :class:`~textual.widgets.Static` that paints the
block-eighths fill (:func:`~eawf.surfaces.render.bars.render_block_bar`) for
its bound completion ratio. The ratio is a single reactive float in
``[0, 1]``; assigning it repaints the bar so a live completion signal (an
iter's closed-wave share, a download fraction) tracks the bound value at
one-eighth-cell sub-resolution.

The widget is the determinate counterpart to the indeterminate braille
spinner (:mod:`~eawf.surfaces.render.spinner`): a known fraction reads as a
left-to-right fill, an unknown one as a cycling spinner. It carries no
colour and no state-schema coupling -- the caller maps state onto the
ratio -- so it composes inline in any pane and stands alone in a test.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.reactive import reactive
from textual.widgets import Static

from eawf.surfaces.render.bars import DEFAULT_WIDTH, render_block_bar

logger = logging.getLogger(__name__)


class ProgressBar(Static):
    """A determinate block-eighths progress bar bound to a completion ratio.

    Set the completion via :meth:`set_ratio` (or the reactive
    :attr:`ratio` attribute directly in tests) and the bar repaints to the
    block-eighths fill for that ratio. The fill width is fixed at
    :attr:`bar_width` cells.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ProgressBar {
        height: 1;
        width: auto;
    }
    """

    #: Completion ratio in ``[0, 1]``. Watched so assignment repaints.
    ratio: reactive[float] = reactive(0.0)

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

    def set_ratio(self, ratio: float) -> None:
        """Set the completion ratio and repaint.

        Args:
            ratio: Completion ratio in ``[0, 1]``.

        Raises:
            ValueError: When *ratio* is outside ``[0, 1]``.
        """
        if ratio < 0.0 or ratio > 1.0:
            raise ValueError(f"ratio out of range, expected [0, 1]: {ratio!r}")
        self.ratio = ratio

    def watch_ratio(self) -> None:
        """Repaint when the bound ratio changes."""
        self._repaint()

    def on_mount(self) -> None:
        """Paint the initial bar from the seeded ratio."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the bar from the current ratio."""
        self.update(render_block_bar(self.ratio, width=self.bar_width))


__all__ = ["ProgressBar"]
