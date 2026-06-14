"""W33: the selected attention row renders plain for readable contrast.

The ``.attention-row.-selected`` rule paints a saturated green selection
rectangle; the semantic content-markup colours ($warn amber title,
$text-muted detail / hint) fail contrast on that green, and Rich
content-markup colours override the widget CSS ``color``. So the selected
row is re-rendered PLAIN -- the ``.-selected`` ``color: $text`` (bright,
bold) then paints every cell at high contrast. Non-selected rows keep their
semantic colours.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.attention import AttentionItem, AttentionKind
from eawf.surfaces.tui.widgets.attention_feed import _render_acute_row

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _item() -> AttentionItem:
    return AttentionItem(
        urgency=Urgency.NORMAL,
        kind=AttentionKind.NEEDS_USER,
        title="P30-I20-W25 Re-close v0.6.0",
        detail="ready to claim",
    )


def test_selected_row_drops_colour_markup_for_contrast() -> None:
    plain = _render_acute_row(_item(), now=_NOW, mode="unicode", selected=True)

    # No per-span colour markup -> the .-selected color:$text paints it bright.
    assert "[$warn]" not in plain
    assert "[$text-muted]" not in plain
    assert "[$accent]" not in plain
    # The content (title + detail + review hint) is still present, just plain.
    assert "P30-I20-W25 Re-close v0.6.0" in plain
    assert "ready to claim" in plain


def test_unselected_row_keeps_semantic_colours() -> None:
    coloured = _render_acute_row(_item(), now=_NOW, mode="unicode", selected=False)

    # The non-selected row keeps the acute $warn title + muted detail on the
    # normal (dark) background where they read fine.
    assert "[$warn]" in coloured
    assert "[$text-muted]" in coloured
    assert "P30-I20-W25 Re-close v0.6.0" in coloured
