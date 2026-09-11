"""The notification rack: three bordered rows per toast, three toasts at most, one shared
width, painted bottom-right over the body one row above the keybar (one higher under
``--verbose``). Each toast expires five seconds after its own arming on the console
clock; a held clock keeps every deadline where it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..chassis.session import Toast
from ..chassis.width import cell_len, clip_words, pad

if TYPE_CHECKING:
    from ..chassis.session import Clock, Session

TOAST_DWELL = 5.0
RACK_MAX = 3


def notify(
    session: Session, clock: Clock, text: str, title: str = "done", sev: str = "info"
) -> None:
    session.toasts.append(Toast(title=title, text=str(text), sev=sev, at=clock.now()))
    while len(session.toasts) > RACK_MAX:
        session.toasts.pop(0)


def sweep(session: Session, clock: Clock) -> bool:
    """Expire toasts older than their dwell; True when the rack changed."""
    now = clock.now()
    delta = now - session.rack_last
    session.rack_last = now
    if not session.toasts:
        return False
    if session.rack_hold:
        for t in session.toasts:
            t.at += delta
        return False
    gone = [t for t in session.toasts if now - t.at >= TOAST_DWELL]
    if not gone:
        return False
    session.toasts = [t for t in session.toasts if now - t.at < TOAST_DWELL]
    for t in gone:
        session.log_key("—", f"toast expired · {t.title} · {TOAST_DWELL:g}s")
    return True


def clear(session: Session) -> int:
    n = len(session.toasts)
    session.toasts = []
    return n


def toast_box(t: Toast, w: int) -> list[str]:
    g = "✗ " if t.sev == "err" else ("! " if t.sev == "warn" else "")
    head = g + t.title
    return [
        "┌─ " + head + " " + "─" * max(1, w - 5 - cell_len(head)) + "┐",
        "│ " + pad(clip_words(t.text, w - 4), w - 4) + " │",
        "└" + "─" * (w - 2) + "┘",
    ]


def overlay_row(out: list[str], row: int, col: int, text: str, w: int) -> None:
    """The one place a frame is overwritten rather than composed."""
    if row < 1 or row >= len(out):
        return
    base = out[row] if out[row] is not None else " " * w
    out[row] = base[:col] + text + base[col + cell_len(text) :]


def paint_rack(session: Session, out: list[str], w: int) -> None:
    if not session.toasts:
        return
    width = 0
    for t in session.toasts:
        width = max(width, 28, cell_len(t.title) + 9, cell_len(t.text) + 4)
    width = min(w - 2, width)
    bottom = len(out) - 1 - (1 if session.verbose else 0)
    for t in reversed(session.toasts):
        top = bottom - 2
        if top < 1:
            break
        box = toast_box(t, width)
        for r in range(3):
            overlay_row(out, top + r, w - 1 - width, box[r], w)
        bottom = top - 1


def paint_verbose(session: Session, out: list[str], w: int) -> None:
    if not session.verbose:
        return
    overlay_row(
        out, len(out) - 1, 0, pad(" VERBOSE   " + (session.trace or "no keystroke yet"), w), w
    )
