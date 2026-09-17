"""receipt: one immutable conformance receipt, what it records, when and where it is kept.

An unknown id renders the absent record rather than failing.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed

DEFAULT_RECEIPT = "EVT-2218"
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


def render(view: View) -> list[str]:
    """Return the receipt card."""
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
    lines.append("A receipt is immutable: a later check writes a new one.")
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
