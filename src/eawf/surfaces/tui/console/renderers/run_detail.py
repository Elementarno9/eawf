"""run.detail: the Run's event timeline with its usage, controls and lineage.

A Run other than the fixture's own renders its record from the register or states the
absence.

The native frame draws one Run -- the session's subject, else the Run under the cursor --
timeline first, then the packet's labelled facts: its state, the Task it runs, its
provider, its usage, its controls and its lineage. The timeline pane draws the Run's latest
rows as the daemon grouped them: each event as its token and its whole word, a repeated
low-priority run of events as its kind, a count and a span. Before that read arrives the
pane points at the transcript, which Enter opens. The register states the Run's status and
its Task; its usage is the Run's own usage read -- spent against the sealed caps with an
estimated remainder, and the elapsed time against its limit and the typical time of its
kind, never a remaining time. Every other fact belongs to a producer the console does not
read yet, so it wears the unknown token with the reason rather than a blank. A Run whose
lifecycle has ended says so, and offers no lifecycle verb.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from eawf.kernel.economics.spend import RunUsageView
from eawf.kernel.projection.attention import build_attention_view
from eawf.kernel.projection.registers import UNWRITTEN_REASON
from eawf.kernel.projection.run_timeline import RunTimeline, TimelineGroup
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.projection.transcript import LANES
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import NO_VALUE, value_cell
from eawf.surfaces.tui.console.format import clock_time, group, instant, span
from eawf.surfaces.tui.console.frame import (
    Table,
    Titled,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import KEY, ROUTE_KEYS
from eawf.surfaces.tui.console.lifecycle import ELAPSED_WORDS
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.overlays.situations import LOST
from eawf.surfaces.tui.console.renderers.budget_lines import cost_line, time_line, tokens_line
from eawf.surfaces.tui.console.renderers.detail import state_of, subject_line, unknown_frame
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    label,
    more,
    native_head,
    restore,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import (
    detail_keys,
    finished_rows,
    finished_subject,
    held,
)
from eawf.surfaces.tui.console.renderers.transcript import NATIVE_GLYPH, UNGLYPHED
from eawf.surfaces.tui.console.width import cell_len, clip_words, pad

OWN = pt.OWN_RUN
# Cells the timeline's first two columns and their gutter take.
TL_PREFIX = 38

#: The priority legend the timeline's glyph lane is read by.
TIMELINE_LEGEND = "P0  P1  P2"

#: What the timeline pane says before its rows arrive: the transcript draws the events.
NO_EVENTS = "events are drawn in the transcript · Enter opens it"

#: How many of a Run's latest timeline rows the pane draws; the transcript holds them all.
TIMELINE_ROWS = 6


def event_cell(group: TimelineGroup) -> str:
    """Return a timeline row's kind cell: the token and whole word, or the coalesced run.

    One event reads as its glyph, its lane word and the phase its kind names. A coalesced
    run reads as its word, the multiplication sign and a count, then its span in one unit,
    never as a range of stamps.
    """
    kind = group.event_kind.value
    lane = LANES.get(group.event_kind)
    word = lane or kind.split("_")[0]
    if group.coalesced:
        return f"{word} ×{group.count} · {_span_word(group.span_seconds)}"  # noqa: RUF001
    if lane is None:
        return f"{UNGLYPHED} {kind.replace('_', ' ')}"
    phase = kind.rsplit("_", 1)[1] if "_" in kind else ""
    return f"{NATIVE_GLYPH.get(lane, UNGLYPHED)} {lane}" + (f" · {phase}" if phase else "")


def _span_word(seconds: int) -> str:
    """Return a span as one number and one unit, as a coalesced row states it."""
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m" if seconds < 3600 else f"{seconds // 3600}h"


def timeline_rows(view: View, timeline: RunTimeline) -> list[str]:
    """Return the timeline pane: its head and the Run's latest rows as the daemon grouped them."""
    w, wide = view.w, view.wide
    text_w = tl_text(w, wide)
    table = Table([10, 23, text_w + 1, 0], 2)
    shown = timeline.groups[-TIMELINE_ROWS:]
    rows = [timeline_head(w), tl_header(w, wide)]
    earlier = len(timeline.groups) - len(shown)
    if earlier:
        rows.append(f"   … {earlier} earlier rows · Enter opens the transcript")
    for item in shown:
        detail = (
            f"sequences {item.first_sequence}–{item.last_sequence}"  # noqa: RUF001
            if item.coalesced
            else f"sequence {item.first_sequence}"
        )
        when = "" if item.coalesced else clock_time(item.first_at)
        cells = [when, event_cell(item), clip_words(detail, text_w), item.priority.value]
        rows.append(table.row(cells))
    if not shown:
        rows.append("   ∅ this Run has produced no event yet")
    return rows


def timeline_head(w: int) -> Titled:
    """Return the timeline pane's head: its title, and the legend over the glyph lane.

    The lane runs down the frame's right side, so its legend is set against the right edge,
    one cell in, as the title is one cell in from the left.
    """
    return Titled(pad(" TIMELINE", w - cell_len(TIMELINE_LEGEND) - 1) + TIMELINE_LEGEND + " ")


