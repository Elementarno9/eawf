"""The Enter-opened cards drawn from the record each is bound to.

The draft and marker cards are overlays; the artifact, rung record and step cards are the
render forms of the ``campaign.artifact``, ``evidence.digest`` and ``campaign.step``
sub-surfaces. Every card is about the one record captured when it opened, and nothing
inside it moves that record. A card opened for an id with no record says so under that
id rather than drawing an empty frame, and a card promises only ``Esc`` and ``y copy``
unless a region of it has rows, when it promises exactly the keys that move within them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from eawf.surfaces.tui.console.decisions import (
    ArtifactRecord,
    ClaimRecord,
    DraftRecord,
    MarkerFact,
    MarkerRecord,
    RungOutcome,
    RungRecord,
    StepRecord,
    StepState,
    short_time,
)
from eawf.surfaces.tui.console.frame import (
    Grid,
    Scrollbar,
    View,
    boxed,
    g_frame,
    g_row,
    lab,
    prose,
    thin,
)
from eawf.surfaces.tui.console.keybar import Pair
from eawf.surfaces.tui.console.overlays.bound import CARD_OVERLAYS
from eawf.surfaces.tui.console.overlays.chassis import cursor_foot
from eawf.surfaces.tui.console.overlays.decision import NO_VALUE, cont, decision_frame
from eawf.surfaces.tui.console.overlays.decision import lab as dlab
from eawf.surfaces.tui.console.overlays.situations import SCREEN_RUNG, rung_means
from eawf.surfaces.tui.console.reads import can_mutate
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import TRUTH
from eawf.surfaces.tui.console.width import pad

NONE = TRUTH["unavailable"].unicode
_ESC: Pair = ("Esc", "back")
_CLOSE: Pair = ("Esc", "close")
_COPY: Pair = ("y", "copy")


def absent_card(view: View, asked: str) -> list[str]:
    """Return a card opened for ``asked`` whose record does not exist: it says so under the id."""
    s = view.session
    name = s.overlay if s.overlay in CARD_OVERLAYS else s.route
    body = [dlab("RECORD", f"{NONE} none · nothing is recorded for {asked}")]
    if s.overlay in CARD_OVERLAYS:
        return decision_frame(
            view, name=name, subject=asked, context=f"{asked} · no record", body=body, keys=[_ESC]
        )
    return g_frame(
        view,
        crumb=f"Eä ▸ {asked}",
        ctx=f"{asked} · no record",
        body=[g_row(row, view.w) for row in body],
        keys=[_ESC],
    )


# ---------- the draft card ----------

#: The promotion fields and what each answers; the draft's field of that name answers it.
PROMOTION: tuple[tuple[str, str], ...] = (
    ("criteria", "what proof will show it is done"),
    ("owner", "who answers for it"),
    ("batch", "where it will be integrated"),
)


def unanswered(d: DraftRecord) -> list[str]:
    """Return the promotion fields ``d`` has not answered, in checklist order."""
    return [field for field, _what in PROMOTION if getattr(d, field) is None]


def _listed(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def promote_refusal(d: DraftRecord) -> str | None:
    """Return the sentence refusing ``p`` on ``d``, derived from its fields; ``None`` if none."""
    missing = unanswered(d)
    if not missing:
        return None
    verb = "are" if len(missing) > 1 else "is"
    return f"p is refused while {_listed(missing)} {verb} unanswered."


def draft_keys(session: Session) -> list[Pair]:
    """Return the draft card's keybar, which is also every key it acts on."""
    if not can_mutate(session):
        return [("↑↓", "field"), _ESC]
    return [("↑↓", "field"), ("Enter", "set"), ("p", "promote"), ("x", "defer"), _ESC]


def render_draft(view: View, d: DraftRecord) -> list[str]:
    """Return the draft card over backlog row ``d``."""
    s, w = view.session, view.w
    s.draft_field = max(0, min(len(PROMOTION) - 1, s.draft_field))
    missing = unanswered(d)
    body = [
        dlab("DUE", f"{d.due_scope} · {d.due}"),
        dlab("NEEDS", " · ".join(missing) or "nothing — it can be promoted"),
        thin(w),
        f" {pad('PROMOTION', 9)}  {pad('FIELD', 9)}{pad('WHAT IT ANSWERS', 34)}ANSWERED BY",
    ]
    for i, (field, what) in enumerate(PROMOTION):
        mark = "▸ " if i == s.draft_field else "  "
        value = getattr(d, field) or f"{NONE} not answered yet"
        body.append(f" {pad('', 9)}{mark}{pad(field, 9)}{pad(what, 34)}{value}")
    body += [
        thin(w),
        dlab("NOT", "Promoting does not dispatch it and does not claim a run."),
        cont(promote_refusal(d) or f"p promotes it into {d.batch}."),
    ]
    return decision_frame(
        view,
        name="draft",
        subject=d.id,
        context=f"{d.title} · {d.state.value.lower()} · not work until it is promoted",
        body=body,
        keys=draft_keys(s),
        foot=cursor_foot("FIELD", s.draft_field + 1, len(PROMOTION)),
    )


