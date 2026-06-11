"""Shared honest-empty render helper (widget catalog).

A single source for the cosmic-terminal *honest-empty hero*: a centered block
under a muted brand sigil, a ``$warn`` headline, an optional ``$muted`` subline,
and optional action chips. Every mode's empty-state ``Static`` routes its body
through :func:`render_empty_state` so an empty surface reads the same calm hero
everywhere instead of a top-left boxed notice -- the reskin's "honest-empty
states render calm microcopy, centered" intent (the operator-flagged divergence
on the research board: a top-left ``$warn``-bordered notice with no sigil and no
action chips).

The *centering* is structural CSS the host pane applies to its empty
``Static`` (``content-align: center middle; text-align: center;`` + no border) --
:data:`HONEST_EMPTY_CSS` is the canonical snippet so every mode centers the hero
identically. This module owns only the *content* markup; the brand sigil is the
single terminal-renderable brand mark (:func:`~eawf.surfaces.tui.widgets.sigils.chrome`
``"brand"``), muted so the hero reads calm rather than as an accent call-to-arms.
"""

from __future__ import annotations

from collections.abc import Sequence

from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

#: Structural CSS body every honest-empty ``Static`` includes so the hero
#: centers in its pane (content centered on both axes, each line centered) and
#: carries no border -- the calm-empty tone the reskin pins, not a ``$warn``
#: alert box. A host pane drops this into its empty rule, e.g.
#: ``MyScreen #my-empty {{ {HONEST_EMPTY_CSS} }}``.
HONEST_EMPTY_CSS: str = "height: 1fr; width: 1fr; content-align: center middle; text-align: center;"


def brand_sigil_markup(*, mode: str = "unicode", tint: str = "$muted") -> str:
    """Return the brand sigil as a tinted content-markup span.

    The brand sigil is the terminal-renderable stand-in for the Seal SVG --
    the same ``"brand"`` chrome glyph the header leads with -- rendered muted by
    default so an empty-state hero reads calm rather than as an accent prompt.

    Args:
        mode: The App's resolved render-mode label -- ``"ascii"`` selects the
            ascii column, any other value the unicode glyph.
        tint: The palette var the glyph renders in (``$muted`` for the calm
            hero, ``$accent`` when a caller wants the brand to lead).

    Returns:
        A content-markup span wrapping the brand glyph in *tint*.
    """
    return f"[{tint}]{chrome('brand', mode=mode)}[/]"


def render_chip(token: str, label: str, *, primary: bool = False) -> str:
    """Render one action affordance as a bracketed key-led chip.

    A chip reads ``[ <key> <label> ]`` with the key bold -- the terminal-honest
    stand-in for the mockup's button affordance (a real ``Button`` is not used:
    the empty-state body is a single ``Static``, and the key IS the affordance).
    The primary chip wears ``$accent``, secondary chips ``$muted``.

    Args:
        token: The key that triggers the action (e.g. ``"n"``).
        label: The short action label (e.g. ``"new campaign"``).
        primary: When ``True`` the chip wears ``$accent`` (the signature
            action), else ``$muted``.

    Returns:
        A content-markup chip string with the literal brackets escaped.
    """
    tint = "$accent" if primary else "$muted"
    return f"[{tint}]\\[ [b]{escape_markup(token)}[/b] {escape_markup(label)} ][/]"


def render_empty_state(
    headline: str,
    subline: str = "",
    *,
    mode: str = "unicode",
    headline_tint: str = "$warn",
    chips: Sequence[tuple[str, str]] = (),
) -> str:
    """Render the honest-empty hero body: sigil + headline + subline + chips.

    The body is a brand sigil over the headline, an optional ``$muted`` subline,
    and an optional row of action chips -- centered by the host pane's
    :data:`HONEST_EMPTY_CSS`. The headline / subline are markup-escaped so a
    bracket in the copy can never be parsed as a style tag.

    Args:
        headline: The honest-empty headline (e.g. ``"no word spoken yet"``).
        subline: An optional one-line elaboration; omitted from the body when
            empty.
        mode: The App's resolved render-mode label, forwarded to the brand
            sigil's glyph column.
        headline_tint: The palette var the headline renders in. Defaults to the
            house honest-empty ``$warn``; a *good-state* empty (e.g. the sandbox
            "nothing was denied" pane) passes ``$muted`` so the calm tone reads
            reassuring rather than alarmed.
        chips: Optional ``(token, label)`` action affordances; the first is the
            primary (accent) chip, the rest muted. Rendered on their own line.

    Returns:
        A newline-joined content-markup body for the empty-state ``Static``.
    """
    lines = [brand_sigil_markup(mode=mode), "", f"[{headline_tint}]{escape_markup(headline)}[/]"]
    if subline:
        lines.append(f"[$muted]{escape_markup(subline)}[/]")
    if chips:
        row = "   ".join(
            render_chip(token, label, primary=index == 0)
            for index, (token, label) in enumerate(chips)
        )
        lines.extend(["", row])
    return "\n".join(lines)


__all__ = [
    "HONEST_EMPTY_CSS",
    "brand_sigil_markup",
    "render_chip",
    "render_empty_state",
]
