"""health: every check with its own result, the focused check's repair docked to the foot."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, g_pad, thin
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "check"),
    ("Enter", "detail"),
    ("\\", "filter"),
    ("Esc", "back"),
)
Check = tuple[str, str, str, str | None]


def _checks() -> list[Check]:
    unknown = chip("info", "? unknown")
    passed = chip("ok", "pass")
    return [
        (
            "Policy revision reachable",
            chip("er", "FAILED"),
            "cannot read revision",
            "Re-point the sandbox policy at a readable revision.",
        ),
        ("Heartbeat probe certified", passed, "14:00", None),
        ("Digest store writable", passed, "14:00", None),
        (
            "Clock skew within 250 ms",
            unknown,
            "probe never ran",
            "Run the clock probe on the host that never answered.",
        ),
        ("Event schema accepted", passed, "14:01", None),
        ("Snapshot restore verified", passed, "13:58", None),
        (
            "Provider rate cards current",
            unknown,
            "no card for the local runner",
            "Attach a rate card for the local runner, or accept it unmetered.",
        ),
        ("Replay digests reproducible", passed, "13:31", None),
        ("Retention sweep on schedule", passed, "09:12", None),
    ]


def render(view: View) -> list[str]:
    """Return the Health frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w, h = view.session, view.w, view.h
    # two columns of air between the longest check name and its result cell
    grid = Grid([30, 11, 0])
    checks = _checks()
    dv.sel_in(s, len(checks))
    body = [
        f" CHECKS       {len(checks)} checks · 1 failed · 2 unknown · 6 pass · as of 14:02",
        grid.head(["CHECK", "RESULT", "REASON"]),
    ]
    body.extend(grid.row([c[0], c[1], c[2]], i == s.sel, w) for i, c in enumerate(checks))
    # the repair follows the cursor at the foot, so it never floats with the list's height
    repair = checks[s.sel][3]
    foot = [
        thin(w),
        " REPAIR       " + (repair or "∅ Nothing to repair · this check passes."),
        "              "
        + (
            "Naming it happens here; running it lives in settings."
            if repair
            else "Its last result stands until the next sweep."
        ),
    ]
    body.extend(g_pad("", w) for _ in range(h - 4 - len(foot) - len(body)))
    body.extend(foot)
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Health",
        ctx="Fleet health · conformance runner · as of 14:02",
        body=body,
        keys=_KEYS,
    )
