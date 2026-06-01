"""``PlaceholderModeScreen`` — honest-empty body for an unbuilt mode.

The MODES chassis (:mod:`eawf.surfaces.tui.modes.registry`) seeds all six
mode digit-keys so the chassis boots and every ``1``..``6`` switch works
the day it lands, before the nine per-pane waves (Home / Trust / Doctor /
Evidence / live-feed / config-modal / ...) have built their real bodies.
A mode whose pane wave has not landed yet registers this screen as its
base: it composes the shared chassis (Header + Footer) around a single
``<Title> - coming soon`` notice so the operator sees an honest,
titled-but-empty surface rather than a blank screen or a crash.

A pane wave replaces its placeholder by swapping the mode's factory in
the registry to its real screen class (one line) and deleting nothing
here -- this module stays the fallback for any still-unbuilt mode.

The screen subclasses :class:`~eawf.surfaces.tui.scopes.ScopeScreen` so it
inherits the exact same Header (brand + breadcrumb) and Footer chrome the
scope screens use; only :meth:`compose_body` differs, yielding the
coming-soon notice. That keeps the brand ``Eae`` + breadcrumb + the
mode-switch digit keys live on a placeholder mode with zero extra wiring.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen

logger = logging.getLogger(__name__)

#: Footer hints for a placeholder mode -- only the always-live chassis
#: affordances (mode digits, palette, help, quit); a real pane wave
#: overrides these with its own pane-specific hints.
_PLACEHOLDER_HINTS: tuple[str, ...] = (
    "1-6 mode",
    "/ palette",
    "? help",
    "q quit",
)


class PlaceholderModeScreen(ScopeScreen):
    """Honest-empty base screen for a mode whose pane wave has not landed.

    Composes the shared chassis (inherited Header + Footer) around a
    single ``<title> - coming soon`` notice. The mode title is passed at
    construction so the registry can seed one placeholder class for every
    unbuilt mode without a subclass per mode.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _PLACEHOLDER_HINTS

    def __init__(self, mode_title: str) -> None:
        """Construct the placeholder for the mode titled *mode_title*.

        Args:
            mode_title: The human-readable mode title rendered in the
                coming-soon notice (e.g. ``"Trust"``).
        """
        super().__init__()
        self._mode_title = mode_title

    def compose_body(self) -> ComposeResult:
        """Yield a centred ``<title> - coming soon`` notice body."""
        with Vertical(id="body", classes="placeholder-body"):
            yield Static(
                f"{self._mode_title} - coming soon",
                classes="placeholder-notice",
            )


__all__ = ["PlaceholderModeScreen"]
