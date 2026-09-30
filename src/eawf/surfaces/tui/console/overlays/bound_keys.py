"""The keys of an overlay or card drawn from its record: exactly the keys its keybar names.

A bound overlay owns the keyboard, so every key reaches it and a key its keybar does not
name does nothing -- the keybar and the key handler read the same record, so they cannot
disagree. The rack clear is the console's and passes through, and a reply being typed
keeps its own field. A card on a sub-surface claims only the keys it draws for; ``Esc``
and the console's own keys pass through to go back and to act as they do everywhere.

A verb no daemon mutator carries is refused here with the reason, and a verb that has a
consequence is previewed on the consequence card first; nothing here writes a record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from eawf.kernel.state.epoch2.consequence import Refusal
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.attention import selected_open_row
from eawf.surfaces.tui.console.decisions import (
    ArtifactRecord,
    ClaimRecord,
    DecisionRecords,
    DraftRecord,
    PauseRecord,
    QuestionRecord,
    StepRecord,
)
from eawf.surfaces.tui.console.keymap import DISMISS, allowlist
from eawf.surfaces.tui.console.mutation import Card, Item, Kind
from eawf.surfaces.tui.console.navigation import Ctx, close_overlay, copied, open_overlay
from eawf.surfaces.tui.console.operations import QuestionAnswer, binding_refusal
from eawf.surfaces.tui.console.overlays.bound import (
    CARD_ROUTES,
    Bound,
    CardRecord,
    Rung,
    bound_card,
    bound_overlay,
    card_absent,
)
from eawf.surfaces.tui.console.overlays.cards import (
    HISTORY,
    PRODUCED,
    PROMOTION,
    file_window,
    focused_region,
    promote_refusal,
    step_regions,
)
from eawf.surfaces.tui.console.overlays.decision import (
    CLAIM_ROWS,
    acceptance_copy,
    claim_copy,
    evidence_rows,
)
from eawf.surfaces.tui.console.overlays.situations import (
    answerable,
    reply_legal,
    unknown_outcome,
)
from eawf.surfaces.tui.console.reads import can_mutate, write_refusal
from eawf.surfaces.tui.console.tokens import Severity
from eawf.workflow.projection.acceptance import AcceptanceBundleView, ReleaseReadinessView

#: The key prefix of a question the daemon's answer verb records an answer to; an
#: operator decision is a pending action, answered through its own seal.
QUESTION_PREFIX: Final = "QST-"
_UP: Final = frozenset({"ArrowUp", "k"})
_DOWN: Final = frozenset({"ArrowDown", "j"})
#: The keys a card on a sub-surface claims; every other key acts as it does on any route.
_CARD_KEYS: Final = frozenset({*_UP, *_DOWN, "Tab", "Enter", "y"})


def bound_key(ctx: Ctx, k: str) -> bool:
    """Act on ``k`` for the bound overlay or card it reaches, returning whether it was claimed.

    Args:
        ctx: The keystroke's context, whose read model and records the frame was drawn
            from.
        k: The key, named the way the dispatcher matches it.
    """
    s = ctx.s
    if k == DISMISS or s.reply is not None or s.typing:
        return False
    if k not in allowlist(s, ctx.fixture) and _claims(ctx):
        ctx.noop(k)
        return True
    if s.overlay is not None:
        record = bound_overlay(s.overlay, s, ctx.projection, ctx.decisions)
        if record is not None:
            _overlay_key(ctx, record, k)
            return True
        if card_absent(s, ctx.decisions) is not None:
            _escape_only(ctx, k, "the card states only that nothing is recorded")
            return True
        return False
    if s.route not in CARD_ROUTES or k not in _CARD_KEYS:
        return False
    card = bound_card(s, ctx.decisions)
    if card is None:
        if card_absent(s, ctx.decisions) is None:
            return False
        ctx.noop(k)
        return True
    _card_key(ctx, card, k)
    return True


def _claims(ctx: Ctx) -> bool:
    """Return whether a bound overlay or card holds the keyboard, bound or stating absence."""
    s, held = ctx.s, ctx.decisions
    if s.overlay is not None:
        found = bound_overlay(s.overlay, s, ctx.projection, held)
        return found is not None or card_absent(s, held) is not None
    return s.route in CARD_ROUTES and (
        bound_card(s, held) is not None or card_absent(s, held) is not None
    )


def _refused_card(
    ctx: Ctx, k: str, *, kind: Kind, verb: str, target: str, noun: str, why: str
) -> None:
    """Open the consequence card for a verb no daemon mutator carries: refused, nothing sent.

    Args:
        ctx: The keystroke's context.
        k: The key that asked for the verb.
        kind: The mutation family the verb belongs to.
        verb: The verb, as the card names it.
        target: The record it would act on.
        noun: What the target is called.
        why: What the verb would change, stated even though it is refused.
    """
    s = ctx.s
    item = Item(
        key=target,
        title=None,
        revision=None,
        status=None,
        effects=(why,),
        not_effects=("nothing is sent, so no record moves",),
        refusal=Refusal(
            code="unbound_verb",
            reason=binding_refusal(noun, verb),
            remediation="Leave it as it is until a daemon verb carries it.",
        ),
        unknown="",
        request=None,
        stale_token="",
    )
    s.c_target = None
    s.mutation = Card(
        kind=kind,
        origin=verb,
        action=verb,
        noun=noun,
        items=(item,),
        if_stale="nothing is sent, so nothing can go stale",
        authority=f"{kind} · refused on the card before any request",
        opened_at=ctx.clock.now(),
    )
    open_overlay(s, "consequence", subject=target)
    ctx.log(k, f"{verb} {target} → consequence card · refused")


def unless_bound(seam: Callable[[Ctx, str, bool], bool]) -> Callable[[Ctx, str, bool], bool]:
    """Return a card route's register-drawn ``seam``, silent while the card is drawn from a record.

    The route's own seam walks the prototype registers; a card drawn from a held record or
    read model, or stating that none is held, has no register row for it to walk.
    """

    def guarded(ctx: Ctx, key: str, shift: bool) -> bool:
        s = ctx.s
        if ctx.projection is not None:
            # a card drawn from a read model draws no register region for Tab to walk
            if key == "Tab":
                ctx.log(key, "no region is drawn here · nothing to walk")
                return True
            return False
        if bound_card(s, ctx.decisions) is not None or card_absent(s, ctx.decisions) is not None:
            return False
        return seam(ctx, key, shift)

    return guarded


def _escape_only(ctx: Ctx, k: str, why: str) -> None:
    if k == "Escape":
        closed = close_overlay(ctx.s)
        ctx.log("Esc", f"close {closed} · {why}")
    else:
        ctx.noop(k)


def _overlay_key(ctx: Ctx, record: Bound, k: str) -> None:
    decisions = ctx.decisions or DecisionRecords()
    if isinstance(record, QuestionRecord):
        _question_key(ctx, record, decisions, k)
    elif isinstance(record, PauseRecord):
        _pause_key(ctx, record, decisions, k)
    elif isinstance(record, ClaimRecord):
        _cursor_key(ctx, k, CLAIM_ROWS, lambda row: claim_copy(record, row))
    elif isinstance(record, AcceptanceBundleView):
        rows = len(evidence_rows(record)) if record.bundle_digest else 0
        copy = (lambda row: acceptance_copy(record, row)) if rows else None
        _cursor_key(ctx, k, rows, copy)
    elif isinstance(record, ReleaseReadinessView):
        _cursor_key(ctx, k, len(record.signals), None)
    elif isinstance(record, DraftRecord):
        _draft_key(ctx, record, k)
    else:
        _escape_only(ctx, k, "nothing was held, nothing was written")


def _cursor_key(ctx: Ctx, k: str, rows: int, copy: Callable[[int], str] | None) -> None:
    """Move a cursor overlay's cursor, copy the row it is on, or leave it."""
    s = ctx.s
    if rows and k in _UP | _DOWN:
        s.sel = max(0, min(rows - 1, s.sel + (1 if k in _DOWN else -1)))
        ctx.log(k, f"row {s.sel + 1} of {rows}")
    elif k == "y" and copy is not None:
        text = copy(s.sel)
        ctx.log("y", f"{copied(ctx.copy(text))} — {text}")
    else:
        _escape_only(ctx, k, "reading changes nothing")