# ---------- the marker card ----------

MARKER_FACTS: dict[MarkerFact, str] = {
    MarkerFact.DATED: "● dated · the date is committed, not forecast",
    MarkerFact.FORECAST: "○ forecast · a proposal, not a commitment",
    MarkerFact.UNDATED: f"{NO_VALUE} undated · no date is committed or forecast",
}


def render_marker(view: View, m: MarkerRecord) -> list[str]:
    """Return the marker card over milestone record ``m``."""
    met = sum(1 for _text, proven in m.criteria if proven)
    body = [
        dlab("MARKER", MARKER_FACTS[m.fact]),
        dlab("TRACK", m.track),
        dlab("STATE", m.state),
        *(dlab("BATCHES" if i == 0 else "", b) for i, b in enumerate(m.batches)),
    ]
    if m.sealed_at is not None:
        body.append(
            dlab("BUNDLE", f"sealed {short_time(m.sealed_at)} · digest {m.digest} · immutable")
        )
    body += [
        dlab("PROMISED", m.promised),
        dlab("BUILT", f"{m.built_batches} batches · {m.built_tasks} tasks"),
        dlab(
            "VERDICT", f"{met} of {len(m.criteria)} criteria proven · {len(m.criteria) - met} open"
        ),
        *(
            dlab("CRITERIA" if i == 0 else "", f"{text} · {'proven' if ok else 'open'}.")
            for i, (text, ok) in enumerate(m.criteria)
        ),
        thin(view.w),
        dlab("NOT", "A marker never carries its fact by glyph alone — this card is"),
        cont("what the glyph on the lane stands for."),
    ]
    return decision_frame(
        view,
        name="marker",
        subject=m.id,
        context=f"{m.title} · {m.track} · {m.fact.value} · on the Roadmap",
        body=body,
        keys=[_ESC],
    )


# ---------- the artifact card ----------

# Rows the artifact card's chrome takes: header, context, rule, the two provenance rows,
# both borders, the foot and the keybar.
_ARTIFACT_CHROME = 9


@dataclass(frozen=True, slots=True)
class FileWindow:
    """The lines of a file a card shows.

    Attributes:
        start: The first line shown.
        take: How many lines are shown.
        above: How many lines are hidden above, derived from the window.
        below: How many lines are hidden below, derived from the window.
    """

    start: int
    take: int
    above: int
    below: int


def file_window(session: Session, total: int, h: int) -> FileWindow:
    """Return the window over a ``total``-line file at frame height ``h``.

    The stored offset is clamped on every call, so an overshoot never strands the arrows,
    and the edge counts are derived from the window rather than stored.
    """
    room = h - _ARTIFACT_CHROME
    if total <= room:
        session.art_scroll = 0
        return FileWindow(0, total, 0, 0)

    def fit(offset: int) -> FileWindow:
        take = room - (1 if offset else 0)
        if total - offset > take:
            take -= 1
        return FileWindow(offset, take, offset, max(0, total - offset - take))

    furthest = total - fit(total).take
    session.art_scroll = max(0, min(session.art_scroll, furthest))
    return fit(session.art_scroll)


def artifact_keys(win: FileWindow) -> list[Pair]:
    """Return the artifact card's keybar: scroll only while something is hidden."""
    scroll: list[Pair] = [("↑↓", "scroll")] if win.above or win.below else []
    return [*scroll, _COPY, _CLOSE]


