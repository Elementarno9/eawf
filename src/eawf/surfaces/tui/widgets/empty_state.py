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

from typing import TYPE_CHECKING

from textual.containers import Vertical

from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.seal import (
    SEAL_ART_CLASS,
    SEAL_ART_LINES,
    seal_art_widget,
)
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.widgets import Static

#: Structural CSS body every honest-empty ``Static`` includes so the hero
#: centers in its pane (content centered on both axes, each line centered) and
#: carries no border -- the calm-empty tone the reskin pins, not a ``$warn``
#: alert box. A host pane drops this into its empty rule, e.g.
#: ``MyScreen #my-empty {{ {HONEST_EMPTY_CSS} }}``.
HONEST_EMPTY_CSS: str = "height: 1fr; width: 1fr; content-align: center middle; text-align: center;"

#: Number of art rows in the half-block Seal -- the fixed height the seal-art
#: :class:`~textual.widgets.Static` is sized to so the disc never clips.
SEAL_ART_HEIGHT: int = len(SEAL_ART_LINES)

#: Stable id of the centered seal-hero wrapper a mode mounts when it leads its
#: honest-empty surface with the ASCII-art Seal (the research-board pattern,
#: spread to the autopilot / feed / agent-watch / evidence empty heroes so the
#: brand mark reads consistently across the honest-empty surfaces). Optional on
#: the wrapper -- a host that re-mounts the hero on a deferred ``remove_children``
#: (the autopilot dynamic-rebuild race) omits the id and queries by the class so
#: a rapid second rebuild never collides on a duplicate id.
SEAL_HERO_ID: str = "seal-empty-hero"

#: CSS class the centered seal-hero wrapper always carries (the centering hook).
#: A class -- not the id -- carries the centering rule so the wrapper can mount
#: id-less on a host that re-mounts it dynamically (autopilot) without a
#: duplicate-id collision.
SEAL_HERO_CLASS: str = "seal-empty-hero"

#: CSS class :func:`seal_empty_hero` stamps onto the honest-empty *body* Static
#: it wraps under the seal art. The body normally carries
#: :data:`HONEST_EMPTY_CSS` (``height: 1fr``) so it FILLS + centers its pane when
#: mounted bare (the ascii fallback path); but under the seal art that
#: full-height body would eat the wrapper's leftover space and pin the seal to
#: the TOP of the pane (the W30 top-anchored defect). The fix is to pin the body
#: back to ``height: auto`` inside the hero so the seal art (fixed
#: :data:`SEAL_ART_HEIGHT`) + the auto-height body form ONE compact block the
#: wrapper's ``align: center middle`` then centers vertically -- the
#: research-board centered-hero look (its ``#research-empty-hero #research-empty
#: { height: auto; }`` override is the same fix, applied id-scoped).
#:
#: The body also carries a top margin (:data:`SEAL_HERO_BODY_GAP`) so the
#: headline sits a clear blank row or two BELOW the seal art rather than flush
#: under its bottom edge -- the operator-flagged breathing room between the brand
#: mark and the hero copy.
#:
#: The CANONICAL overrides are the INLINE ``height: auto`` + ``margin-top``
#: :func:`seal_empty_hero` sets on the body (inline styles win over every
#: stylesheet rule, including a host that styles its body by *id* such as feed's
#: ``#feed-empty``, whose id rule out-ranks any class). This class is the
#: stylesheet BACKSTOP / styling hook -- :func:`seal_hero_css` emits the matching
#: ``height: auto`` + ``margin-top`` class rule so the hero's intent is visible
#: in the screen's ``DEFAULT_CSS`` too.
SEAL_HERO_BODY_CLASS: str = "seal-empty-hero-body"

