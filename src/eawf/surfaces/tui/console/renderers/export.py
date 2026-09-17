"""export: the parts a plain-text report of a Run carries and what each costs.

Nothing leaves the machine.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed, g_pad

_PARTS: tuple[tuple[str, str, str, str], ...] = (
    ("timeline", "yes", "41,208 events", "Every event this Run recorded, in order."),
    ("usage and cost", "yes", "~ 4.62 · derived", "Derived from the events, not from a bill."),
    ("transcript", "yes", "6,102 lines known", "What the runner said, quoted exactly."),
    ("secrets", "never", "∅ redacted by policy", "Policy redacts these; no export can carry them."),
    (
        "sandbox decisions",
        "no",
        "142 · space includes",
        "Left out — including them adds 142 lines.",
    ),
)
_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "part"), ("Enter", "export"), ("Esc", "cancel"))


def render(view: View) -> list[str]:
    """Return the export card."""
    s = view.session
    dv.sel_in(s, len(_PARTS))
    lines = [f"{LABEL_MARK}PART                INCLUDED   SIZE{CHIP_END}", ""]
    lines.extend(
        ("▸" if i == s.sel else " ") + g_pad(part, 19) + g_pad(included, 11) + size
        for i, (part, included, size, _why) in enumerate(_PARTS)
    )
    lines.extend(["", "Plain text, one line per fact — nothing leaves the machine."])
    return boxed(
        view,
        crumb="Eä ▸ … ▸ RUN-538453eb ▸ Export",
        ctx="Run RUN-538453eb · claude · WAIT-PERM",
        pre=[],
        title="EXPORT · report this Run",
        lines=lines,
        foot=f"{_PARTS[s.sel][3]}  Enter writes the report and names the path.",
        keys=_KEYS,
    )