def render_artifact(view: View, a: ArtifactRecord) -> list[str]:
    """Return the artifact card over ``a``: its provenance, then the file as it is."""
    lines = ("binary · it opens externally and is never rendered here",) if a.binary else a.lines
    win = file_window(view.session, len(lines), view.h)
    shown: list[str] = []
    if win.above:
        shown.append(f"… {win.above} lines above")
    shown.extend(lines[win.start : win.start + win.take])
    if win.below:
        shown.append(f"… {win.below} lines below")
    written = f"written {short_time(a.written_at)} by step {a.step}"
    as_of = short_time(a.as_of)
    return boxed(
        view,
        crumb=f"Eä ▸ {a.campaign} ▸ {a.file}",
        ctx=f"Campaign {a.campaign} · artifact {a.index} of {a.total} · as of {as_of}",
        pre=[
            f" SOURCE     {a.file} · {a.media} · {a.size} · {written}",
            f" RECORD     Kept with {a.campaign} · sha256 {a.digest}",
        ],
        title=a.file.upper(),
        lines=shown,
        foot="the file is the record · the console renders it, it does not rewrite it",
        keys=artifact_keys(win),
        scrollbar=Scrollbar(total=len(lines), take=win.take, start=win.start),
    )


# ---------- the rung record card ----------


def rung_title(r: RungRecord) -> str:
    """Return the rung card's title: the rung, its name and its outcome, never a stage name."""
    if r.outcome is RungOutcome.FAIL and r.rung == SCREEN_RUNG:
        outcome = "ADVISORY NEGATIVE"
    else:
        outcome = {RungOutcome.FAIL: "FAILED", RungOutcome.NOT_RUN: "NOT RUN"}.get(
            r.outcome, r.outcome.value.upper()
        )
    return f"RUNG {r.rung} {r.name.upper()} · {outcome}"


def render_rung(view: View, c: ClaimRecord, r: RungRecord) -> list[str]:
    """Return the rung record card over rung ``r`` of claim ``c``."""
    pending = r.outcome in (RungOutcome.UNKNOWN, RungOutcome.NOT_RUN)
    # a full digest outgrows the line beside its reference, so it takes the line below
    over = [x for i in r.inputs for x in (i.ref, f"  {i.digest}")] or [f"{NONE} no input recorded"]
    found = [r.finding or "No outcome yet — unknown, not failed.", *r.counts]
    when = (
        "open" if r.outcome is RungOutcome.UNKNOWN else NO_VALUE if pending else short_time(r.as_of)
    )
    record = f"Kept with {c.id}"
    record += f" · written at sequence {r.sequence}" if r.sequence is not None else ""
    record += f" · {r.evidence}" if r.evidence else ""
    lines = [
        f"CHECK      {r.check}",
        "",
        *(f"{'OVER' if i == 0 else '':<11}{x}" for i, x in enumerate(over)),
        "",
        *(f"{'FOUND' if i == 0 else '':<11}{x}" for i, x in enumerate(found)),
        "",
        f"AS OF      {when} · {r.evaluator}",
        f"RECORD     {record}",
        "",
        f"MEANS      {rung_means(r)}",
    ]
    return boxed(
        view,
        crumb=f"Eä ▸ {c.id} ▸ Rung {r.rung}",
        ctx=f"Claim {c.id} · rung {r.rung} of {len(c.rungs)} · as of {short_time(c.as_of)}",
        pre=[],
        title=rung_title(r),
        lines=lines,
        foot="the finding is the record · the scorer keeps its own output",
        keys=[_COPY, _CLOSE],
    )


# ---------- the campaign step card ----------

HISTORY = "HISTORY"
PRODUCED = "PRODUCED"
_EVENTS = Grid([12, 8, 0])
_PRODUCTS = Grid([12, 22, 0])
_STEP_GLYPHS: dict[str, str] = {"done": "✓", "running": "⋯", "blocked": "○", "pending": "○"}


def step_word(st: StepRecord) -> str:
    """Return the step's state word; ``blocked`` is derived from what it waits on."""
    return "blocked" if st.blocked else st.state.value


def step_regions(st: StepRecord) -> tuple[str, ...]:
    """Return the step card's regions that have rows, in Tab order."""
    return tuple(name for name, rows in ((HISTORY, st.events), (PRODUCED, st.produced)) if rows)


def step_keys(st: StepRecord) -> list[Pair]:
    """Return the step card's keybar, which follows the regions that have rows."""
    regions = step_regions(st)
    tab: list[Pair] = [("Tab", "region")] if len(regions) > 1 else []
    line: list[Pair] = [("↑↓", "line")] if regions else []
    return [*tab, *line, _COPY, _ESC]


def _when(st: StepRecord) -> str:
    if st.state is StepState.DONE:
        return f"{short_time(st.started_at)} → {short_time(st.ended_at)}"
    if st.state is StepState.RUNNING:
        return f"started {short_time(st.started_at)}"
    return "never started"


