"""sandbox.log: authorisation decisions, the focused one explained at the foot; Enter goes
to the run the decision was about and p to the policy it was read against."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis import keys
from ...chassis.frame import GTBL, chip, g_frame, g_pad, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [
    ("↑↓", "row"),
    ("Enter", "run"),
    ("\\", "filter"),
    ("p", "policy"),
    ("Esc", "back"),
]
_RUNS = ["RUN-538453eb", "RUN-538453eb", "RUN-7b0e4d31", "RUN-7b0e4d31"]


def _decisions() -> list[list[str]]:
    return [
        [
            "14:02",
            chip("er", "denied"),
            "RUN-538453eb",
            "write outside root",
            "fs.write.scope = project root",
        ],
        [
            "14:01",
            chip("ok", "allowed"),
            "RUN-538453eb",
            "read repository",
            "fs.read.scope = repository",
        ],
        [
            "14:00",
            chip("ok", "allowed"),
            "RUN-7b0e4d31",
            "network egress pinned",
            "net.egress = pinned hosts",
        ],
        [
            "13:59",
            chip("er", "denied"),
            "RUN-7b0e4d31",
            "network egress open",
            "net.egress = pinned hosts",
        ],
    ]


def sandbox_run(session: Session) -> str:
    return _RUNS[min(session.sel or 0, len(_RUNS) - 1)]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    d = _decisions()
    dv.sel_in(s, len(d))
    sb = GTBL([7, 10, 17, 0], 1)
    b: list[str] = [
        " WINDOW       142 decisions · 6 denied · 4 of 142 shown",
        sb.head(["TIME", "DECISION", "RUN", "REASON"]),
    ]
    for i, x in enumerate(d):
        b.append(sb.row(x[:4], i == s.sel, w))
    cur = d[s.sel]
    foot = [
        thin(w),
        f" DECISION     {cur[1]} · {cur[3]}",
        f"              {cur[2]} · rule {cur[4]}",
        "              pol-2026-08-11.3 · the revision makes it explicable",
    ]
    while len(b) + len(foot) < h - 4:
        b.append(g_pad("", w))
    b.extend(foot)
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Sandbox log",
        "Authorisation decisions for every agent in eawf-core",
        b,
        _KEYS,
        w,
        h,
    )


def seam(ctx: keys.Ctx, key: str, shift: bool) -> bool:
    if keys.busy(ctx.s):
        return False
    if key == "p":
        ctx.notify("settings ▸ sandbox · pol-2026-08-11.3", "opened")
        keys.go(ctx, "settings", "the policy these decisions were read against")
        return True
    if key == "Enter":
        keys.go(ctx, "run.detail", "the run this decision was about", sandbox_run(ctx.s))
        return True
    return False
