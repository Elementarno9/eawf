"""notifications: which classes may raise a toast, and which contract decided so.

The route only reads: every row is one row of the presentation matrix the attention
reducer states, over exactly its three columns. No class takes focus, opens a modal or
changes route; a toast is the one interruption any class may make.
"""

from __future__ import annotations

from eawf.kernel.projection.attention import NOTIFICATION_MATRIX, NotificationPolicy, ToastPolicy
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed, g_pad
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.registers import native_frame

_KEYS = route_pairs("notifications")

#: What the foot says a class does, by its toast policy.
_EFFECT: dict[ToastPolicy, str] = {
    ToastPolicy.YES: "may raise a toast",
    ToastPolicy.NO: "raises no toast",
    ToastPolicy.ONCE_PER_REVISION: "may raise one toast per revision",
}


def class_name(row: NotificationPolicy) -> str:
    """Return the words a class is drawn with."""
    return row.notification_class.value.replace("_", " ")


def render(view: View) -> list[str]:
    """Return the Notifications card."""
    if view.register is not None:
        return native_frame(view, view.register)
    s = view.session
    classes = NOTIFICATION_MATRIX.classes
    dv.sel_in(s, len(classes))
    lines = [f"{LABEL_MARK}CLASS               TOAST               DECIDED BY{CHIP_END}", ""]
    lines.extend(
        ("▸" if i == s.sel else " ")
        + g_pad(class_name(row), 19)
        + g_pad(row.may_interrupt.value, 20)
        + row.decided_by
        for i, row in enumerate(classes)
    )
    lines.extend(
        [
            "",
            "Read only · a toast is the one interruption; nothing takes focus.",
            "A class that raises no toast still counts in the header's count.",
        ]
    )
    row = classes[s.sel]
    return boxed(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Notifications",
        ctx="26 runs · following",
        pre=[],
        title="NOTIFICATIONS · what may interrupt",
        lines=lines,
        foot=f"A {class_name(row)} {_EFFECT[row.may_interrupt]}, as the {row.decided_by} decided.",
        keys=_KEYS,
    )