def _num(value: float) -> str:
    return f"{value:g}"


def _spent(st: StepRecord) -> str:
    if st.spent is None:
        return NO_VALUE
    bound = f" of ≤{_num(st.limit)}" if st.limit is not None else ""
    return f"{_num(st.spent)}{bound} {st.unit}".rstrip()


def _state_rows(st: StepRecord, room: int) -> list[str]:
    if st.state is StepState.DONE:
        return prose("OUTCOME", [st.outcome or f"{NONE} no outcome recorded"], room)
    if st.state is StepState.RUNNING:
        done, total = st.done_units, st.total_units
        count = f"~{done} of {total} {st.progress_unit}" if done is not None and total else NO_VALUE
        bound = (
            f" · {_num(st.spent)} of the ≤{_num(st.limit)} {st.unit} bound spent"
            if st.spent is not None and st.limit is not None
            else ""
        )
        return [lab("PROGRESS", f"{count}{bound}")]
    return prose("WAITING", [f"It waits on {' and '.join(st.waits_on) or 'nothing named'}."], room)


def _region(view: View, st: StepRecord, name: str, focused: bool) -> list[str]:
    s, w = view.session, view.w
    rows = st.events if name == HISTORY else st.produced
    if not rows:
        empty = (
            "Nothing to show — the step has not started."
            if name == HISTORY
            else f"None yet · {st.why_none}"
        )
        return [lab("ACTIVITY" if name == HISTORY else PRODUCED, f"{NONE} {empty}")]
    cursor = s.hist_sel if name == HISTORY else s.sel
    cursor = max(0, min(cursor, len(rows) - 1))
    if name == HISTORY:
        s.hist_sel = cursor
        head = _EVENTS.head(["", "AT", "WHAT HAPPENED"])
        label = "ACTIVITY" if st.state is StepState.RUNNING else HISTORY
        cells = [["", at, what] for at, what in st.events]
        table = _EVENTS
    else:
        s.sel = cursor
        head = _PRODUCTS.head(["", "WHAT IT MADE", "KIND"])
        label = PRODUCED
        cells = [
            [
                "",
                item,
                "receipt" if item.startswith("EVD-") else f"artifact · kept with {st.campaign}",
            ]
            for item in st.produced
        ]
        table = _PRODUCTS
    cap = 8 if view.xwide else 5 if view.wide else 2
    start = max(0, min(cursor - cap + 1, len(cells) - cap)) if cursor >= cap else 0
    out = [lab(label, head[13:])]
    if start:
        out.append(g_row(_EVENTS.row(["", "", f"… {start} earlier"], False, w), w))
    for k, row in enumerate(cells[start : start + cap]):
        out.append(g_row(table.row(row, focused and start + k == cursor, w), w))
    later = len(cells) - start - cap
    if later > 0:
        out.append(g_row(_EVENTS.row(["", "", f"… {later} later"], False, w), w))
    return out


def focused_region(session: Session, st: StepRecord) -> str | None:
    """Return the region the step card's arrows walk: the held one while it has rows."""
    regions = step_regions(st)
    if not regions:
        return None
    return session.step_reg if session.step_reg in regions else regions[0]


def render_step(view: View, st: StepRecord) -> list[str]:
    """Return the campaign step card over ``st``."""
    w = view.w
    as_of = short_time(st.as_of)
    word = step_word(st)
    focus = focused_region(view.session, st)
    runner = (
        " · ".join(x for x in (st.runner, st.provider, st.session_fact) if x)
        if st.runner
        else f"{NONE} none · {NO_VALUE}"
    )
    body = [
        lab("STEP", f"{st.ordinal} {st.title} · {_STEP_GLYPHS[word]} {word} · {_when(st)}"),
        lab("WAITS ON", " · ".join(st.waits_on) or "Nothing — it could start at once."),
        lab("RUNNER", runner),
        lab("SPENT", _spent(st)),
        thin(w),
        *_state_rows(st, w - 15),
        thin(w),
        *_region(view, st, HISTORY, focus == HISTORY),
        thin(w),
        *_region(view, st, PRODUCED, focus == PRODUCED),
    ]
    return g_frame(
        view,
        crumb=f"Eä ▸ {st.campaign} ▸ Step {st.ordinal}",
        ctx=f"Campaign {st.campaign} · step {st.ordinal} of {st.total} · as of {as_of}",
        body=body,
        keys=step_keys(st),
    )