def tl_label(wide: bool) -> str:
    """Return the priority column's head, abbreviated below the wide layout."""
    return "PRIORITY" if wide else "PRI"


def tl_text(w: int, wide: bool) -> int:
    """Return the cells the event detail column gets in a ``w``-cell frame."""
    return w - TL_PREFIX - 1 - cell_len(tl_label(wide))


def tl_header(w: int, wide: bool) -> str:
    """Return the timeline head row.

    Raises:
        ValueError: the head does not fit ``w`` cells.
    """
    line = Table([10, 23, tl_text(w, wide) + 1, 0], 2).head(
        ["TIMELINE", "EVENT", "DETAIL", tl_label(wide)]
    )
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
    """Return the Run the frame is about, pinning it as the subject when none was named.

    A frame opened with no subject is about the Run it first drew, and stays about it: a
    cursor move never re-derives the subject. A subject the register does not hold is
    about nothing, never about whichever Run sits at the cursor.
    """
    s = view.session
    if s.subj_id is not None:
        found = spine.index_of(s.subj_id)
        return spine.rows[found] if found is not None else None
    if not spine.rows:
        return None
    run = spine.rows[restore(s, spine)]
    s.subj_id = run.key
    return run


def _instant(run: SpineRow, name: str) -> datetime | None:
    """Return one instant a Run states, or ``None`` when it states none readable."""
    return instant(run.facts.get(name))


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


def usage_rows(view: View, spine: SpineView, run: SpineRow) -> list[str]:
    """Return the usage pane: time, tokens and cost, each a budget line, never a countdown.

    Until the Run's usage read arrives the pane states its elapsed time and says the rest
    was not read, rather than drawing a zero.
    """
    usage = _held_usage(view.live, run.key)
    took = elapsed(view, spine, run)
    if usage is None:
        return [
            label("USAGE", f"{took} · cost {UNKNOWN_WORD}"),
            more("the Run's usage read has not arrived"),
        ]
    estimated = usage.quality not in (None, "measured")
    return [
        label("USAGE", time_line(took, usage.wall_seconds, usage.typical_seconds)),
        more(tokens_line(usage.tokens, usage.cap_tokens, estimated=estimated)),
        more(cost_line(usage.cost_microusd, usage.cap_cost_microusd)),
    ]


def _held_usage(live: Mapping[str, object], key: str) -> RunUsageView | None:
    """Return the usage read's answer for Run *key*, or ``None`` before it arrives."""
    return next(
        (item for item in live.values() if isinstance(item, RunUsageView) and item.run_key == key),
        None,
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
    if run is not None and lost(view, run):
        return unknown_frame(view, spine, run, detail_keys(view, spine))
    treated = state_of(run) if run is not None else None
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(
            view, spine, *([run.parent_key] if run and run.parent_key else []), key
        ),
        summary=_summary(run, state, spine, finished=finished is not None, w=w),
        terminal=finished is not None,
    )
    rows = list(top)
    if finished is not None:
        rows += [*finished_rows(s.route, finished, label), thin(w)]
    if run is not None and view.timeline is not None:
        rows += [*timeline_rows(view, view.timeline), thin(w)]
    else:
        pane = NO_EVENTS if run is not None else "no events · no Run is held to record them"
        rows += [timeline_head(w), f"   {pane}", thin(w)]
    if run is None:
        missing = f"∅ {s.subj_id} is not held in this scope" if s.subj_id else ""
        rows.append(label("RUN", missing or "∅ this scope holds no Run"))
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
            thin(w),
            *usage_rows(view, spine, run),
            thin(w),
            label("CONTROLS", "no control sent from this console"),
            label("LINEAGE", f"{attempt} · forks are not read yet"),
        ]
    # Enter opens the transcript the timeline pane names, so it sits beside the event keys
    keys = detail_keys(view, spine)
    at = keys.index(KEY["up_event"]) + 1 if KEY["up_event"] in keys else 0
    enter = [KEY["transcript"]] if run is not None else []
    # the frame draws no timeline -- the transcript does -- so the event keys walk nothing
    shown = [e for e in [*keys[:at], *enter, *keys[at:]] if e != KEY["up_event"]]
    return build(view, rows, route_keys_bar(view, shown))


def _summary(run: SpineRow | None, state: str, spine: SpineView, *, finished: bool, w: int) -> str:
    """Return the Run frame's summary: the Run, its state and the sequence it was read at.

    With no Run to name, the frame says what it holds instead, as a list frame does.
    """
    if run is None:
        return counts(spine)
    said = f"{state} · final" if finished else state
    return subject_line(run, f"{said} · seq {group(int(spine.source_cursor))}", w)


def open_transcript(ctx: Ctx) -> None:
    """Open the transcript of the Run the frame is about, which the timeline pane names."""
    s = ctx.s
    run = s.subj_id or s.sel_id
    if not isinstance(ctx.projection, SpineView) or run is None:
        ctx.noop("Enter")
        return
    go(ctx, "transcript", f"transcript · {run}", run)


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
        tl_header(w, view.wide),
    ]
    text_w = tl_text(w, view.wide)
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
