"""The launched header keeps one cell clear at its right edge, as it does at its left.

The band stops inside the outer gutter, so a state slot drawn on the frame's last cell sat
flush against the band's edge while the crumb opened one cell in. The bare frame the golden
contract replays keeps the packet's flush slot.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, bar, build
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

W, H = 80, 24


def _head(*, gutter: int, needs: int = 0, crumb: str = " Eä ▸ eawf ▸ Settings") -> str:
    session = Session()
    view = View(
        session=session, fixture=Fixture.from_chrome(load_chrome()), w=W, h=H, gutter=gutter
    )
    head = header_row(session, crumb=crumb, scope="eawf", needs=needs, w=W)
    return build(view, [head, pad(" context", W), bar(W)], pad(" Esc back", W))[0]


# ---------- V-14: the right edge keeps a cell ----------


@pytest.mark.parametrize("needs", [0, 4])
def test_v14_the_launched_header_ends_one_cell_in(needs: int) -> None:
    head = _head(gutter=1, needs=needs)
    assert len(head) == W
    assert head.endswith("● LIVE ")
    assert head.startswith(" Eä ▸ eawf ▸ Settings")
    assert ("!4 NEEDS YOU  ● LIVE " in head) == bool(needs)


def test_v14_the_bare_frame_keeps_the_packet_flush_slot() -> None:
    head = _head(gutter=0)
    assert head.endswith("● LIVE") and len(head) == W


def test_v14_a_crumb_that_leaves_no_spare_padding_keeps_its_slot_flush() -> None:
    crumb = " " + "x" * (W - len("  ● LIVE") - 1)
    head = _head(gutter=1, crumb=crumb)
    assert head.endswith("  ● LIVE") and len(head) == W
