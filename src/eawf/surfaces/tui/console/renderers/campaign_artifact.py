"""campaign.artifact: one artifact as rendered text with the file's own provenance.

Where the terminal is shorter than the file the card scrolls rather than truncating
without saying so. The card scrolls its own file; the route beneath keeps its cursor.
"""

from __future__ import annotations

from typing import Any

from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import Scrollbar, View, boxed
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers.campaign import Win
from eawf.surfaces.tui.console.renderers.spine import held, native_frame
from eawf.surfaces.tui.console.session import Session

# Rows the card's chrome takes: header, context, rule, two provenance lines, both borders,
# the foot and the keybar.
_CHROME = 9


def artifact_of(session: Session, fixture: Fixture) -> dict[str, Any]:
    """Return the artifact the card was opened on, the last artifact past the end."""
    arts = fixture.registers.cam_art
    return arts[min(session.artifact, len(arts) - 1)]


def art_window(session: Session, art: dict[str, Any], h: int) -> Win:
    """Return the file window at height ``h``, correcting the stored offset for it.

    The offset is re-clamped on every call so an overshoot never leaves ArrowUp doing
    nothing, and the furthest offset is published for the scroll keys.
    """
    total = len(art["body"])
    room = h - _CHROME
    if total <= room:
        session.art_scroll = 0
        session.art_max = 0
        return Win(0, total, 0, 0)

    def fit(offset: int) -> Win:
        marker = 1 if offset > 0 else 0
        take = room - marker
        if total - offset - take > 0:
            take = room - marker - 1
        return Win(offset, take, offset, max(0, total - offset - take))

    furthest = total - fit(total).take
    offset = max(0, min(session.art_scroll, furthest))
    session.art_scroll = offset
    session.art_max = furthest
    return fit(offset)


def render(view: View) -> list[str]:
    """Return the artifact card, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, h = view.session, view.fixture, view.h
    art = artifact_of(s, fx)
    win = art_window(s, art, h)
    lines: list[str] = []
    if win.above:
        lines.append(f"… {plural(win.above, 'line')} above")
    lines.extend(art["body"][win.start : win.start + win.take])
    if win.below:
        lines.append(f"… {plural(win.below, 'line')} below")
    keys: list[tuple[str, str]] = [("↑↓", "scroll")] if (win.above or win.below) else []
    keys.extend([("y", "copy"), ("Esc", "close")])
    arts = list(fx.registers.cam_art)
    source = f"{art['f']} · {art['k']} · {art['sz']} · written {art['at']} by {art['by']}"
    return boxed(
        view,
        crumb=f"Eä ▸ eawf-core ▸ Research ▸ CAM-0001 ▸ {art['f']}",
        ctx=f"Campaign CAM-0001 · artifact {arts.index(art) + 1} of {len(arts)} · as of 14:02",
        pre=[
            f" SOURCE     {source}",
            f" RECORD     Kept with CAM-0001 · {art['dg']}",
        ],
        title=str(art["f"]).upper(),
        lines=lines,
        foot="the file is the record · the console renders it, it does not rewrite it",
        keys=keys,
        scrollbar=Scrollbar(total=len(art["body"]), take=win.take, start=win.start),
    )


def copy(session: Session, fixture: Fixture) -> str:
    """Return the artifact's stable URN."""
    return f"urn:eawf:{fixture.scope}:CAM-0001:artifact:{artifact_of(session, fixture)['f']}"


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Scroll the file; a file that fits refuses the arrows with its reason."""
    s = ctx.s
    if s.route != "campaign.artifact" or key not in ("ArrowDown", "ArrowUp"):
        return False
    # the fit is computed for this artifact at this height, never read from the last render
    art = artifact_of(s, ctx.fixture)
    s.art_scroll = max(0, s.art_scroll)
    art_window(s, art, ctx.h)
    furthest = s.art_max
    if not furthest:
        s.art_scroll = 0
        ctx.log(key, "the whole file is shown — nothing to scroll")
        return True
    direction = 1 if key == "ArrowDown" else -1
    nxt = s.art_scroll + direction
    # offset 1 hides a single line behind a marker that costs the row it would show, so the
    # step passes over it rather than the clamp undoing it
    if nxt == 1 and furthest >= 2:
        nxt = 2 if direction > 0 else 0
    s.art_scroll = max(0, min(nxt, furthest))
    ctx.log(key, f"scroll {art['f']}")
    return True
