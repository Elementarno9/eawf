"""The Enter table: what Enter opens on each route, and the confirmed verb it can send.

Enter is the depth key, so each route answers it with its own drill or card; an overlay
that owns Enter (the draft card, the consequence card) is asked first. A confirmed verb
leaves only through :func:`send_verb`, past the one write gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.kernel.projection.compute import STALL_KIND, ProjectionRow
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import attention as att
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import drill
from eawf.surfaces.tui.console.attach import ONBOARDING
from eawf.surfaces.tui.console.drill import HOME
from eawf.surfaces.tui.console.keymap import ENTRY_ROUTE
from eawf.surfaces.tui.console.mutation import open_questions
from eawf.surfaces.tui.console.navigation import (
    Ctx,
    copied,
    go,
    leave_overlay,
    open_overlay,
    remember,
)
from eawf.surfaces.tui.console.notices import notice_of, short_key
from eawf.surfaces.tui.console.onboarding import ONBOARDING_KIND, WORKSPACE_VERB
from eawf.surfaces.tui.console.operations import (
    ANSWER_OPTIONS,
    ATTENTION_ROUTE,
    DISMISS_REPLY,
    DISMISS_VERB,
    PERMISSION_VERBS,
    RUN_CONTROLS,
    RUN_KINDS,
    AnswerRequest,
    ControlRequest,
    PermissionDecision,
    QuestionAnswer,
    VerbRequest,
)
from eawf.surfaces.tui.console.overlays.bound_keys import open_held_row
from eawf.surfaces.tui.console.overlays.evidence import claim_id
from eawf.surfaces.tui.console.overlays.resolution import Ending
from eawf.surfaces.tui.console.reads import write_refusal
from eawf.surfaces.tui.console.registry import route_for_id
from eawf.surfaces.tui.console.renderers import cost_ceiling, history, track
from eawf.surfaces.tui.console.renderers import timeline as tl
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import Severity

# Why a confirmed verb went nowhere: the console was started without a daemon link.
NO_LINK = "the console holds no daemon link · nothing was written"
# The draft card's fields, in the order its arrows walk them.
DRAFT_FIELDS: tuple[str, ...] = ("criteria", "owner", "batch")


def _nav_at_cursor(s: Session) -> str | None:
    nav = s.record_nav or []
    return nav[s.sel] if s.sel < len(nav) else None


def send_verb(ctx: Ctx, k: str, request: VerbRequest, verb: str) -> None:
    """Hand a confirmed verb to the daemon link; nothing the console holds changes here.

    The daemon's answer, and the patch its commit pushes, are the only things that move
    the frame afterwards, so a verb the daemon never heard of cannot look done.
    """
    s = ctx.s
    refusal = write_refusal(s, ctx.fixture, verb=verb, principal_refusal=ctx.principal_refusal)
    if refusal:
        ctx.log(k, f"{verb} {request.target} refused — {refusal}")
        return
    if ctx.dispatch_write(request):
        ctx.log(k, f"{verb} {request.target} sent to the daemon · waiting for its answer")
        return
    ctx.notify(NO_LINK, "not sent", Severity.WARN)
    ctx.log(k, f"{verb} {request.target} not sent — {NO_LINK}")


def confirm(ctx: Ctx) -> None:
    """Confirm the consequence card: its verb is sent to the daemon, or refused with why.

    An answer is addressed only to the selected row of the Attention projection the
    link holds. The prototype registers are never read here: a console holding them and
    no projection has nothing it could answer, so it sends nothing.
    """
    s = ctx.s
    leave_overlay(s)
    target = s.c_target
    if target:
        s.c_target = None
        _confirm_target(ctx, target)
        return
    held = ctx.attention
    row = _selected_row(s, held.rows) if held is not None else None
    if row is None:
        ctx.log("Enter", "no attention row is held here — nothing was sent")
        return
    action_id = row.key
    # the register lists a Run only as a ceiling breach, a notice nothing answers, or as a
    # stall, which is resumed or let go from the pause over its Run
    if row.collection is Epoch2Collection.RUN:
        if row.facts.get("kind") == STALL_KIND:
            ctx.log("Enter", f"{action_id} — {att.STALL_REFUSAL} · nothing was sent")
        else:
            ctx.log("Enter", f"{action_id} is a notice — nothing answers it, nothing was sent")
        return
    if row.collection is Epoch2Collection.OPEN_QUESTION:
        ctx.log("Enter", f"{action_id} is a question — answer it from its detail, nothing was sent")
        return
    verb = att.VERB[s.verb or "a"]
    refusal = write_refusal(s, ctx.fixture, verb=verb.name, kind=att.ATTENTION_ROUTE)
    # a provider permission is decided by its own verb, never sealed as an answer
    permission = row.collection is Epoch2Collection.PERMISSION
    option = (PERMISSION_VERBS if permission else ANSWER_OPTIONS).get(verb.name)
    if refusal or option is None:
        ctx.log("Enter", f"{verb.name} {action_id} refused — {refusal}")
        return
    request: VerbRequest = (
        PermissionDecision(target=action_id, verb=option)
        if permission
        else AnswerRequest(target=action_id, option_id=option)
    )
    send_verb(ctx, "Enter", request, verb.name)


def _selected_row(s: Session, rows: tuple[ProjectionRow, ...]) -> ProjectionRow | None:
    """Return the held row the cursor selects, by stable id only.

    A selection whose id the rows no longer hold, or a frame whose caret names no row,
    selects nothing, rather than sliding onto whichever row sits at the cursor's offset.
    """
    if s.sel_id is None:
        return None
    return next((row for row in rows if row.key == s.sel_id), None)


def _confirm_target(ctx: Ctx, target: Mapping[str, str]) -> None:
    """Send a Run control the card previewed, or say why its verb reaches no daemon.

    A first-run step's card is confirmed into its own daemon verbs, which write the
    machine registry rather than a projection row, so no connection gate applies.
    """
    verb, kind, target_id = target["verb"], target["kind"], target["id"]
    if kind == ONBOARDING_KIND:
        started = ctx.onboard is not None and ctx.onboard()
        note = "sent to the daemon · waiting for its answer" if started else NO_LINK
        ctx.log("Enter", f"{verb} {target_id} {note}")
        return
    if kind == ATTENTION_ROUTE and verb == DISMISS_VERB:
        _dismiss_questions(ctx, target_id)
        return
    refusal = write_refusal(ctx.s, ctx.fixture, verb=verb, kind=kind)
    control = RUN_CONTROLS.get(verb) if kind in RUN_KINDS else None
    if refusal or control is None:
        ctx.log("Enter", f"{verb} on {target_id} — nothing was written · {refusal}")
        return
    send_verb(ctx, "Enter", ControlRequest(target=target_id, control=control), verb)


def _dismiss_questions(ctx: Ctx, target_id: str) -> None:
    """Answer every marked question, else the one under the cursor, as no longer needed.

    Each is one write through the question answer verb, so each settles, or is refused,
    on its own; the marks are cleared once they are sent.
    """
    s = ctx.s
    listed = open_questions(ctx)
    targets = [key for key in s.marked if key in listed] or [
        key for key in (target_id,) if key in listed
    ]
    if not targets:
        ctx.log("Enter", "nothing to dismiss · mark the questions with Space or * first")
        return
    for key in targets:
        send_verb(ctx, "Enter", QuestionAnswer(target=key, reply=DISMISS_REPLY), "dismiss")
    s.marked = []


def _enter_overlay(ctx: Ctx) -> bool:
    """Act on Enter inside an overlay that owns it."""
    s = ctx.s
    if s.overlay == "draft":
        field = DRAFT_FIELDS[s.draft_field]
        if not refused(ctx, "Enter", f"set {field}"):
            ctx.log("Enter", f"set {field} · the field the cursor is on")
        return True
    if s.overlay == "consequence":
        confirm(ctx)
        return True
    return False


def _enter_track(ctx: Ctx) -> None:
    s = ctx.s
    drills = track.group_of(s.track_group).drills
    entity_id, dest = drills[min(s.sel, len(drills) - 1)]
    s.back.record(remember(s))
    s.region = None
    s.route = dest
    s.sel = 0
    s.subj_id = entity_id
    s.sel_id = None
    ctx.log("Enter", f"drill → {dest} · {entity_id}")


def _enter_release(ctx: Ctx) -> None:
    s = ctx.s
    if s.rel_reg == "READINESS":
        open_overlay(s, "readiness", subject=s.subj_id)
        ctx.log("Enter", "readiness matrix · on the signal you were reading")
        return
    target = _nav_at_cursor(s)
    drill.drill_to(ctx, route_for_id(target) or "milestone", target)


def _enter_entry(ctx: Ctx) -> None:
    s = ctx.s
    state = ctx.fixture.proto.entry[s.entry_sel]
    if state.id == ONBOARDING and s.path_sel == 0 and ctx.first_run is not None:
        # the workspace step runs here, through the daemon, once its card is confirmed
        s.c_target = ctx.first_run.card()
        open_overlay(s, "consequence", subject=ctx.first_run.code)
        ctx.log("Enter", f"{WORKSPACE_VERB} {ctx.first_run.code} → consequence preview")
        return
    if state.commands:
        # One command per path or row, else the primary command first.
        at = s.path_sel if (state.paths or state.rows) else 0
        command = state.commands[min(at, len(state.commands) - 1)]
        if command:
            ctx.log("Enter", f"{copied(ctx.copy(command))}: {command} · shown, never run here")
        else:
            ctx.log("Enter", "no declared command runs this step yet")
        return
    first_cell = (
        (state.rows or (("",),))[s.path_sel][0] if state.id in ("ambiguous", "offline") else ""
    )
    notes = {
        "ambiguous": f"attach to {first_cell} · remembered for this session only",
        "failed": "copied: eawf repo register . --yes",
        "schema": "copied: eawf migrate epoch2 --export",
        "offline": f"drill → {first_cell} · read-only, controls are gone",
    }
    if state.paths:
        command = state.paths[s.path_sel][0].replace(" ", "-")
        ctx.log("Enter", f"eawf migrate --{command} · shown, never auto-applied")
    else:
        ctx.log("Enter", notes.get(state.id, "nothing to run while attaching"))


def _enter_timeline(ctx: Ctx) -> None:
    s = ctx.s
    region = s.tl_reg or tl.LANES
    if region == tl.LANES:
        lane = tl.lane_name(s.sel)
        if s.timeline_marker is None:
            ctx.log("Enter", f"no marker on the {lane} lane · nothing to open")
            return
        open_overlay(s, "marker", subject=s.timeline_marker)
        s.marker_card = {
            "id": s.timeline_marker,
            "lane": lane,
            "glyph": tl.marker_glyph(lane, s.mark),
        }
        ctx.log("Enter", f"marker detail · {s.marker_card['id']} on the {lane} lane")
        return
    rows = (s.tl_regs or {}).get(region, [])
    pick = rows[s.tl_sel] if s.tl_sel < len(rows) else None
    if not pick:
        ctx.log("Enter", f"nothing to open in {region}")
        return
    s.back.record(remember(s))
    s.region = None
    s.route = "milestone" if region == tl.UNDATED else "release"
    s.subj_id = pick[0]
    s.sel = 0
    if region == tl.UNDATED:
        ctx.log("Enter", f"→ {pick[0]} · {pick[1]}")
    else:
        ctx.log("Enter", f"→ {pick[0]} {pick[1]} · {str(pick[3]).lower()}")


def _enter_milestone(ctx: Ctx) -> None:
    s = ctx.s
    target = _nav_at_cursor(s)
    if not target:
        open_overlay(s, "acceptance", subject=s.subj_id)
        s.sel = 0
        ctx.log("Enter", "acceptance evidence at this digest")
        return
    s.back.record(remember(s))
    s.region = None
    s.route = route_for_id(target) or "batch.detail"
    s.sel = 0
    s.subj_id = target
    s.sel_id = None
    ctx.log("Enter", f"drill → {s.route} · {target}")


def _enter_attention(ctx: Ctx) -> None:
    s = ctx.s
    notice = notice_of(ctx.notices, s.sel_id)
    if notice is not None:
        # a notice has nothing to confirm: its detail is the notifications record form
        go(ctx, "notifications", f"notice detail · {short_key(notice)}", notice.notice_key)
        return
    pause = ctx.decisions.pause(s.sel_id) if ctx.decisions is not None and s.sel_id else None
    if pause is not None and pause.id == s.sel_id:
        # a pause is answered by nobody here: its detail says what would end it
        open_overlay(s, "pause", subject=pause.id)
        ctx.log("Enter", f"pause detail · {pause.id}")
        return
    if att.held_refusal(ctx, "Enter") or open_held_row(ctx):
        return
    rows = att.attn_list(s, ctx.fixture)
    row = rows[s.sel] if s.sel < len(rows) else (rows[0] if rows else None)
    row_id = row.id if row else ""
    if row is not None and "question" in row.kind:
        open_overlay(s, "question", subject=row_id)
        ctx.log("Enter", f"question detail · {row_id}")
    elif row is not None and row.bucket == "lost":
        open_overlay(s, "pause", subject=row_id)
        ctx.log("Enter", "pause detail · the outcome is unknown")
    elif att.is_notice(row):
        # a notice has nothing to confirm, so its detail is the notifications record form
        go(ctx, "notifications", f"notice detail · {row_id}")
    else:
        open_overlay(s, "consequence", subject=row_id or None)
        ctx.log("Enter", f"action detail · {row_id}")


def _enter_overlay_route(overlay: str, note: str) -> Callable[[Ctx], None]:
    def enter(ctx: Ctx) -> None:
        open_overlay(ctx.s, overlay)
        ctx.log("Enter", note)

    return enter


def _enter_campaign(ctx: Ctx) -> None:
    """Open the evidence viewer on the claim under the cursor, its receipts from the top."""
    s = ctx.s
    open_overlay(s, "evidence", subject=claim_id(s.sel))
    s.sel = 0
    ctx.log("Enter", "evidence viewer · the receipts behind this claim")


def _enter_history(ctx: Ctx) -> None:
    """Open the resolution card on the fact under the cursor, with the ending it records."""
    s = ctx.s
    if ctx.unheld:
        ctx.noop("Enter")
        return
    facts = history.filtered_facts(dv.filter_of(s))
    fact = facts[s.sel] if 0 <= s.sel < len(facts) else None
    target = fact[0].split(" ")[0] if fact is not None else None
    open_overlay(s, "resolution", subject=target)
    if fact is not None and fact[2] == "retention":
        s.resolution_ending = Ending.PURGED.value
    ctx.log("Enter", "resolution card — what happened to this target")


def _enter_note(note: str) -> Callable[[Ctx], None]:
    def enter(ctx: Ctx) -> None:
        ctx.log("Enter", note)

    return enter


def _enter_nav(default: str) -> Callable[[Ctx], None]:
    def enter(ctx: Ctx) -> None:
        target = _nav_at_cursor(ctx.s)
        drill.drill_to(ctx, route_for_id(target) or default, target)

    return enter


def _enter_activity(ctx: Ctx) -> None:
    row = dv.current_fleet_row(ctx.s, ctx.fixture)
    drill.drill_to(ctx, "run.detail", row.run if row else None)


def _enter_cost_ceiling(ctx: Ctx) -> None:
    """Open the Run the cursor sits on in the stopped list, which the footer promises."""
    drill.drill_to(ctx, "run.detail", cost_ceiling.stopped_run(ctx.s))


_ENTER: Mapping[str, Callable[[Ctx], None]] = MappingProxyType(
    {
        HOME: _enter_note("nothing to drill"),
        "track": _enter_track,
        "task.detail": _enter_nav("run.detail"),
        "release": _enter_release,
        ENTRY_ROUTE: _enter_entry,
        "campaign": _enter_campaign,
        "backlog": _enter_overlay_route("draft", "draft detail · what it still needs"),
        "timeline": _enter_timeline,
        "history": _enter_history,
        "settings": _enter_note("the WHY pane below already shows this chain"),
        "milestone": _enter_milestone,
        "activity": _enter_activity,
        "batch.detail": _enter_nav("task.detail"),
        "attention": _enter_attention,
        "cost.ceiling": _enter_cost_ceiling,
    }
)


def enter(ctx: Ctx, k: str, pane: bool) -> None:
    """Act on Enter: an overlay that owns it first, else the route's own Enter."""
    if pane:
        ctx.noop("Enter")
        return
    if _enter_overlay(ctx):
        return
    handler = _ENTER.get(ctx.s.route)
    if handler is not None:
        handler(ctx)


def refused(ctx: Ctx, k: str, verb: str) -> bool:
    """Return whether the connection gate refuses ``verb``, logging the gate's own reason.

    An overlay's verb passes the same gate as the route's, with the same words, so an
    overlay is never a way around a refusal.
    """
    refusal = write_refusal(ctx.s, ctx.fixture, verb=verb)
    if refusal:
        ctx.log(k, f"{verb} is unavailable — {refusal}")
    return bool(refusal)


__all__ = ["DRAFT_FIELDS", "NO_LINK", "confirm", "enter", "refused", "send_verb"]
