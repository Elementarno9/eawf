"""settings.stack: the nine-layer stack of the focused key, a read-only card opened with ``i``."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, Grid, View, boxed
from eawf.surfaces.tui.console.renderers import settings as st
from eawf.surfaces.tui.console.renderers.provenance import stack_frame

_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "layer"), ("Esc", "close"))
_NOT_SET = "–"  # noqa: RUF001
# One spec: the head clears the same one-column gutter the rows reserve.
_GRID = Grid([12, 17, 12, 0], 1)


def _label(name: str) -> str:
    return f"{LABEL_MARK}{name}{CHIP_END}" + " " * (9 - len(name))


def render(view: View) -> list[str]:
    """Return the stack card, native when the effective-settings view is held.

    Outside the native mode it draws the prototype catalog's stack, and the settings
    frame when no key is focused.
    """
    if view.settings is not None:
        return stack_frame(view, view.settings)
    s, cfg, w, h = view.session, view.fixture.settings, view.w, view.h
    name = st.sec(s, cfg)
    k = st.cur_key(s, cfg)
    if not k:
        return st.render(view)
    # nine layers always fit: at 80x24 the spacer rows go, never a layer
    r = st.resolve(cfg, name, k[0], st.at(k, 2))
    airy = h > 24
    typ = cfg.types[k[1]] + (f" · {k[3]}" if st.at(k, 3) else "")
    lines = [f"{_label('KEY')}{name}.{k[0]}", f"{_label('TYPE')}{typ}"]
    if airy:
        lines.append("")
    lines.append(_GRID.head(["LAYER", "VALUE", "KIND", "WHERE"]))
    dv.sel_in(s, len(r.stack))
    for i, layer in enumerate(r.stack):
        value = st.show(cfg, layer.value) if layer.is_set else _NOT_SET
        lines.append(_GRID.row([layer.layer, value, layer.kind, layer.where], i == s.sel, w))
    if airy:
        lines.append("")
    focused = r.stack[min(s.sel, len(r.stack) - 1)]
    lens = st.lens(s)
    if not focused.is_set:
        on_this = f"{focused.layer} sets nothing here."
    elif focused.layer == r.winner_layer:
        on_this = f"{focused.layer} sets {st.show(cfg, focused.value)} and wins."
    else:
        on_this = f"{focused.layer} sets {st.show(cfg, focused.value)}, and a layer above it wins."
    lines.extend(
        [
            f"{_label('WINNING')}{r.winner_layer} {st.show(cfg, r.winner_value)}",
            f"{_label('LENS')}{lens} · l cycles the five writable layers",
            f"{_label('ON THIS')}{on_this}",
        ]
    )
    return boxed(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Settings",
        ctx=f"LAYER ▸ {lens}",
        pre=[f"  {name.upper()} · {k[0]}"],
        title="STACK · nine layers",
        lines=lines,
        foot="Read only · a value is changed through the lens, never from here.",
        keys=_KEYS,
    )