# ---------- the question ----------


def _question_key(ctx: Ctx, q: QuestionRecord, decisions: DecisionRecords, k: str) -> None:
    s = ctx.s
    run_state = decisions.run_states.get(q.run) if q.run is not None else None
    live = answerable(q, run_state) and can_mutate(s)
    if live and k.isdigit() and 1 <= int(k) <= len(q.options):
        option = q.options[int(k) - 1]
        if q.id.startswith(QUESTION_PREFIX):
            # imported here: the Enter table imports this module to open a held row
            from eawf.surfaces.tui.console.enter_keys import send_verb

            send_verb(ctx, k, QuestionAnswer(target=q.id, option_key=option.key), "answer")
            return
        refusal = binding_refusal("question", "answer")
        ctx.log(k, f"answer {q.id} · {option.label} — nothing was written · {refusal}")
    elif live and k == "w" and reply_legal(q, run_state):
        s.reply = {"text": ""}
        ctx.log("w", "reply field open · ≤ 2,000 characters · Enter sends")
    elif live and k == "x":
        _refused_card(
            ctx,
            k,
            kind="answer",
            verb="decline",
            target=q.id,
            noun="question",
            why="the question is recorded as declined, with the reason you give",
        )
    elif k == "Escape":
        close_overlay(s)
        ctx.log("Esc", f"back — {q.id} stays as it is")
    else:
        ctx.noop(k)


