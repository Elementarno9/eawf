"""notifications: which classes may raise a toast, and which contract decided so.

The route only reads: every row is one row of the presentation matrix the attention
reducer states, over exactly its three columns, in the same boxed card whether the console
runs on the prototype or on a held register. No class takes focus, opens a modal or
changes route; a toast is the one interruption any class may make.
"""

from __future__ import annotations

from eawf.kernel.projection.attention import NOTIFICATION_MATRIX, NotificationPolicy, ToastPolicy
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed, g_pad
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.read_model import route_crumb

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
    """Return the Notifications card, over the held register's crumb when one is held.

    The card is the presentation matrix whether or not a register is held: the Runs the
    register carries are not classes, so the cursor walks the matrix and nothing else.
    """
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
    register = view.register
    return boxed(
        view,
        crumb=(
            route_crumb(view, register, "Notifications").lstrip()
            if register is not None
            else f"Eä ▸ {view.fixture.scope} ▸ Notifications"
        ),
        ctx=(
            f"{dv.plural(len(classes), 'notification class', 'es')} · what may interrupt you"
            if register is not None
            else "26 runs · following"
        ),
        pre=[],
        title="NOTIFICATIONS · what may interrupt",
        lines=lines,
        foot=f"A {class_name(row)} {_EFFECT[row.may_interrupt]}, as the {row.decided_by} decided.",
        keys=_KEYS,
    )
