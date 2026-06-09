"""``PlaceholderModeScreen`` -- honest-empty body for an unbuilt mode.

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

Cosmic-terminal reskin (P30-I02-W33)
------------------------------------
The notice speaks the calm honest-empty voice the rest of the reskin
uses, so an unbuilt surface reads as INTENTIONALLY empty rather than
broken: the line leads with the green ``$accent`` pending sigil (the
hollow dotted ring -- a "not-yet-here, on the roadmap" mark, NOT a spinner
or any other false-busy chrome), then the byte-for-byte
``<title> - coming soon`` copy in the same green ``$accent``, and a muted
sub-note that names the empty state as deliberate. The pending sigil is
the same SHAPE the lifecycle panes draw for a wave that has not started
(:data:`~eawf.surfaces.tui.widgets.sigils.Sigil.PENDING`), so an
unbuilt mode reads in the project's shared vocabulary -- pending, not
failed. No spinner, no progress bar, no fabricated activity: a calm,
green, intentionally-empty mark.

The render half is a pure helper (:func:`render_placeholder_notice`) that
returns the content-markup line, so the reskinned voice is unit-testable
without mounting Textual; the screen is a thin :class:`ScopeScreen` body
over it. The sigil is threaded the App's resolved render mode so an
ASCII / unicode flip swaps the glyph column.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

logger = logging.getLogger(__name__)

#: The byte-for-byte coming-soon copy suffix. The notice text is
#: ``"<mode title> - coming soon"`` -- this suffix is held as a named
#: constant so the reskin (the green sigil + accent span around it) can
#: never silently drift the words: the words stay exactly this, only the
#: render language around them is the reskin.
COMING_SOON_SUFFIX: str = " - coming soon"

#: The muted sub-note rendered beneath the coming-soon line. It names the
#: empty state as deliberate -- the calm honest-empty voice -- so the
#: operator reads the surface as "on the roadmap, not yet built", never as
#: a broken or crashed pane.
INTENTIONAL_EMPTY_NOTE: str = "intentionally empty until this pane lands"

#: Footer hints for a placeholder mode -- only the always-live chassis
#: affordances (palette, help, quit); a real pane wave overrides these with its
#: own pane-specific hints. The mode digits are surfaced by the always-visible
#: mode row, not duplicated in the hint strip. Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_PLACEHOLDER_HINTS: tuple[str, ...] = (
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


def coming_soon_text(mode_title: str) -> str:
    """Return the byte-for-byte ``<title> - coming soon`` notice copy.

    The single home of the coming-soon words: the title joined to
    :data:`COMING_SOON_SUFFIX`. The reskin wraps green-accent content markup
    AROUND this string; the words themselves are unchanged.

    Args:
        mode_title: The human-readable mode title (e.g. ``"Trust"``).

    Returns:
        The exact ``"<mode_title> - coming soon"`` copy.
    """
    return f"{mode_title}{COMING_SOON_SUFFIX}"


def render_placeholder_notice(mode_title: str, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the calm honest-empty coming-soon notice in the reskin voice.

    The line leads with the green ``$accent`` pending sigil (the hollow
    dotted ring -- the shared SHAPE for a not-yet-started lifecycle row, NOT
    a spinner or any other false-busy chrome) and the byte-for-byte
    ``<title> - coming soon`` copy in the same green ``$accent``, then a
    muted sub-note that names the empty state as deliberate. The colours
    resolve against the active theme's green accent at render time via
    Textual content markup, so the surface reads as intentionally empty --
    on the roadmap, not broken.

    Args:
        mode_title: The human-readable mode title rendered in the notice
            (e.g. ``"Trust"``).
        mode: The App's resolved render-mode label, threaded so the pending
            sigil resolves its ASCII / unicode glyph column; defaults to
            :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE`.

    Returns:
        A content-markup string: the green-accent sigil + coming-soon copy
        line, then the muted intentional-empty sub-note.
    """
    sigil = glyph(Sigil.PENDING, mode=mode)
    copy = coming_soon_text(mode_title)
    return "\n".join(
        [
            f"[$accent]{sigil} {copy}[/]",
            f"[$muted]{INTENTIONAL_EMPTY_NOTE}[/]",
        ]
    )


class PlaceholderModeScreen(ScopeScreen):
    """Honest-empty base screen for a mode whose pane wave has not landed.

    Composes the shared chassis (inherited Header + Footer) around a
    single calm ``<title> - coming soon`` notice in the cosmic-terminal
    reskin voice (green-accent pending sigil + accent copy + muted
    intentional-empty sub-note). The mode title is passed at construction
    so the registry can seed one placeholder class for every unbuilt mode
    without a subclass per mode.
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
        """Yield a centred calm honest-empty coming-soon notice body."""
        with Vertical(id="body", classes="placeholder-body"):
            yield Static(
                render_placeholder_notice(self._mode_title, mode=self._render_mode()),
                classes="placeholder-notice",
            )

    def _render_mode(self) -> RenderMode:
        """Return the host app's live render mode, or the safe default.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the
        pending sigil so an ASCII / unicode flip rerenders the notice with
        the matching glyph column. Falls back to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` under a
        bare harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The active ``"unicode"`` / ``"ascii"`` mode.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)


__all__ = [
    "COMING_SOON_SUFFIX",
    "INTENTIONAL_EMPTY_NOTE",
    "PlaceholderModeScreen",
    "coming_soon_text",
    "render_placeholder_notice",
]
