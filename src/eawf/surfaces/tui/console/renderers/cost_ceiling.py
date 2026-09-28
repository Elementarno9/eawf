"""cost.ceiling: spend against the day's ceiling; observe only, the ceiling moves in settings.

The native frame draws the ceiling, the Runs its hard limit stopped and the spend by
provider, and binds no verb that moves a limit. The governor ceiling and the spend
accounting are not yet read into the console, so each renders unknown rather than a zero;
which Runs a cap stopped is a run-ledger fact, so that list is unknown too, never inferred
from a terminal status. The budget notice's own contract says what a crossing does.
"""

from __future__ import annotations

from eawf.kernel.projection.registers import RegisterView, budget_reading
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    finish,
    label,
    more,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.session import Session

_KEYS = route_pairs("cost.ceiling")
_STOPPED: tuple[tuple[str, str, str], ...] = pt.STOPPED_RUNS


def stopped_run(session: Session) -> str | None:
    """Return the Run the cursor sits on in the stopped list, or nothing when it is empty.

    The footer advertises ``Enter run``, so the key has to name a Run; this is the one
    place the list's shape is read, rather than the dispatcher reaching into it.
    """
    if not _STOPPED:
        return None
    return _STOPPED[min(max(session.sel, 0), len(_STOPPED) - 1)][0]


#: Where the ceiling is owned: the Settings section whose governor field binds it.
OWNER = "settings ▸ the governor field that binds the ceiling"


def native_frame(view: View, register: RegisterView) -> list[str]:
    """Return the Cost ceiling frame drawn from the register the daemon served.

    Args:
        view: The render being built.
        register: The route's register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    w = view.w
    reading = budget_reading(register)
    dv.sel_in(view.session, 0)
    top = native_head(
        view,
        register,
        crumb_text=route_crumb(register, "Cost ceiling"),
        summary=f"spend against ceiling · observe only · {counts(register)}",
    )
    answer = "interrupts you" if reading.interrupts else "does not interrupt you"
    body = [
        label("CEILING", f"{UNKNOWN_WORD} spent of ? · no governor ceiling is read"),
        label("OWNED BY", OWNER),
        thin(w),
        label("STOPPED", value_cell(reading.stopped).full),
        Grid([17, 8, 0]).head(["RUN", "AT", "REASON"]),
        thin(w),
        label("SPEND", f"{UNKNOWN_WORD} · no spend accounting is read"),
        more("a provider with no rate card reads ∅ unmetered, never zero"),
        label("BUDGET", f"a crossing opens {reading.control} · leaves {reading.terminal_status}"),
        more(f"a budget notice {answer}"),
        thin(w),
        label("AUTHORITY", "This surface observes — the ceiling moves in settings."),
    ]
    return finish(view, top, body, _KEYS)


def render(view: View) -> list[str]:
    """Return the Cost ceiling frame."""
    if view.register is not None:
        return native_frame(view, view.register)
    s, w = view.session, view.w
    grid = Grid([17, 8, 0])
    dv.sel_in(s, len(_STOPPED))
    body = [
        " CEILING      ~41.20 of 60.00 today · ≈18.80 left",
        " OWNED BY     settings ▸ execution ▸ concurrency · policy",
        thin(w),
        " STOPPED",
        grid.head(["RUN", "AT", "REASON"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_STOPPED))
    body.extend(
        [
            thin(w),
            " SPEND        claude  ~31.40 · 8 runs",
            "              codex   ~9.80 · 6 runs",
            "              local   ∅ unmetered — no rate card",
            thin(w),
            " AUTHORITY    This surface observes — the ceiling moves in settings.",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Cost ceiling",
        ctx="spend against ceiling · observe only",
        body=body,
        keys=_KEYS,
    )