#: Blank rows of vertical gap between the bottom of the seal art and the hero
#: headline. The seal art Static and the body Static stack flush in the wrapper,
#: so without this gap the headline (e.g. "no word spoken yet") sits directly
#: under the seal's last art row with no breathing room. A one-row top margin on
#: the body opens a clear blank gap below the seal while the whole block stays
#: centered (the wrapper's ``align: center middle`` re-centers the seal + gap +
#: copy as one taller block).
SEAL_HERO_BODY_GAP: int = 1


def seal_hero_css(screen_selector: str) -> str:
    """Return the seal-hero centering CSS scoped to *screen_selector*.

    Emits the three rules a host screen drops into its ``DEFAULT_CSS`` so the
    seal hero centers, ALL prefixed with the screen selector so the rules never
    leak across screens:

    * the wrapper (:data:`SEAL_HERO_CLASS`) is a FULL-HEIGHT region
      (``height: 1fr``) whose ``align: center middle`` stacks the seal + the
      body block and centers that stack on BOTH axes in the pane;
    * the seal ``Static`` (carrying
      :data:`~eawf.surfaces.tui.widgets.seal.SEAL_ART_CLASS`) takes the full
      pane width (``width: 1fr``) and centers each of its lines (``text-align:
      center``);
    * the body ``Static`` (carrying :data:`SEAL_HERO_BODY_CLASS`, stamped on by
      :func:`seal_empty_hero`) is pinned to ``height: auto`` so it never eats
      the wrapper's leftover space and shove the seal to the top -- the body's
      own :data:`HONEST_EMPTY_CSS` carries ``height: 1fr`` for the bare-mount
      (ascii) path, which must be overridden under the art seal -- AND a
      ``margin-top: SEAL_HERO_BODY_GAP`` so the headline sits a clear blank row
      below the seal art rather than flush under its bottom edge.

    The ``width: 1fr; text-align: center`` pair is load-bearing: a fixed
    ``width: 42`` left-anchors the symmetric block, while the full-width +
    centered-text pair centers the 42-wide art on the screen midline (the
    operator-approved research-board centering). The body ``height: auto``
    override is the load-bearing VERTICAL fix: with the seal at a fixed
    :data:`SEAL_ART_HEIGHT` and the body at ``auto``, the two form one compact
    block the wrapper's ``align: center middle`` centers, so equal blank space
    sits above and below the seal + copy -- matching the research board's
    ``#research-empty-hero #research-empty { height: auto; }`` override.

    Args:
        screen_selector: The host screen's CSS type selector (e.g.
            ``"AutopilotModeScreen"``) every rule is prefixed with.

    Returns:
        The three scoped CSS rules, space-joined, ready to drop into a screen's
        ``DEFAULT_CSS``.
    """
    return (
        f"{screen_selector} .{SEAL_HERO_CLASS} "
        "{ height: 1fr; width: 1fr; align: center middle; } "
        f"{screen_selector} .{SEAL_ART_CLASS} "
        f"{{ width: 1fr; height: {SEAL_ART_HEIGHT}; "
        "content-align: center middle; text-align: center; color: $accent; } "
        f"{screen_selector} .{SEAL_HERO_BODY_CLASS} "
        f"{{ height: auto; margin-top: {SEAL_HERO_BODY_GAP}; }}"
    )


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
    sigil: bool = True,
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
    lines: list[str] = [brand_sigil_markup(mode=mode), ""] if sigil else []
    lines.append(f"[{headline_tint}]{escape_markup(headline)}[/]")
    if subline:
        lines.append(f"[$muted]{escape_markup(subline)}[/]")
    if chips:
        row = "   ".join(
            render_chip(token, label, primary=index == 0)
            for index, (token, label) in enumerate(chips)
        )
        lines.extend(["", row])
    return "\n".join(lines)


