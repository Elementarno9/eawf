"""receipt: one immutable conformance receipt, what it records, when it was written and
where it is kept; an unknown id renders the absent record rather than crashing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...chassis.frame import boxed

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_UNKNOWN: dict[str, Any] = {
    "what": "∅ Nothing is recorded for this receipt.",
    "run": "∅ unknown",
    "at": "∅ unknown",
    "by": "∅ unknown",
    "digest": "∅ unavailable",
    "kept": "∅ unknown",
    "body": [],
}
_KEYS: list[tuple[str, str]] = [("y", "copy"), ("Esc", "back")]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    rid = s.subj_id or "EVT-2218"
    r = fixture.g.RECEIPTS.get(rid, _UNKNOWN)
    lines: list[str] = [
        f"\x07RECORDS\x06   {r['what']}",
        f"\x07WRITTEN\x06   {r['at']} by {r['by']} · during {r['run']}",
        f"\x07KEPT\x06      with {r['kept']} · digest {r['digest']}",
        "",
    ]
    lines.extend(r["body"])
    if r["body"]:
        lines.append("")
    lines.append("A receipt is immutable: a later check writes a new one.")
    return boxed(
        s,
        fixture,
        f"Eä ▸ … ▸ {r['kept']} ▸ {rid}",
        f"Receipt {rid} · {r['at']}",
        [],
        f"RECEIPT · {rid}",
        lines,
        "y copies this receipt with its digest and where it is kept.",
        _KEYS,
        w,
        h,
    )
