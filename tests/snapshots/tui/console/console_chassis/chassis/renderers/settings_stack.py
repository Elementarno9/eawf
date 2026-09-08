"""settings.stack: the nine-layer stack of the focused key, a read-only card opened with i."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, boxed
from ...chassis.renderers import settings as st

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    cfg = fixture.settings
    # one spec: the header clears the same 1-column marker gutter the rows reserve
    sk = GTBL([12, 17, 12, 0], 1)
    name = st.sec(s, cfg)
    k = st.cur_key(s, cfg)
    if not k:
        return st.render(session, fixture, w, h)
    # nine layers always fit: at 80x24 the spacer rows go, never a layer
    r = st.resolve(cfg, name, k[0], k[2] if len(k) > 2 else None)
    airy = h > 24
    lines: list[str] = [
        "\x07KEY\x06      " + f"{name}.{k[0]}",
        "\x07TYPE\x06     " + cfg.TYPE[k[1]] + (f" · {k[3]}" if len(k) > 3 and k[3] else ""),
    ]
    if airy:
        lines.append("")
    lines.append(sk.head(["LAYER", "VALUE", "KIND", "WHERE"]))
    dv.sel_in(s, len(r.stack))
    for i, layer in enumerate(r.stack):
        lines.append(
            sk.row(
                [
                    layer.layer,
                    st.show(cfg, layer.value) if layer.set_ else "–",
                    layer.kind,
                    layer.where,
                ],
                i == s.sel,
                w,
            )
        )
    if airy:
        lines.append("")
    lr = r.stack[min(s.sel or 0, len(r.stack) - 1)]
    lines.append("\x07WINNING\x06  " + f"{r.winner_layer} {st.show(cfg, r.winner_value)}")
    lines.append("\x07LENS\x06     " + f"{st.lens(s)} · l cycles the five writable layers")
    if lr.set_:
        on_this = f"{lr.layer} sets {st.show(cfg, lr.value)}" + (
            " and wins." if lr.layer == r.winner_layer else ", and a layer above it wins."
        )
    else:
        on_this = f"{lr.layer} sets nothing here."
    lines.append("\x07ON THIS\x06  " + on_this)
    return boxed(
        session,
        fixture,
        "Eä ▸ eawf-core ▸ Settings",
        f"LAYER ▸ {st.lens(s)}",
        [f"  {name.upper()} · {k[0]}"],
        "STACK · nine layers",
        lines,
        "Read only · a value is changed through the lens, never from here.",
        [("↑↓", "layer"), ("Esc", "close")],
        w,
        h,
    )
