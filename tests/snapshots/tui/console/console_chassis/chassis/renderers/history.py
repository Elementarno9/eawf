"""history: what changed, from which source, at which revision; the SOURCE colour table
is html-only in the prototype, so the text path emits each fact row already padded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, TSPEC, Fixed, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_FACTS: tuple[tuple[str, str, str, str], ...] = (
    ("MLS-0004 accepted", "41,208", "operator", "14:01"),
    ("BAT-0003 authority invalidated", "41,199", "head moved", "13:58"),
    ("EAWF-0044 criteria set", "41,140", "imported", "Jul 11"),
    ("RUN-3c6ef367 events purged", "41,002", "retention", "Jul 4"),
)


def _note(fr: tuple[str, str, str, str] | None) -> list[str]:
    fid = fr[0].split(" ")[0] if fr else ""
    if fr and fr[2] == "imported":
        return [
            f" IMPORTED    {fid}",
            "             It arrived from a v0.6 wave as alias W-0044, and never counts as",
            "             native completion.",
        ]
    if fr and fr[2] == "retention":
        return [
            f" RESOLVED    {fid}",
            "             Purged under a 7d retention, so there is nothing left to open.",
            "             The fact remains; only its events are gone.",
        ]
    src = fr[2] if fr else "∅ unknown"
    rev = fr[1] if fr else "–"
    return [
        f" SOURCE      {fid}",
        f"             Recorded live by {src} at revision {rev}.",
        "             Nothing was imported and nothing has been purged.",
    ]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    q = dv.filter_of(s)
    facts = [r for r in _FACTS if q.lower() in (r[0] + r[2]).lower()] if q else list(_FACTS)
    dv.sel_in(s, len(facts))
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {fixture.scope} ▸ History", w),
        " what changed, from what source, at which revision",
        bar(w),
    ]
    if s.typing or q:
        rows.append(" SEARCH    \\" + q + ("▏" if s.typing else ""))
    rows.append(TBL([34, 10, 12, 0], 2).head(["FACT", "REVISION", "SOURCE", "WHEN"]))
    for i, r in enumerate(facts):
        rows.append(Fixed(pad(TSPEC["RN"].row(list(r), i == s.sel), w)))
    if not facts:
        rows.append("   nothing matches")
    fr = facts[s.sel] if s.sel < len(facts) else (facts[0] if facts else _FACTS[0])
    foot: list[str] = [thin(w), *_note(fr)]
    while len(rows) + len(foot) < h - 1:
        rows.append(pad("", w))
    rows.extend(foot)
    return build(s, rows, bar_(s, ROUTE_KEYS["history"], w), w, h)