def seal_empty_hero(body: Static, *, hero_id: str | None = SEAL_HERO_ID) -> Vertical:
    """Wrap the ASCII-art Seal over the honest-empty *body* in a centered hero.

    Mirrors the research-board honest-empty hero so the four other empty-state
    surfaces (autopilot / feed / agent-watch / evidence) lead with the same
    brand mark: a centered :class:`~textual.containers.Vertical` carrying
    :data:`SEAL_HERO_CLASS` and stacking the
    :func:`~eawf.surfaces.tui.widgets.seal.seal_art_widget` art over *body*. The
    host screen drops :func:`seal_hero_css` into its ``DEFAULT_CSS`` so the
    wrapper is a full-height ``align: center middle`` region: the seal Static's
    ``width: 1fr; text-align: center`` centers the symmetric 42-wide art block on
    the screen midline (a fixed-width seal would left-anchor), and the wrapper
    centers the seal + body block VERTICALLY in the pane. The *body* Static keeps
    whatever id / classes the caller already styles it with, plus the
    :data:`SEAL_HERO_BODY_CLASS` this helper stamps on, AND a load-bearing inline
    ``height: auto`` this helper sets directly on the body.

    The inline height is what actually centers the block: the body normally
    carries :data:`HONEST_EMPTY_CSS` (``height: 1fr``) so it FILLS + centers when
    mounted bare (the ascii fallback path), but under the seal art that
    full-height body would eat the wrapper's leftover space and pin the seal to
    the TOP (the W30 top-anchored defect). A stylesheet class override loses the
    specificity race against a host that styles its body by *id* (e.g. feed's
    ``#feed-empty``, whose id rule out-ranks any class), so the override is set
    INLINE here -- inline styles win over every stylesheet rule regardless of the
    body's id / class. With the seal at a fixed :data:`SEAL_ART_HEIGHT` and the
    body at ``auto``, the two form ONE compact block the wrapper's ``align:
    center middle`` centers vertically -- the research-board centered-hero look.

    The body also carries an INLINE ``margin-top`` of :data:`SEAL_HERO_BODY_GAP`
    (set here for the same id-rule-beating reason as the height) so the headline
    sits a clear blank row below the seal art rather than flush under its bottom
    edge -- the operator-flagged breathing room between the brand mark and the
    copy. The wrapper's ``align: center middle`` re-centers the seal + gap + copy
    as one taller block, so the gap does not break the vertical centering.

    The body should NOT carry its own brand sigil when led by the art seal --
    pass ``sigil=False`` to :func:`render_empty_state` so the art is the single
    brand mark rather than a redundant glyph beside it.

    Args:
        body: The honest-empty body ``Static`` (headline + subline + chips) the
            seal art leads.
        hero_id: The wrapper's widget id. Defaults to :data:`SEAL_HERO_ID` for
            a compose-once host that queries the hero by id; pass ``None`` on a
            host that re-mounts the hero on a deferred ``remove_children`` (the
            autopilot dynamic-rebuild race), where a fixed id collides with the
            not-yet-torn-down prior hero -- the :data:`SEAL_HERO_CLASS` carries
            the centering regardless.

    Returns:
        The centered seal hero wrapper, ready to ``yield`` or ``mount``.
    """
    body.add_class(SEAL_HERO_BODY_CLASS)
    # Inline height + margin win over the body's own id/class rules (e.g. feed's
    # `#feed-empty { height: 1fr }`), so the body never eats the wrapper's
    # leftover space and the seal + gap + copy block centers vertically with a
    # clear blank row between the seal art and the headline.
    body.styles.height = "auto"
    body.styles.margin = (SEAL_HERO_BODY_GAP, 0, 0, 0)
    return Vertical(seal_art_widget(), body, id=hero_id, classes=SEAL_HERO_CLASS)


__all__ = [
    "HONEST_EMPTY_CSS",
    "SEAL_ART_HEIGHT",
    "SEAL_HERO_BODY_CLASS",
    "SEAL_HERO_BODY_GAP",
    "SEAL_HERO_CLASS",
    "SEAL_HERO_ID",
    "brand_sigil_markup",
    "render_chip",
    "render_empty_state",
    "seal_empty_hero",
    "seal_hero_css",
]
