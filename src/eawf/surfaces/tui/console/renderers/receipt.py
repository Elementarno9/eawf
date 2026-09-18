"""receipt: one immutable conformance receipt, what it records, when and where it is kept.

An unknown id renders the absent record rather than failing.

When the console holds the receipt route's read model the card is opened from that: every
cell is copied out of the frozen proof record, so the card holds nothing through which
the receipt could be edited and two opens at one cursor draw the same bytes. A correction
is a later receipt that supersedes this one by reference, and the card names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed
from eawf.surfaces.tui.console.renderers.read_model import native
from eawf.workflow.projection.acceptance import ReceiptCard, ReceiptCardView

DEFAULT_RECEIPT = "EVT-2218"

#: What the card says for an id no held receipt is filed under.
NO_RECEIPT = "∅ no receipt is held under this id at this cursor."

#: The line every receipt card ends on, native or not.
IMMUTABLE = "A receipt is immutable: a later check writes a new one."
_UNKNOWN: Mapping[str, Any] = MappingProxyType(
    {
        "what": "∅ Nothing is recorded for this receipt.",
        "run": "∅ unknown",
        "at": "∅ unknown",
        "by": "∅ unknown",
        "digest": "∅ unavailable",
        "kept": "∅ unknown",
        "body": [],
    }
)
_KEYS: tuple[tuple[str, str], ...] = (("y", "copy"), ("Esc", "back"))


def _label(name: str, pad_to: int) -> str:
    return f"{LABEL_MARK}{name}{CHIP_END}" + " " * (pad_to - len(name))


def _card_lines(card: ReceiptCard | None) -> list[str]:
    """Return the card's content, copied out of the held record or stating its absence."""
    if card is None:
        return [NO_RECEIPT, "", IMMUTABLE]
    superseded = card.supersedes_key or "nothing"
    return [
        f"{_label('RECORDS', 10)}{card.gate_id} · {' · '.join(card.criterion_ids)}",
        f"{_label('RESULT', 10)}{card.result} · started {card.started_at.isoformat()}",
        f"{_label('WRITTEN', 10)}{card.ended_at.isoformat()} · at head {card.head_sha}",
        f"{_label('KEPT', 10)}freshness {card.freshness_key} · supersedes {superseded}",
        "",
        IMMUTABLE,
    ]


def native_card(view: View, model: ReceiptCardView) -> list[str]:
    """Return the receipt card, opened from the read model the daemon served.

    Args:
        view: The render being built; its session names the receipt to open.
        model: The receipt route's read model at the committed cursor.

    Returns:
        The full card, keybar last.
    """
    rid = view.session.subj_id or DEFAULT_RECEIPT
    card = model.card(rid)
    return boxed(
        view,
        crumb=f"Eä ▸ {model.scope_id} ▸ {rid}",
        ctx=f"Receipt {rid} · cursor {model.source_cursor}",
        pre=[],
        title=f"RECEIPT · {rid}",
        lines=_card_lines(card),
        foot=f"y copies this receipt at digest {model.digest}.",
        keys=_KEYS,
    )


def render(view: View) -> list[str]:
    """Return the receipt card, native when its read model is held."""
    model = native(view)
    if isinstance(model, ReceiptCardView):
        return native_card(view, model)
    rid = view.session.subj_id or DEFAULT_RECEIPT
    r = view.fixture.registers.receipts.get(rid, _UNKNOWN)
    lines = [
        f"{_label('RECORDS', 10)}{r['what']}",
        f"{_label('WRITTEN', 10)}{r['at']} by {r['by']} · during {r['run']}",
        f"{_label('KEPT', 10)}with {r['kept']} · digest {r['digest']}",
        "",
        *r["body"],
    ]
    if r["body"]:
        lines.append("")
    lines.append(IMMUTABLE)
    return boxed(
        view,
        crumb=f"Eä ▸ … ▸ {r['kept']} ▸ {rid}",
        ctx=f"Receipt {rid} · {r['at']}",
        pre=[],
        title=f"RECEIPT · {rid}",
        lines=lines,
        foot="y copies this receipt with its digest and where it is kept.",
        keys=_KEYS,
    )
