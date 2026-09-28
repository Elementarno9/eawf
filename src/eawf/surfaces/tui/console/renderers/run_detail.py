"""run.detail: the Run's event timeline with its usage, controls and lineage.

A Run other than the fixture's own renders its record from the register or states the
absence.

The native frame draws one Run -- the session's subject, else the Run under the cursor --
as the packet's labelled facts: its state, the Task it runs, its provider, its usage, its
controls and its lineage, then its timeline. The register states the Run's status and its
Task; every other fact belongs to a producer the console does not read yet, so it wears
the unknown token with the reason rather than a blank. A Run whose lifecycle has ended
says so, and offers no lifecycle verb.
"""

from __future__ import annotations

from datetime import datetime

from eawf.kernel.projection.attention import build_attention_view
from eawf.kernel.projection.registers import UNWRITTEN_REASON
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import NO_VALUE, value_cell
from eawf.surfaces.tui.console.format import clock_time, group, span
from eawf.surfaces.tui.console.frame import Table, View, bar, build, header, route_keys_bar, thin
from eawf.surfaces.tui.console.keybar import KEY, ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.lifecycle import ELAPSED_WORDS
from eawf.surfaces.tui.console.overlays.situations import LOST
from eawf.surfaces.tui.console.renderers.detail import state_of, unknown_frame
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    label,
    more,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import finished_rows, finished_subject, held, restore
from eawf.surfaces.tui.console.width import cell_len

OWN = pt.OWN_RUN
# Cells the timeline's first two columns and their gutter take.
TL_PREFIX = 38


def tl_label(w: int) -> str:
    """Return the priority column's head, abbreviated only below 120 columns."""
    return "PRIORITY" if w >= 120 else "PRI"


def tl_text(w: int) -> int:
    """Return the cells the event detail column gets at width ``w``."""
    return w - TL_PREFIX - 1 - cell_len(tl_label(w))


def tl_header(w: int) -> str:
    """Return the timeline head row.

    Raises:
        ValueError: the head does not fit ``w`` cells.
    """
    line = Table([10, 23, tl_text(w) + 1, 0], 2).head(["TIMELINE", "EVENT", "DETAIL", tl_label(w)])
    if cell_len(line) > w:
        raise ValueError(f"timeline header overruns the frame: {cell_len(line)}/{w}")
    return line


def _facts(view: View, rid: str) -> str:
    s, fx = view.session, view.fixture
    row = dv.fleet_of(fx, rid)
    if dv.own_body(s, OWN):
        return "claude · RUNNING · started 10:00:00"
    if row:
        return f"{row.prov} · {row.state}" + (f" · {row.reason}" if row.reason else "")
    facts = [("PROVIDER", "token"), ("STATE", "field")]
    return dv.subj_facts(fx, rid, facts) or "∅ provider and state unavailable"


def _subject(view: View, spine: SpineView) -> SpineRow | None:
    """Return the Run the frame is about: the session's subject, else the Run under the cursor."""
    found = spine.index_of(view.session.subj_id)
    if found is not None:
        return spine.rows[found]
    return spine.rows[restore(view.session, spine)] if spine.rows else None


def _instant(run: SpineRow, name: str) -> datetime | None:
    """Return one instant a Run states, or ``None`` when it states none readable."""
    stamp = run.facts.get(name)
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def elapsed(view: View, spine: SpineView, run: SpineRow) -> str:
    """Return how long a Run has run: to its end once it ended, else to now.

    A Run that has ended states both stamps, so its elapsed time is a recorded fact. A
    running one is measured to the frame's own instant, or, with no wall clock held, to
    the instant the rows were read -- and then it says so.
    """
    started = _instant(run, "started_at")
    if started is None:
        return f"elapsed {UNKNOWN_WORD}"
    ended = _instant(run, "ended_at")
    to = ended or view.now or spine.generated_at
    if to is None:
        return f"elapsed {UNKNOWN_WORD}"
    text = f"elapsed {span(max(0, int((to - started).total_seconds())))}"
    if ended is None and view.now is None:
        text += f" as of {clock_time(to)}"
    return text


def asked(view: View, run: SpineRow) -> str:
    """Return what the Run is waiting on an answer to, read off the Attention register."""
    register = view.attention
    if register is None:
        return f"{UNKNOWN_WORD} · the attention register has not been read"
    if register.withheld:
        return f"{UNKNOWN_WORD} · {UNWRITTEN_REASON}"
    open_keys = {item.key for item in build_attention_view(register).items}
    for row in register.rows:
        if row.key in open_keys and row.facts.get("subject") == run.key:
            return row.facts.get("question", row.key)
    return "nothing · no open action names this Run"


