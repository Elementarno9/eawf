"""The token-to-surface map: which theme variable paints which console surface.

The design packet's stylesheet binds its mockup cell classes to palette variables
(``.canvas .dm`` to faint, ``.canvas .bd`` to muted, ``.canvas .br`` to accent), but it
names none of the product's own surfaces. This table is the product-side counterpart: each
console surface names the one theme variable that paints it and the style property it
paints, so a styled capture has an addressable subject for every token and a palette drift
reds on the surface it moves. A row whose surface has no packet class is product-authored.

Pure data plus one renderer. Nothing here imports Textual; the rendered rules resolve
against whichever registered theme is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Channel(StrEnum):
    """The style property a surface's token drives."""

    COLOR = "color"
    BACKGROUND = "background"
    BORDER = "border"


@dataclass(frozen=True, slots=True)
class SurfaceToken:
    """One console surface and the theme variable that paints it.

    Attributes:
        surface: The surface name; its stylesheet class is ``console-<surface>``.
        channel: The style property the token drives.
        token: The theme variable name, written without the ``$`` sigil.
        packet_class: The packet stylesheet selector this row mirrors, or ``None`` when the
            packet has no counterpart and the row is product-authored.

    Raises:
        ValueError: ``surface`` or ``token`` is empty, or ``token`` carries the ``$`` sigil,
            which the rendered rule would double into an unparseable variable.
    """

    surface: str
    channel: Channel
    token: str
    packet_class: str | None

    def __post_init__(self) -> None:
        """Reject a row whose rendered rule could not parse."""
        if not self.surface:
            raise ValueError(f"surface for token {self.token!r} is empty")
        if not self.token or self.token.startswith("$"):
            raise ValueError(f"token {self.token!r} of surface {self.surface!r} is not a bare name")

    @property
    def css_class(self) -> str:
        """Return the class a widget wears to take this surface's colour."""
        return f"console-{self.surface}"

    def css_rule(self) -> str:
        """Return the one Textual CSS rule that paints this surface."""
        value = f"${self.token}"
        if self.channel is Channel.BORDER:
            value = f"round {value}"
        return f".{self.css_class} {{ {self.channel.value}: {value}; }}"


# Chrome first, then the severity vocabulary, then the lifecycle tints. The keybar and
# header hints take muted rather than the packet's faint: faint misses 4.5:1 on the band.
TOKEN_MAP: tuple[SurfaceToken, ...] = (
    SurfaceToken("canvas", Channel.BACKGROUND, "surface", ".screen"),
    SurfaceToken("text", Channel.COLOR, "foreground", ".canvas"),
    SurfaceToken("band", Channel.BACKGROUND, "panel", None),
    SurfaceToken("hint", Channel.COLOR, "muted", None),
    SurfaceToken("brand", Channel.COLOR, "accent", ".canvas .brand"),
    SurfaceToken("live", Channel.COLOR, "accent", ".canvas .br"),
    SurfaceToken("rule", Channel.COLOR, "muted", ".canvas .bd"),
    SurfaceToken("rail", Channel.COLOR, "faint", ".canvas .dm"),
    SurfaceToken("dim", Channel.COLOR, "faint", ".canvas .dm"),
    SurfaceToken("frame", Channel.BORDER, "border", ".blk"),
    SurfaceToken("focus", Channel.BORDER, "primary", None),
    SurfaceToken("ok", Channel.COLOR, "ok", ".ok"),
    SurfaceToken("info", Channel.COLOR, "status-claimed", ".info"),
    SurfaceToken("warn", Channel.COLOR, "warn", ".canvas .wn"),
    SurfaceToken("err", Channel.COLOR, "err", ".canvas .er"),
    SurfaceToken("pending", Channel.COLOR, "status-pending", None),
    SurfaceToken("claimed", Channel.COLOR, "status-claimed", None),
    SurfaceToken("in-progress", Channel.COLOR, "status-in-progress", None),
    SurfaceToken("closed", Channel.COLOR, "status-closed", None),
    SurfaceToken("failed", Channel.COLOR, "status-failed", None),
)


def render_css(rows: tuple[SurfaceToken, ...]) -> str:
    """Render the stylesheet that paints every surface in ``rows``.

    Args:
        rows: The surfaces to style, normally :data:`TOKEN_MAP`.

    Returns:
        One rule per row, newline-joined, in row order; empty for no rows.

    Raises:
        ValueError: Two rows name the same surface, so the later rule would silently
            override the earlier one.
    """
    seen: set[str] = set()
    for row in rows:
        if row.surface in seen:
            raise ValueError(f"surface {row.surface!r} is mapped twice")
        seen.add(row.surface)
    return "\n".join(row.css_rule() for row in rows)
