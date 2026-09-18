"""evidence.digest: the rung card.

What a rung checked, what it ran over and what it found are the facts; the digest is the
runner's own bookkeeping and stays with the record.
"""

from __future__ import annotations

import re
from typing import Any

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, boxed
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame
from eawf.surfaces.tui.console.session import Session

_UNKNOWN_PREFIX = re.compile(r"^\? ")
_KEYS: tuple[tuple[str, str], ...] = (("y", "copy"), ("Esc", "close"))


def rung_of(session: Session, fixture: Fixture) -> dict[str, Any]:
    """Return the rung the card was opened on, the last rung past the end."""
    rungs = fixture.registers.ev_rungs
    return rungs[min(session.rung, len(rungs) - 1)]


def _means(outcome: str) -> str:
    if outcome == "pass":
        return "This rung holds — it does not certify the claim on its own."
    if outcome == "? unknown":
        return "nothing yet — an unknown rung is not a failed one"
    return "nothing · the rung has not run"


def render(view: View) -> list[str]:
    """Return the rung card, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    fx = view.fixture
    r = rung_of(view.session, fx)
    found = list(r["found"])
    lines = [
        f"CHECK      {r['check']}",
        "",
        f"OVER       {r['over']}",
        "",
        f"FOUND      {found[0]}",
        *(f"           {f}" for f in found[1:]),
        "",
        f"AS OF      {r['when']}",
        "RECORD     Kept with CLM-0004 · written by EVT-0119 · the runner keeps its own digests",
        "",
        f"MEANS      {_means(r['out'])}",
    ]
    n = str(r["n"]).split(" ")[0]
    outcome = _UNKNOWN_PREFIX.sub("", str(r["out"])).upper()
    return boxed(
        view,
        crumb=f"Eä ▸ eawf-core ▸ Evidence ▸ CLM-0004 ▸ Rung {n}",
        ctx=f"Claim CLM-0004 · rung {n} of {len(fx.registers.ev_rungs)} · as of 14:02",
        pre=[],
        title=f"RUNG {str(r['n']).upper()} · {outcome}",
        lines=lines,
        foot="the finding is the record · the runner keeps its own output",
        keys=_KEYS,
    )


def copy(session: Session, fixture: Fixture) -> str:
    """Return the rung's stable URN."""
    n = str(rung_of(session, fixture)["n"]).split(" ")[0]
    return f"urn:eawf:{fixture.scope}:CLM-0004:rung:{n}"