def lost(view: View, run: SpineRow) -> bool:
    """Return whether the Run is stored as running but is known to have stopped answering.

    The Run register carries no heartbeat, so stopped-answering is read from the decision
    records the console holds -- the same Run states the pause detail reads -- and only for
    a Run whose stored status is the one the registry labels ambiguous.
    """
    status = run.field("status")
    records = view.decisions
    return (
        records is not None
        and status.value == RunStatus.RUNNING.value
        and records.run_states.get(run.key) == LOST
    )


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return the Run frame drawn from the read model the daemon served.

    The connection, the Run's own state and the state of the Task it runs are three
    values on three rows: a Run that finished never reads as a Task that did.

    Args:
        view: The render being built; its session names the Run.
        spine: The Run register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    run = _subject(view, spine)
    key = run.key if run is not None else "no run"
    state = value_cell(run.field("status")).slot if run is not None else UNKNOWN_WORD
    finished = finished_subject(s, spine)
    # a finished Run has no lifecycle left, so the lifecycle menu is not offered
    keys = [e for e in native_keys(s.route) if finished is None or e != KEY["actions"]]
    if run is not None and lost(view, run):
        return unknown_frame(view, spine, run, keys)
    treated = state_of(run) if run is not None else None
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(spine, *([run.parent_key] if run and run.parent_key else []), key),
        summary=f"Run {key} · {state} · {counts(spine)}",
        terminal=finished is not None,
    )
    rows = list(top)
    if finished is not None:
        rows += [*finished_rows(s.route, finished), thin(w)]
    if run is None:
        rows.append(label("RUN", "∅ this scope holds no Run"))
    else:
        facts = run.facts
        task = run.parent_key
        title = facts.get("task_title")
        task_state = facts.get("task_status", UNKNOWN_WORD)
        started = _instant(run, "started_at")
        ended = _instant(run, "ended_at")
        scope = " · ".join(facts[name] for name in ("batch", "milestone", "track") if name in facts)
        attempt = (
            f"attempt {facts['attempt']} of {facts['attempts']}"
            if "attempt" in facts
            else f"attempt {UNKNOWN_WORD}"
        )
        rows += [
            label(
                "STATE",
                f"{state} · {treated.meaning}" if treated else f"{state} · the Run's own lifecycle",
            ),
            *([label("CLOCK", ELAPSED_WORDS[treated.elapsed])] if treated else []),
            label(
                "TASK",
                f"{task}{f' {title}' if title else ''} · task {task_state}"
                if task
                else f"{NO_VALUE} the Run states no Task",
            ),
            label("SCOPE", scope or f"{NO_VALUE} the Task is filed in no Batch"),
            label("PROVIDER", value_cell(run.field("provider")).full),
            label(
                "STARTED",
                (clock_time(started) if started else UNKNOWN_WORD)
                + (f" · for {facts['purpose']}" if "purpose" in facts else ""),
            ),
            label("ENDED", clock_time(ended) if ended else "still running"),
            label("ASKED", asked(view, run)),
            label("USAGE", f"{elapsed(view, spine, run)} · cost {UNKNOWN_WORD}"),
            more("no metering producer states tokens or cost yet"),
            label("CONTROLS", "no control sent from this console"),
            label("LINEAGE", f"{attempt} · forks are not read yet"),
        ]
    rows += [
        thin(w),
        tl_header(w),
        f"   {UNKNOWN_WORD} · the Run's events are read on its transcript route",
    ]
    return build(view, rows, route_keys_bar(view, keys))


def render(view: View) -> list[str]:
    """Return the Run frame, native when a read model is held.

    Raises:
        ValueError: an event's detail is wider than its column.
    """
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    proto = fx.proto
    dv.sel_in(s, len(proto.timeline))
    rid = dv.subj_of(s, OWN)
    parent = "EAWF-0001 ▸ " if rid == OWN else ""
    rows = [
        header(view, f" Eä ▸ … ▸ {parent}{rid}"),
        f" Run {rid} · {_facts(view, rid)} · seq {group(proto.revision)}",
        bar(w),
        tl_header(w),
    ]
    text_w = tl_text(w)
    table = Table([10, 23, text_w + 1, 0], 2)
    for i, event in enumerate(proto.timeline):
        if cell_len(event[2]) > text_w:
            raise ValueError(f"event text exceeds its column: {event[2]}")
        rows.append(table.row(list(event), i == s.sel))
    rows.extend(
        [
            thin(w),
            " USAGE     elapsed 18m 04s of 60m · typical ~22m · tokens out 128,410",
            "           cost ~4.62 of 20.00 · ≈15.38 left",
            "           peak rss ∅ unavailable · probe uncertified",
            thin(w),
            " CONTROLS  no control outstanding · last confirmed 10:01:12",
            " LINEAGE   attempt 1 of 1 · no retry · no fork",
        ]
    )
    if not dv.own_body(s, OWN):
        rows = dv.absent(s, fx, rows, entity_id=rid, what="timeline or usage", w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["run.detail"]))
