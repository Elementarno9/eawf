"""The attention reducer: severity-first exception buckets over the action register.

One ordering, one label table and one bucket match feed the header's ``!N``, scope home,
the Attention route, the verbs and the consequence card, so none of them can disagree.
Every count is derived from the register when a frame renders; nothing is stored. A
notice is left out of the open count because it never blocks and never needs the operator.
A verb never writes the register here: an answer is sent to the daemon, which seals an open
action once and reports a later answer as superseded, and a verb no daemon mutator carries
is refused with its reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from eawf.kernel.projection.attention import build_attention_view, top_item
from eawf.kernel.projection.compute import ProjectionRow, RouteProjection
from eawf.kernel.projection.registers import build_register_view
from eawf.surfaces.tui.console.action_menu import Availability, MenuVerb, VerbWeight
from eawf.surfaces.tui.console.fixture import Action, Fixture
from eawf.surfaces.tui.console.reads import write_refusal
from eawf.surfaces.tui.console.session import Session

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.navigation import Ctx

OPEN = "OPEN"
ATTENTION_ROUTE = "attention"
# The bucket whose sub-buckets a parent filter also matches.
NEEDS = "needs"


@dataclass(frozen=True, slots=True)
class Verb:
    """One attention verb: its name and the state it resolves an action to."""

    name: str
    state: str


VERB: Mapping[str, Verb] = MappingProxyType(
    {
        "a": Verb("answer", "ANSWERED"),
        "x": Verb("deny", "DECLINED"),
        "z": Verb("snooze", "SNOOZED"),
        "v": Verb("resolve", "SEALED"),
    }
)


def bucket_keys(fixture: Fixture) -> list[str]:
    """Return every bucket key in severity order, sub-buckets in place of their parent."""
    out: list[str] = []
    for bucket in fixture.proto.xbuckets:
        if bucket.sub:
            out.extend(sub.key for sub in bucket.sub)
        else:
            out.append(bucket.key)
    return out


def top_bucket(key: str | None) -> str:
    """Return the top-level bucket of ``key``: ``needs`` for ``needs.permission``."""
    return (key or "").split(".")[0]


def bucket_label(fixture: Fixture, key: str) -> str:
    """Return the label of bucket ``key``; a sub-bucket names its parent too."""
    label = key
    for bucket in fixture.proto.xbuckets:
        if bucket.key == key:
            label = bucket.label
        for sub in bucket.sub or ():
            if sub.key == key:
                label = f"{bucket.label} ↳ {sub.label}"
    return label


def _in_bucket(action: Action, key: str) -> bool:
    """Return whether ``action`` falls in bucket ``key``; a parent holds its sub-buckets."""
    if key == NEEDS:
        return top_bucket(action.bucket) == NEEDS
    return action.bucket == key


def bucket_count(fixture: Fixture, key: str) -> int:
    """Return the open actions in bucket ``key``, counted from the register now."""
    return sum(1 for a in fixture.proto.attention if a.state == OPEN and _in_bucket(a, key))


def is_notice(action: Action | None) -> bool:
    """Return whether ``action`` is a notice, which never blocks and never needs an answer."""
    return bool(action and action.kind.startswith("notice"))


def verbs_for(action: Action | None) -> list[str]:
    """Return the verb keys ``action`` accepts: a notice can only be snoozed or resolved."""
    return ["z", "v"] if is_notice(action) else ["a", "x", "z", "v"]


def ordered_actions(fixture: Fixture) -> list[Action]:
    """Return every action, open ones first, each group in bucket severity order."""
    order = {key: i for i, key in enumerate(bucket_keys(fixture))}
    return sorted(
        fixture.proto.attention,
        key=lambda a: (0 if a.state == OPEN else 1, order.get(a.bucket, 0)),
    )


def attn_list(session: Session, fixture: Fixture) -> list[Action]:
    """Return the rows the Attention route shows now, so a verb only reads a visible row."""
    rows = ordered_actions(fixture)
    if not session.bucket:
        return rows
    return [a for a in rows if _in_bucket(a, session.bucket)]


def open_actions(fixture: Fixture) -> list[Action]:
    """Return the open actions that need the operator, in display order."""
    return [a for a in ordered_actions(fixture) if a.state == OPEN and not is_notice(a)]


def open_count(fixture: Fixture) -> int:
    """Return the header's ``!N``: the open actions that need the operator."""
    return len(open_actions(fixture))