# ---------- the pause ----------

_PAUSE_VERBS: Final[dict[str, tuple[str, str, str]]] = {
    "n": (
        "reconcile",
        "the daemon is asked again for the outcome of the pause",
        "it does not restart the run and does not fail the task",
    ),
    "c": (
        "let go",
        "the run is let go; whether its last control landed stays unknown until it answers",
        "it is not a cancel and it does not fail the task",
    ),
}


def _pause_key(ctx: Ctx, p: PauseRecord, decisions: DecisionRecords, k: str) -> None:
    s = ctx.s
    verb = _PAUSE_VERBS.get(k)
    live = unknown_outcome(p, decisions.run_states.get(p.scope)) and can_mutate(s)
    if live and k == "c":
        _refused_card(
            ctx,
            k,
            kind="control",
            verb="let go",
            target=p.scope,
            noun="run",
            why=_PAUSE_VERBS["c"][1],
        )
    elif verb is not None and live:
        name, effects, non_effects = verb
        s.c_target = {
            "verb": name,
            "id": p.scope,
            "kind": "run",
            "effects": effects,
            "not": non_effects,
        }
        open_overlay(s, "consequence", subject=p.scope)
        ctx.log(k, f"{name} {p.scope} → consequence preview")
    elif k == "Escape":
        close_overlay(s)
        ctx.log("Esc", "back — the pause stays as it is")
    else:
        ctx.noop(k)


# ---------- the draft card ----------


def _draft_key(ctx: Ctx, d: DraftRecord, k: str) -> None:
    s = ctx.s
    field = PROMOTION[max(0, min(len(PROMOTION) - 1, s.draft_field))][0]
    if k in _UP | _DOWN:
        s.draft_field = max(0, min(len(PROMOTION) - 1, s.draft_field + (1 if k in _DOWN else -1)))
        ctx.log(k, PROMOTION[s.draft_field][0])
        return
    if k == "Escape":
        close_overlay(s)
        ctx.log("Esc", f"back — {d.id} stays a {d.state.value.lower()}")
        return
    if k not in ("Enter", "p", "x"):
        ctx.noop(k)
        return
    refused = write_refusal(s, ctx.fixture, verb=k, principal_refusal=ctx.principal_refusal)
    if refused:
        ctx.log(k, f"unavailable — {refused}")
        return
    if k == "Enter":
        ctx.log("Enter", f"set {field} · its setting surface is not drawn yet")
    elif k == "p":
        refusal = promote_refusal(d)
        if refusal is not None:
            ctx.notify(refusal, "NOT PROMOTED", Severity.WARN)
            ctx.log("p", f"refused · {refusal}")
        else:
            ctx.log(
                "p",
                f"promote {d.id} — nothing was written · {binding_refusal('backlog', 'promote')}",
            )
    else:
        _refused_card(
            ctx,
            k,
            kind="lifecycle",
            verb="defer",
            target=d.id,
            noun="backlog",
            why="the draft is deferred with the durable reason you give; it keeps its due scope",
        )


