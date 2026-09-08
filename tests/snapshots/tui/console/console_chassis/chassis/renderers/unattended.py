"""unattended: the dispatch queue the daemon owns; a and d ask it through the consequence
card, Enter goes to the focused row's run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis import keys
from ...chassis.frame import GTBL, chip, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [
    ("↑↓", "row"),
    ("Enter", "run"),
    ("a", "request pause"),
    ("d", "request drain"),
    ("Esc", "back"),
]
_RUNS = ["RUN-538453eb", "RUN-7b0e4d31", "RUN-1c93af08", "RUN-4e2b6c77"]


def unattended_run(session: Session) -> str:
    return _RUNS[min(session.sel or 0, len(_RUNS) - 1)]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    q = [
        ["RUN-538453eb", "EAWF-0042", chip("ok", "RUNNING"), "~ 62%"],
        ["RUN-7b0e4d31", "EAWF-0044", chip("ok", "RUNNING"), "~ 18%"],
        ["RUN-1c93af08", "EAWF-0051", chip("info", "QUEUED"), "∅ not started"],
        ["RUN-4e2b6c77", "EAWF-0052", chip("info", "QUEUED"), "∅ not started"],
    ]
    dv.sel_in(s, len(q))
    un = GTBL([17, 12, 11, 0])
    b: list[str] = [
        " QUEUE        9 queued · 4 running · 2 forced sequential",
        un.head(["RUN", "TASK", "STATE", "PROGRESS"]),
    ]
    for i, x in enumerate(q):
        b.append(un.row(x, i == s.sel, w))
    b.extend(
        [
            thin(w),
            " PLAN         Concurrency 4 of 6 — derived from the dependency graph.",
            "              EAWF-0051 waits on EAWF-0042 · forced sequential",
            thin(w),
            " CONTROL      This surface observes — every verb is a daemon request.",
            "              the daemon accepted request pause on RUN-7b0e4d31 at 13:58",
        ]
    )
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Unattended",
        "Dispatch queue · the daemon owns scheduling · 14:02",
        b,
        _KEYS,
        w,
        h,
    )


def seam(ctx: keys.Ctx, key: str, shift: bool) -> bool:
    s = ctx.s
    if keys.busy(s):
        return False
    if key in ("a", "d"):
        pause = key == "a"
        s.c_target = {
            "verb": "request pause" if pause else "request drain",
            "state": None,
            "id": "RUN-7b0e4d31",
            "kind": "dispatch queue",
            "effects": "the daemon is asked to pause the queue at its next safe point"
            if pause
            else "the daemon is asked to stop claiming new work and finish what it holds",
            "not": "it does not stop a run that is already claimed"
            if pause
            else "it does not cancel anything already running",
        }
        s.overlay = "consequence"
        ctx.log(key, f"{s.c_target['verb']} → consequence preview")
        return True
    if key == "Enter":
        keys.go(ctx, "run.detail", "the run this dispatch row is about", unattended_run(s))
        return True
    return False
