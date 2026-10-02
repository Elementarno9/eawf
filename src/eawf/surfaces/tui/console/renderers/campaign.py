"""campaign: the research campaign as three windowed sections, one walkable at a time.

Each section shows a window of its list and counts what is off either end, so no section
may lie about its length; the focused window follows the cursor and the others show their
head.

The native frame draws one Campaign's three sections from the read model. Under
``REPLAYING`` it renders exactly as of the replayed sequence and says that later facts
exist: the status line names the sequence the replay started at, the head it heads toward
and how many findings were promoted past the cursor, so the frame never shows a fact from
a later revision and never hides that one exists.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from eawf.kernel.projection.campaign import CampaignView
from eawf.kernel.projection.connection import ReplayNote
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.state.epoch2.campaign import ResearchBudget
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.decisions import DecisionRecords
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Breadth,
    Fixed,
    Grid,
    View,
    chip,
    g_frame,
    g_pad,
    g_row,
    strip_chips,
    thin,
)
from eawf.surfaces.tui.console.keybar import KEY, route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.budget_lines import axis_line
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    UNKNOWN_WORD,
    counts,
    finish,
    label,
    more,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

PLAN = "PLAN"
EVIDENCE = "EVIDENCE"
ARTIFACTS = "ARTIFACTS"
FINDINGS = "FINDINGS"
CLAIM = "CLM-0004"
#: The one campaign the prototype plan records.
OWN = "CAM-0001"
_GUTTER = 13
_KEYS = route_pairs("campaign")
_TAB = KEY["tab_section"].pair()


@dataclass(frozen=True, slots=True)
class Win:
    """A window onto a list: its first shown index, how many, and what hides each side."""

    start: int
    take: int
    above: int
    below: int


def section_list(session: Session, fixture: Fixture) -> tuple[Any, ...]:
    """Return the list the focused section walks."""
    reg = fixture.registers
    sec = session.cam_sec or PLAN
    return reg.cam_steps if sec == PLAN else reg.cam_evid if sec == EVIDENCE else reg.cam_art


def caps(breadth: Breadth) -> dict[str, int]:
    """Return each section's row cap; the smallest windowable section is three rows."""
    if breadth is Breadth.XWIDE:
        return {PLAN: 6, EVIDENCE: 5, ARTIFACTS: 5, FINDINGS: 5}
    if breadth is Breadth.WIDE:
        return {PLAN: 6, EVIDENCE: 4, ARTIFACTS: 3, FINDINGS: 3}
    return {PLAN: 3, EVIDENCE: 3, ARTIFACTS: 3, FINDINGS: 3}