# ---------- the cards on sub-surfaces ----------


def _card_key(ctx: Ctx, card: CardRecord, k: str) -> None:
    s = ctx.s
    if k == "y":
        text = _card_copy(card)
        ctx.log("y", f"{copied(ctx.copy(text))} — {text}")
    elif isinstance(card, ArtifactRecord) and k in _UP | _DOWN:
        win = file_window(s, len(card.lines), ctx.h)
        if not (win.above or win.below):
            ctx.noop(k)
            return
        s.art_scroll = max(0, s.art_scroll + (1 if k in _DOWN else -1))
        file_window(s, len(card.lines), ctx.h)
        ctx.log(k, f"scroll {card.file}")
    elif isinstance(card, StepRecord):
        _step_key(ctx, card, k)
    else:
        ctx.noop(k)


def _card_copy(card: CardRecord) -> str:
    if isinstance(card, Rung):
        return f"{card.claim.urn}#rung-{card.rung.rung}"
    if isinstance(card, ArtifactRecord):
        return f"{card.key} · sha256 {card.digest}"
    return f"{card.campaign} · step {card.ordinal}"


def _step_key(ctx: Ctx, st: StepRecord, k: str) -> None:
    s = ctx.s
    regions = step_regions(st)
    if k == "Tab" and len(regions) > 1:
        s.step_reg = PRODUCED if focused_region(s, st) == HISTORY else HISTORY
        ctx.log("Tab", f"region → {s.step_reg}")
        return
    focus = focused_region(s, st)
    if focus is None or k not in _UP | _DOWN:
        ctx.noop(k)
        return
    step = 1 if k in _DOWN else -1
    if focus == HISTORY:
        s.hist_sel = max(0, min(len(st.events) - 1, s.hist_sel + step))
        ctx.log(k, st.events[s.hist_sel][0])
    else:
        s.sel = max(0, min(len(st.produced) - 1, s.sel + step))
        ctx.log(k, st.produced[s.sel])


# ---------- the live Attention row ----------


def open_held_row(ctx: Ctx) -> bool:
    """Open the detail the held Attention row's record kind owns, returning whether one opened.

    A pending action opens its action detail, which the consequence card realises, unless
    it is an operator decision whose options are held: that opens the question detail, the
    card that draws a decision's own options, where approve and decline would not be among
    them. A provider permission is a two-option action, approve or deny, so it opens the
    action detail too, and a question opens its question detail. The row is the one the
    frame drew from the held projection, never a prototype register.
    """
    held = ctx.attention
    if held is None:
        return False
    s = ctx.s
    row = selected_open_row(s, held)
    if row is None:
        # nothing listed, or a sealed row: an answered action has nothing left to confirm
        ctx.log("Enter", "no open attention row is selected — nothing to open")
        return True
    s.sel_id = row.key
    if (
        row.collection in (Epoch2Collection.PENDING_ACTION, Epoch2Collection.OPEN_QUESTION)
        and ctx.decisions is not None
        and ctx.decisions.question(row.key) is not None
    ):
        open_overlay(s, "question", subject=row.key)
        ctx.log("Enter", f"question detail · {row.key}")
        return True
    if row.collection is Epoch2Collection.OPEN_QUESTION:
        ctx.log("Enter", f"{row.key} · its question is not read yet — nothing opened")
        return True
    open_overlay(s, "consequence", subject=row.key)
    ctx.log("Enter", f"action detail · {row.key}")
    return True