def audience_refusal(assignee_ref: str | None, principal: str | None) -> str:
    """Return why ``principal`` may not act on an item addressed to ``assignee_ref``.

    The audience is the attention reducer's own rule -- an item addressed to nobody is
    everyone's, one addressed to a principal is theirs -- and this is the one gate Enter
    and every verb on an Attention row consult, so a refusal always names who may act.

    Args:
        assignee_ref: The principal the item is addressed to, or ``None`` for everyone.
        principal: Who the console acts as, or ``None`` when it acts as nobody.

    Returns:
        The refusal naming the holder, or an empty string when ``principal`` may act.
    """
    if assignee_ref is None or assignee_ref == principal:
        return ""
    who = "you act as nobody" if principal is None else f"you act as {principal}"
    return f"{assignee_ref} only · {who} · relaunch with --actor {assignee_ref} to act on it"


def held_refusal(ctx: Ctx, k: str) -> bool:
    """Refuse ``k`` on the held Attention row the cursor names when it is another's.

    Returns:
        Whether the key was refused, the refusal logged naming who holds the row.
    """
    held = ctx.attention
    row: ProjectionRow | None = None
    if held is not None and ctx.s.sel_id is not None:
        row = next((r for r in held.rows if r.key == ctx.s.sel_id), None)
    refusal = audience_refusal(row.assignee_ref, ctx.principal) if row is not None else ""
    if refusal and row is not None:
        ctx.log(k, f"{row.key} refused — {refusal}")
    return bool(refusal)


def selected_open_row(session: Session, held: RouteProjection) -> ProjectionRow | None:
    """Return the held Attention row the caret names, while it is still open.

    The row is found by the stable id the frame published, never by offset. A sealed
    row has been answered, so it is nothing to act on, and a caret that names no row
    selects nothing.

    Args:
        session: The session whose ``sel_id`` names the row.
        held: The Attention projection the link holds.
    """
    if session.sel_id is None:
        return None
    row = next((r for r in held.rows if r.key == session.sel_id), None)
    if row is None:
        return None
    register = build_register_view(held)
    items = () if register.withheld else build_attention_view(register).items
    return row if any(item.key == row.key for item in items) else None


def top_key(
    fixture: Fixture, attention: RouteProjection | None, *, principal: str | None
) -> str | None:
    """Return the key of the item ``!`` jumps to, or ``None`` when nothing needs anyone.

    A linked console reads the Attention register it holds, this principal's own top
    item first; a console with no link reads its prototype register.

    Args:
        fixture: The prototype registers a console with no link reads.
        attention: The Attention projection the link holds, if any.
        principal: Who the console acts as; ``None`` when it acts as nobody.
    """
    if attention is not None:
        item = top_item(build_register_view(attention), principal=principal)
        return item.key if item is not None else None
    actions = open_actions(fixture)
    return actions[0].id if actions else None


def top_attention(ctx: Ctx, k: str, pane: bool) -> None:
    """Jump to the top attention item from anywhere: one gesture, never a modal.

    The jump is a fresh arrival with no path behind it, as a ``g`` letter is, so it opens
    nothing and leaves no back step. With nothing open the session stays where it is.
    """
    s = ctx.s
    key = top_key(ctx.fixture, ctx.attention, principal=ctx.principal)
    if key is None:
        ctx.log("!", "nothing needs you")
        return
    # a fresh arrival: no bucket, no filter, no scroll, as every route reached by name
    s.bucket, s.filter, s.scroll, s.typing = None, "", 0, False
    s.filters[ATTENTION_ROUTE] = ""
    s.route, s.sel, s.sel_id, s.region, s.subj_id = ATTENTION_ROUTE, 0, key, None, None
    s.back.clear()
    ctx.log("!", f"→ {ATTENTION_ROUTE} · top item {key}")


def attn_row(session: Session, fixture: Fixture) -> Action | None:
    """Return the Attention row under the cursor, the first row past the end."""
    rows = attn_list(session, fixture)
    if not rows:
        return None
    return rows[session.sel] if session.sel < len(rows) else rows[0]


def question_row(session: Session, fixture: Fixture) -> Action:
    """Return the action the question overlay reads: the selected question, else the first.

    Raises:
        LookupError: the register holds no action at all.
    """
    register = fixture.proto.attention
    if not register:
        raise LookupError("the attention register holds no action")
    ordered = sorted(register, key=lambda a: 0 if a.state == OPEN else 1)
    selected = ordered[session.sel] if session.sel < len(ordered) else None
    if selected is not None and "question" in selected.kind:
        return selected
    for action in register:
        if "question" in action.kind:
            return action
    return register[1] if len(register) > 1 else register[0]