def window(total: int, cap: int, sel: int, focused: bool) -> Win:
    """Return the window onto a list of ``total`` rows.

    A single hidden row is shown rather than announced, and the cursor never leaves its
    own window.
    """
    if total <= cap:
        return Win(0, total, 0, 0)
    sel = max(0, min(total - 1, sel))
    max_start = max(0, total - (cap - 1))

    def fit(start: int) -> Win:
        start = 0 if start == 1 else start
        marker = 1 if start > 0 else 0
        take = cap - marker
        if total - start - take > 0:
            take = cap - marker - 1
        return Win(start, take, start, max(0, total - start - take))

    win = fit(max(0, min(sel - (cap - 2) // 2, max_start)) if focused else 0)
    if focused and sel >= win.start + win.take:
        win = fit(max(0, min(sel - (win.take - 1), max_start)))
    return win


def made_cell(step: Sequence[Any]) -> str:
    """Return what a step made as one cell: the count is the truth, the name a sample."""
    made: Sequence[str] = step[4]
    if made:
        return made[0] + (f" +{len(made) - 1}" if len(made) > 1 else "")
    return "– still running" if "running" in step[1] else "– not started"  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class Section:
    """One section's name, grid, head cells and the cells each of its rows shows."""

    name: str
    grid: Grid
    heads: list[str]
    cells: Callable[[Any], list[str]]


def _sections(breadth: Breadth, fixture: Fixture) -> list[tuple[Section, Sequence[Any], str]]:
    reg = fixture.registers
    x, wide = breadth is Breadth.XWIDE, breadth >= Breadth.WIDE
    steps, evid, arts = reg.cam_steps, reg.cam_evid, reg.cam_art
    done = sum(1 for st in steps if "done" in st[1])
    if x:
        plan = Section(
            PLAN,
            Grid([12, 24, 13, 13, 18, 0]),
            ["STEP", "STATE", "DEPENDS ON", "PRODUCED", "SPENT"],
            lambda st: [st[0], chip(st[2], st[1]), st[3], made_cell(st), st[5]],
        )
        proof = Section(
            EVIDENCE,
            Grid([12, 11, 26, 20, 13, 0]),
            ["RECEIPT", "WHAT IT SHOWS", "CLAIM", "SUPPORT", "DETAIL"],
            lambda e: list(e[:5]),
        )
    elif wide:
        plan = Section(
            PLAN,
            Grid([12, 24, 13, 13, 0]),
            ["STEP", "STATE", "DEPENDS ON", "PRODUCED"],
            lambda st: [st[0], chip(st[2], st[1]), st[3], made_cell(st)],
        )
        proof = Section(
            EVIDENCE,
            Grid([12, 11, 26, 20, 0]),
            ["RECEIPT", "WHAT IT SHOWS", "CLAIM", "SUPPORT"],
            lambda e: list(e[:4]),
        )
    else:
        plan = Section(
            PLAN,
            Grid([12, 20, 12, 0]),
            ["STEP", "STATE", "DEPENDS ON"],
            lambda st: [st[0], chip(st[2], st[1]), st[3]],
        )
        proof = Section(
            EVIDENCE,
            Grid([12, 11, 26, 0]),
            ["RECEIPT", "WHAT IT SHOWS", "CLAIM"],
            lambda e: list(e[:3]),
        )
    files = Section(
        ARTIFACTS, Grid([12, 22, 0]), ["ARTIFACT", "WRITTEN"], lambda a: [a["f"], a["at"]]
    )
    return [
        (plan, steps, f"{done} of {len(steps)} steps done · 1 running · 1 blocked by step 5"),
        (proof, evid, f"{len(evid)} receipts · 1 contradiction open · 2 claims still uncertified"),
        (files, arts, f"{len(arts)} files · 1 finding promoted to MLS-0007 · 2 held"),
    ]


def _receded(text: str, w: int) -> Fixed:
    return Fixed(pad(strip_chips(text), w))


def _section_rows(
    view: View, section: Section, rows: Sequence[Any], summary: str, *, graph: bool = True
) -> list[str]:
    """Return one section: its summary row, head, window and edge markers.

    Args:
        view: The render being built.
        section: The section's name, grid, heads and cells.
        rows: The section's list.
        summary: The line beside the section's name.
        graph: Whether the plan draws the prototype's step graph; a read Campaign
            states no graph layout, so its plan draws none.
    """
    s, w = view.session, view.w
    on = (s.cam_sec or PLAN) == section.name
    out: list[str] = [Fixed(g_pad(g_pad(" " + section.name, _GUTTER) + summary, w))]
    if graph and section.name == PLAN and view.xwide:
        span = "─ ✓ 3 ───"
        out.append(g_pad(" GRAPH", _GUTTER) + "✓ 1 ─┐" + " " * len(span) + "┌─ ✓ 4 ─┐")
        out.append(g_pad("", _GUTTER) + "✓ 2 ─┴" + span + "┴" + "───────" + "┴─ ⋯ 5 ─── ○ 6")
    elif graph and section.name == PLAN and view.wide:
        out.append(g_pad(" GRAPH", _GUTTER) + "✓ 1 · ✓ 2 → ✓ 3 → ✓ 4 → ⋯ 5 → ○ 6")
    out.append(section.grid.head(["", *section.heads]))
    win = window(len(rows), caps(view.breadth)[section.name], s.sel, on)
    if win.above:
        out.append(_receded(section.grid.row(["", f"… {win.above} above"], False, w), w))
    for k, item in enumerate(rows[win.start : win.start + win.take]):
        row = section.grid.row(["", *section.cells(item)], False, w)
        if on and win.start + k == s.sel:
            # the cursor sits one column before the first cell, not twelve clear in the gutter
            row = row[:13] + "▸" + row[14:]
        out.append(g_row(row, w) if on else _receded(row, w))
    if win.below:
        out.append(_receded(section.grid.row(["", f"… {win.below} below"], False, w), w))
    return out


def _absent(view: View, campaign: str) -> list[str]:
    """Return the frame of a campaign this route holds no plan for, said rather than swapped."""
    s, fx = view.session, view.fixture
    head = [f"Eä ▸ {fx.scope} ▸ {campaign}", f"Campaign {campaign}", ""]
    # the header, context and rule rows are g_frame's own, so only the body is kept
    _, _, _, *body = dv.absent(
        s, fx, head, entity_id=campaign, what="plan, evidence or artifacts", w=view.w
    )
    return g_frame(view, crumb=head[0], ctx=head[1], body=body, keys=_KEYS)


#: The connection value under which the frame is drawn as of the replayed sequence.
REPLAYING = "REPLAYING"

#: Each section and what it says while the Campaign has not been read. With no row to
#: stand over, a section draws no column heads: its label line already states the absence.
_NATIVE_SECTIONS: tuple[tuple[str, str], ...] = (
    (PLAN, "the campaign plan has not been read yet"),
    (EVIDENCE, "no receipt is recorded against it yet"),
    (ARTIFACTS, "no artifact is recorded against it yet"),
)
#: The sections a read Campaign walks with Tab, in order; Enter opens a plan step or an
#: artifact, and a finding is read where it is listed.
_WALKED: tuple[str, ...] = (PLAN, ARTIFACTS, FINDINGS)
_STEP_GLYPHS: dict[str, str] = {"done": "✓", "running": "⋯", "blocked": "○", "pending": "○"}


def replay_line(note: ReplayNote | None) -> str:
    """Return the replaying status line: where the replay stands and what lies past it.

    Args:
        note: The replay the link is carrying out; ``None`` when the link replays without
            having stated the head, which the line says rather than inventing one.

    Returns:
        ``replaying N → M · K findings promoted after this point``, with ``K`` the unknown
        token while no producer counts promotions.
    """
    if note is None:
        return f"replaying · the head it replays toward is {UNKNOWN_WORD}"
    promoted = note.findings_promoted_after_cursor
    count = UNKNOWN_WORD if promoted is None else group(promoted)
    return (
        f"replaying {group(note.replaying_from_sequence)} → {group(note.head_sequence)}"
        f" · {count} findings promoted after this point"
    )


def _campaign(session: Session, spine: SpineView) -> SpineRow | None:
    """Return the Campaign the frame is about: the session's subject, else the first held."""
    rows = [row for row in spine.rows if row.collection is Epoch2Collection.CAMPAIGN]
    subject = session.subj_id
    return next((row for row in rows if row.key == subject), rows[0] if rows else None)


def read_campaign(
    session: Session, spine: SpineView, decisions: DecisionRecords | None
) -> CampaignView | None:
    """Return the read Campaign the frame draws its sections from; ``None`` before its read."""
    row = _campaign(session, spine)
    return decisions.campaign(row.key) if row is not None and decisions is not None else None


def shown_findings(campaign: CampaignView, replay: ReplayNote | None) -> tuple[Any, ...]:
    """Return the findings the frame may draw promoted: under a replay, only those at or
    before its cursor, since a later promoting event is a fact the replay has not reached.
    """
    if replay is None:
        return campaign.findings
    later = {f.key for f in campaign.promoted_after(replay.replaying_from_sequence)}
    return tuple(f for f in campaign.findings if f.key not in later)


def section_rows(campaign: CampaignView, replay: ReplayNote | None) -> dict[str, list[list[str]]]:
    """Return each walked section's rows as the cells the frame draws, in plan order."""
    steps = [
        [
            f"{at.step.ordinal} {at.step.title}",
            f"{_STEP_GLYPHS[at.state]} {at.state}",
            " · ".join(at.waits_on) or "–",  # noqa: RUF001
            str(len(at.step.produced)) if at.step.produced else "–",  # noqa: RUF001
        ]
        for at in campaign.steps
    ]
    files = [
        [
            card.file_name,
            f"{card.written_at:%H:%M}"
            + (f" by step {card.written_by.step_ordinal}" if card.written_by.step_ordinal else ""),
        ]
        for card in campaign.artifacts
    ]
    promoted = [
        [
            f.key,
            f.statement,
            f.disposition.value
            + (
                f" · promoted at {group(campaign.promoted_at[f.key])}"
                if f.key in campaign.promoted_at
                else ""
            ),
        ]
        for f in shown_findings(campaign, replay)
    ]
    return {PLAN: steps, ARTIFACTS: files, FINDINGS: promoted}


def walked_section(session: Session, rows: dict[str, list[list[str]]]) -> str:
    """Return the section the cursor walks: the held one while it has rows, else the first."""
    held = session.cam_sec if rows.get(session.cam_sec) else None
    return held or next((name for name in _WALKED if rows[name]), PLAN)


#: Each walked section's grid and heads, wide then narrow; a narrow plan drops PRODUCED.
_NATIVE_GRIDS: dict[str, tuple[tuple[Grid, list[str]], tuple[Grid, list[str]]]] = {
    PLAN: (
        (Grid([12, 44, 14, 24, 0]), ["STEP", "STATE", "WAITS ON", "PRODUCED"]),
        (Grid([12, 34, 14, 0]), ["STEP", "STATE", "WAITS ON"]),
    ),
    ARTIFACTS: ((Grid([12, 34, 0]), ["ARTIFACT", "WRITTEN"]),) * 2,
    FINDINGS: (
        (Grid([12, 10, 60, 0]), ["FINDING", "WHAT IT LEARNED", "STANDING"]),
        (Grid([12, 10, 34, 0]), ["FINDING", "WHAT IT LEARNED", "STANDING"]),
    ),
}


def _native_sections(view: View, campaign: CampaignView) -> list[str]:
    """Return the plan, artifact and finding sections of a read Campaign."""
    s = view.session
    rows = section_rows(campaign, view.replay if s.conn == REPLAYING else None)
    on = walked_section(s, rows)
    s.cam_sec = on
    dv.sel_in(s, len(rows[on]))
    held = len(campaign.findings)
    summaries = {
        PLAN: campaign.plan_line,
        ARTIFACTS: f"{len(rows[ARTIFACTS])} files kept with {campaign.key}",
        FINDINGS: f"{len(rows[FINDINGS])} promoted"
        + (
            f" · {held - len(rows[FINDINGS])} past the replay cursor"
            if held > len(rows[FINDINGS])
            else ""
        ),
    }
    body: list[str] = []
    for name in _WALKED:
        body.append(thin(view.w))
        if not rows[name]:
            body.append(label(name, f"∅ none yet · {summaries[name]}"))
            continue
        grid, heads = _NATIVE_GRIDS[name][0 if view.wide else 1]
        shown = [row[: len(heads)] for row in rows[name]]
        section = Section(name, grid, heads, list)
        body += _section_rows(view, section, shown, summaries[name], graph=False)
    return body


def bounds_rows(budget: ResearchBudget) -> list[str]:
    """Return the BOUNDS rows: one axis of the Campaign's budget per row, label on the first."""
    first, *rest = (axis_line(axis) for axis in budget.axes)
    return [label("BOUNDS", first), *(more(line) for line in rest)]


def native_frame(view: View, spine: SpineView) -> list[str]:
    """Return the Campaign frame drawn from the read model the daemon served.

    Args:
        view: The render being built; its replay note heads the frame while replaying.
        spine: The campaign register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    campaign = _campaign(s, spine)
    read = read_campaign(s, spine, view.decisions)
    key = campaign.key if campaign is not None else "no campaign"
    status = value_cell(campaign.field("status")).slot if campaign is not None else UNAVAILABLE
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(view, spine, "Research", *([key] if campaign else [])),
        summary=(f"Campaign {key} · {status}" if campaign else "No Campaign held")
        + f" · {counts(spine)}",
    )
    body: list[str] = []
    if s.conn == REPLAYING:
        body += [label("REPLAYING", replay_line(view.replay)), thin(w)]
    question = (campaign.title if campaign else None) or f"{UNAVAILABLE} · no question is stated"
    body.append(label("QUESTION", question))
    if read is not None:
        body += bounds_rows(read.evidence_budget)
        body += _native_sections(view, read)
        walked = sum(1 for rows in section_rows(read, None).values() if rows)
        keys = [p for p in _KEYS if walked > 1 or p != _TAB]
        return finish(view, top, body, keys)
    dv.sel_in(s, 0)
    body.append(label("BOUNDS", f"{UNKNOWN_WORD} · the campaign has not been read yet"))
    for name, absent in _NATIVE_SECTIONS:
        body += [thin(w), label(name, f"{UNKNOWN_WORD} · {absent}")]
    return finish(view, top, body, [p for p in _KEYS if p != _TAB])


def render(view: View) -> list[str]:
    """Return the Campaign frame, native when a read model is held.

    A drill onto a campaign other than the one the prototype plan records states that
    campaign's absence rather than drawing the recorded plan under its id.
    """
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    if s.subj_id and s.subj_id != OWN:
        return _absent(view, s.subj_id)
    x, wide = view.xwide, view.wide
    dv.sel_in(s, len(section_list(s, fx)))
    body = [
        " QUESTION    Does provider drift change replay digests?",
        " BOUNDS      ≤20 runs · ≤8h · 2 providers · stop at first contradiction",
    ]
    if x:
        body.append(" STOP RULE   Armed — one contradiction is open, so step 6 may not start.")
    for section, rows, summary in _sections(view.breadth, fx):
        body.append(thin(w))
        body += _section_rows(view, section, rows, summary)
        if section.name == EVIDENCE and wide:
            body.append(
                g_pad(" CONFLICT", _GUTTER) + "EVD-0011 and EVD-0014 disagree about the same task"
            )
        if section.name == EVIDENCE and x:
            body.append(
                g_pad("", _GUTTER)
                + "the bound says stop · step 6 stays blocked until one is retracted"
            )
    if x:
        body.append(thin(w))
        body.append(
            " NEXT        Step 5 finishes, then the conflict is settled before the write-up."
        )
        body.append(" RECORD      Every artifact is kept with CAM-0001 · digests in each card")
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Research ▸ CAM-0001",
        ctx="Campaign CAM-0001 Provider drift · REVIEW · W30 · ~6.2 of 8h · 14 of 20 runs",
        body=body,
        keys=_KEYS,
    )


def _native_key(ctx: Ctx, key: str, shift: bool) -> bool:
    """Walk a read Campaign's sections on Tab and open its step or artifact on Enter."""
    s = ctx.s
    spine = ctx.projection
    read = read_campaign(s, spine, ctx.decisions) if isinstance(spine, SpineView) else None
    if read is None:
        held_any = bool(spine.rows) if spine is not None else False
        why = "the campaign has not been read yet" if held_any else "no campaign is held"
        ctx.log(key, f"nothing to open · {why}")
        return True
    rows = section_rows(read, None)
    on = walked_section(s, rows)
    if key == "Tab":
        walked = [name for name in _WALKED if rows[name]]
        at = walked.index(on)
        s.cam_sec = walked[(at + (len(walked) - 1 if shift else 1)) % len(walked)]
        s.sel = 0
        ctx.log("Tab", f"section → {s.cam_sec}")
        return True
    i = min(s.sel, len(rows[on]) - 1)
    if on == PLAN:
        s.cam_step = i
        go(ctx, "campaign.step", f"step {read.steps[i].step.ordinal}", read.key)
        return True
    if on == ARTIFACTS:
        card = read.artifacts[i]
        s.art_scroll = 0
        go(ctx, "campaign.artifact", f"artifact {card.file_name}", card.artifact_ref)
        return True
    ctx.log("Enter", f"{rows[on][i][0]} is read where it is listed · nothing to open")
    return True


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Cycle the sections on Tab and open what the focused section holds on Enter."""
    s = ctx.s
    reg = ctx.fixture.registers
    if s.route != "campaign" or busy(s):
        return False
    if ctx.projection is not None:
        return key in ("Tab", "Enter") and _native_key(ctx, key, shift)
    if key == "Tab":
        sections = list(reg.cam_sects)
        at = sections.index(s.cam_sec or PLAN)
        s.cam_sec = sections[(at + (len(sections) - 1 if shift else 1)) % len(sections)]
        s.sel = 0
        ctx.log("Tab", f"section → {s.cam_sec}")
        return True
    if key != "Enter":
        return False
    sec = s.cam_sec or PLAN
    i = s.sel
    if sec == ARTIFACTS:
        s.artifact = i
        s.art_scroll = 0
        go(ctx, "campaign.artifact", f"artifact {reg.cam_art[i]['f']}")
        return True
    if sec == EVIDENCE:
        receipt = reg.cam_evid[i]
        go(ctx, "evidence", f"receipt {receipt[0]} · the claim it supports", receipt[5])
        return True
    # a step is a place: Enter opens the step, not one of the things it made; the row is
    # zeroed after the jump so the back stack keeps the row Esc returns to
    s.cam_step = i
    go(ctx, "campaign.step", f"step {reg.cam_steps[i][0]}")
    s.sel = 0
    return True
