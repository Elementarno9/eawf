"""cost.ceiling: spend against the governor ceiling; observe only, the ceiling moves in settings.

The native frame draws the ceiling, the Runs its hard limit stopped and the spend by
provider from the spend read, and binds no verb that moves a limit. Until that read
arrives each renders unknown rather than a zero. A stop is listed from the run ledger's
budget notices, and one no confirmed control effect answers reads unknown rather than
stopped, never inferred from a terminal status. A provider no reading priced reads
``∅ unmetered``. The budget notice's own contract says what a crossing does.
"""

from __future__ import annotations

from collections.abc import Mapping

from eawf.kernel.economics.spend import CostCeilingView, ProviderSpend, StoppedRun
from eawf.kernel.projection.registers import RegisterView, budget_reading
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.format import clock_time, group
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.renderers.budget_lines import (
    UNMETERED,
    cost_line,
    money,
    tokens_line,
)
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    finish,
    label,
    more,
    native_head,
    route_crumb,
    wrapped,
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


def held_ceiling(live: Mapping[str, object]) -> CostCeilingView | None:
    """Return the spend read's answer among the route's live reads, or ``None`` before it."""
    return next((item for item in live.values() if isinstance(item, CostCeilingView)), None)


def stopped_key(session: Session, ceiling: CostCeilingView) -> str | None:
    """Return the Run the cursor sits on in the read's stopped list, or nothing when empty."""
    if not ceiling.stopped:
        return None
    return ceiling.stopped[min(max(session.sel, 0), len(ceiling.stopped) - 1)].run_key


def _stop_row(stop: StoppedRun) -> list[str]:
    """Return one stop's cells: the Run, when it crossed, and what vouches for the stop."""
    said = "hard limit · stopped" if stop.confirmed else f"{UNKNOWN_WORD} · no confirmed stop"
    reading = f"{group(stop.observed_tokens)} of {group(stop.cap_tokens)} tokens"
    return [stop.run_key, clock_time(stop.noticed_at), f"{said} · {reading}"]


def _spend_row(row: ProviderSpend) -> str:
    """Return one provider's spend with its pricing quality, unmetered never a zero."""
    tokens = f"tokens {group(row.tokens)}" if row.tokens is not None else "tokens ∅"
    if row.cost_microusd is None:
        cost = f"{UNMETERED} — no rate card"
    elif row.pricing == "partial":
        cost = f"≥{money(row.cost_microusd)} · partly unmetered"
    else:
        cost = money(row.cost_microusd)
    runs = f"{row.runs} run{'s' if row.runs != 1 else ''}"
    return f"{row.provider}  {cost} · {tokens} · {runs}"


def _read_body(view: View, ceiling: CostCeilingView) -> list[str]:
    """Return the ceiling, the stops and the spend the spend read answered."""
    s, w = view.session, view.w
    dv.sel_in(s, len(ceiling.stopped))
    flight = f"{ceiling.live_runs} live Run{'s' if ceiling.live_runs != 1 else ''}"
    tokens = tokens_line(ceiling.spent_tokens, ceiling.ceiling_tokens, held=ceiling.held_tokens)
    body = [
        label("CEILING", tokens),
        more(
            cost_line(
                ceiling.spent_cost_microusd,
                ceiling.ceiling_cost_microusd,
                partial=ceiling.spent_cost_pricing == "partial",
            )
        ),
        more(f"in flight · {flight}"),
        label("OWNED BY", f"settings ▸ {ceiling.owner}"),
        thin(w),
    ]
    if ceiling.stopped:
        grid = Grid([17, 10, 0])
        body += [" STOPPED", grid.head(["RUN", "AT", "STOP"])]
        body += [grid.row(_stop_row(stop), i == s.sel, w) for i, stop in enumerate(ceiling.stopped)]
    else:
        body.append(label("STOPPED", "none · no hard limit has stopped a Run"))
    spend = [_spend_row(row) for row in ceiling.providers] or ["none · no Run reported usage"]
    return [*body, thin(w), label("SPEND", spend[0]), *(more(row) for row in spend[1:])]


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
    ceiling = held_ceiling(view.live)
    if ceiling is None:
        dv.sel_in(view.session, 0)
    top = native_head(
        view,
        register,
        crumb_text=route_crumb(view, register, "Cost ceiling"),
        summary="spend against ceiling · observe only",
    )
    answer = "interrupts you" if reading.interrupts else "does not interrupt you"
    held = (
        _read_body(view, ceiling)
        if ceiling is not None
        else [
            label("CEILING", f"{UNKNOWN_WORD} spent of ? · the spend read has not arrived"),
            label("OWNED BY", OWNER),
            thin(w),
            # no stopped Run is listed, so no table head stands over rows that are not there
            *wrapped("STOPPED", value_cell(reading.stopped).full, w),
            thin(w),
            label("SPEND", f"{UNKNOWN_WORD} · the spend read has not arrived"),
            more("a provider with no rate card reads ∅ unmetered, never zero"),
        ]
    )
    body = [
        *held,
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
