"""notifications: which Run classes may interrupt and who decided.

The route only reads; the policy lives in settings.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed, g_pad
from eawf.surfaces.tui.console.renderers.registers import native_frame

_CLASSES: tuple[tuple[str, str, str], ...] = (
    ("needs permission", "yes", "policy"),
    ("needs your answer", "yes", "policy"),
    ("stopped responding", "yes", "policy"),
    ("run finished", "no", "profile"),
    ("budget passed", "no", "ratified · R23"),
)
_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "class"), ("Esc", "close"))


def render(view: View) -> list[str]:
    """Return the Notifications card."""
    if view.register is not None:
        return native_frame(view, view.register)
    s = view.session
    dv.sel_in(s, len(_CLASSES))
    lines = [f"{LABEL_MARK}CLASS               MAY INTERRUPT   DECIDED BY{CHIP_END}", ""]
    lines.extend(
        ("▸" if i == s.sel else " ") + g_pad(name, 19) + g_pad(may, 16) + by
        for i, (name, may, by) in enumerate(_CLASSES)
    )
    lines.extend(
        [
            "",
            "Read only · settings ▸ interface owns this policy.",
            "A muted class still counts in !N NEEDS YOU.",
        ]
    )
    _name, may, by = _CLASSES[s.sel]
    verb = "interrupts you" if may == "yes" else "does not interrupt you"
    return boxed(
        view,
        crumb="Eä ▸ eawf-core ▸ Notifications",
        ctx="26 runs · following",
        pre=[],
        title="NOTIFICATIONS · what may interrupt",
        lines=lines,
        foot=f"A run in this class {verb}, and {by} decided that.",
        keys=_KEYS,
    )