def answer_card(action: Action | None) -> str:
    """Return the kind of card an answer to ``action`` opens, empty for the consequence card.

    A question is answered from its numbered options and a readiness item from the
    readiness matrix; every other kind is answered on the consequence preview.
    """
    kind = action.kind if action is not None else ""
    return next((card for card in ("question", "readiness") if card in kind), "")


def is_mutation(session: Session, verb: MenuVerb | None) -> bool:
    """Return whether ``verb`` writes: it carries an authority, or it is an attention verb."""
    if verb is None:
        return False
    return verb.mutates or (verb.key in VERB and session.route == ATTENTION_ROUTE)


def _attention_refusal(session: Session, fixture: Fixture, key: str) -> str:
    """Return why an attention verb cannot act on the selected row, or nothing."""
    row = attn_row(session, fixture)
    if row is None:
        return ""
    if key not in verbs_for(row):
        return "a notice has nothing to " + ("answer" if key == "a" else "deny")
    if row.state != OPEN:
        return f"{row.id} is already {row.state.lower()}"
    return ""


def verb_available(
    session: Session, fixture: Fixture, verb: MenuVerb | None, *, principal_refusal: str = ""
) -> Availability:
    """Return whether ``verb`` can act now; the menu and the key path share this judgement.

    Args:
        session: The session whose route, selection and connection state are judged.
        fixture: The registers the refusal reasons are read from.
        verb: The verb listed or pressed; ``None`` for a letter that binds none.
        principal_refusal: Why every bound write is refused whatever it is, because the
            console's daemon link acts as nobody; empty when it acts as someone, or has
            no link to attribute a write through.
    """
    if verb is None:
        return Availability(False, "no verb")
    if session.route == "backlog" and verb.key == "m" and session.promote:
        missing = session.promote.get("missing", [])
        if missing:
            return Availability(False, "needs " + ", ".join(missing))
        refusal = write_refusal(
            session, fixture, kind=None, verb=verb.verb, principal_refusal=principal_refusal
        )
        return Availability(False, refusal) if refusal else Availability(True)
    if not verb.available:
        return Availability(False, verb.reason)
    if session.route == ATTENTION_ROUTE and verb.key in VERB:
        refusal = _attention_refusal(session, fixture, verb.key)
        if refusal:
            return Availability(False, refusal)
    refusal = (
        write_refusal(
            session,
            fixture,
            kind=session.route,
            verb=verb.verb,
            principal_refusal=principal_refusal,
        )
        if is_mutation(session, verb)
        else ""
    )
    return Availability(False, refusal) if refusal else Availability(True)


def gated(
    session: Session,
    fixture: Fixture,
    *,
    key: str,
    verb: str,
    row: Action | None,
    principal_refusal: str,
) -> bool:
    """Return whether a verb must refuse, logging the live reason when it does.

    Args:
        session: The session whose connection state and key log are used.
        fixture: The registers the refusal reason is read from.
        key: The key that asked for the verb.
        verb: The verb's name, as the refusal names it.
        row: The action the verb would act on, when it acts on one.
        principal_refusal: Why every bound write is refused because the daemon link
            acts as nobody; empty when it acts as someone or there is no link.
    """
    if row is not None and key in VERB and key not in verbs_for(row):
        session.log_key(
            key,
            "a notice has nothing to "
            + ("answer" if key == "a" else "deny")
            + " — snooze or resolve it",
        )
        return True
    if row is not None and row.state != OPEN:
        session.log_key(
            key, f"{row.id} is already {row.state.lower()} — a resolved action is immutable"
        )
        return True
    refusal = write_refusal(
        session,
        fixture,
        kind=ATTENTION_ROUTE if key in VERB else None,
        verb=VERB[key].name if key in VERB else verb,
        principal_refusal=principal_refusal,
    )
    if refusal:
        session.log_key(key, f"{verb} is unavailable — {refusal}")
        return True
    return False


def menu_verbs(session: Session, fixture: Fixture) -> tuple[MenuVerb, ...]:
    """Return the route's verbs in menu order: light verbs first."""
    return fixture.menus.verbs(session.route)


def is_light(verb: MenuVerb | None) -> bool:
    """Return whether ``verb`` acts at once instead of previewing its consequence."""
    return verb is not None and verb.weight == VerbWeight.LIGHT
