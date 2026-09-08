"""evidence.digest: the rung card. What a rung checked, what it ran over and what it
found are the facts; the digest is the runner's own bookkeeping and stays with the
record."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ...chassis.frame import boxed

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def _rung(session: Session, fixture: Fixture) -> dict[str, Any]:
    rungs = fixture.g.EV_RUNGS
    return rungs[min(session.rung or 0, len(rungs) - 1)]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    r = _rung(session, fixture)
    lines: list[str] = [
        f"CHECK      {r['check']}",
        "",
        f"OVER       {r['over']}",
        "",
        f"FOUND      {r['found'][0]}",
    ]
    lines.extend(f"           {f}" for f in r["found"][1:])
    if r["out"] == "pass":
        means = "This rung holds — it does not certify the claim on its own."
    elif r["out"] == "? unknown":
        means = "nothing yet — an unknown rung is not a failed one"
    else:
        means = "nothing · the rung has not run"
    lines.extend(
        [
            "",
            f"AS OF      {r['when']}",
            "RECORD     Kept with CLM-0004 · written by EVT-0119 · the runner keeps its own digests",
            "",
            f"MEANS      {means}",
        ]
    )
    n = str(r["n"]).split(" ")[0]
    return boxed(
        session,
        fixture,
        f"Eä ▸ eawf-core ▸ Evidence ▸ CLM-0004 ▸ Rung {n}",
        f"Claim CLM-0004 · rung {n} of {len(fixture.g.EV_RUNGS)} · as of 14:02",
        [],
        f"RUNG {str(r['n']).upper()} · {re.sub(r'^\? ', '', str(r['out'])).upper()}",
        lines,
        "the finding is the record · the runner keeps its own output",
        [("y", "copy"), ("Esc", "close")],
        w,
        h,
    )


def copy(session: Session, fixture: Fixture) -> str:
    n = str(_rung(session, fixture)["n"]).split(" ")[0]
    return f"urn:eawf:{fixture.scope}:CLM-0004:rung:{n}"
