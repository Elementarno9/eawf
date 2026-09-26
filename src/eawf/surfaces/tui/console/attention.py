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

from eawf.surfaces.tui.console.action_menu import Availability, MenuVerb, VerbWeight
from eawf.surfaces.tui.console.fixture import Action, Fixture
from eawf.surfaces.tui.console.operations import binding_refusal
from eawf.surfaces.tui.console.reads import can_mutate, mut_reason
from eawf.surfaces.tui.console.session import Session

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


def verb_available(session: Session, fixture: Fixture, verb: MenuVerb | None) -> Availability:
    """Return whether ``verb`` can act now; the menu and the key path share this judgement."""
    if verb is None:
        return Availability(False, "no verb")
    if session.route == "backlog" and verb.key == "m" and session.promote:
        missing = session.promote.get("missing", [])
        if missing:
            return Availability(False, "needs " + ", ".join(missing))
        if not can_mutate(session):
            return Availability(False, mut_reason(session, fixture))
        return Availability(True)
    if not verb.available:
        return Availability(False, verb.reason)
    if session.route == ATTENTION_ROUTE and verb.key in VERB:
        refusal = _attention_refusal(session, fixture, verb.key)
        if refusal:
            return Availability(False, refusal)
    refusal = _write_refusal(session, fixture, verb) if is_mutation(session, verb) else ""
    return Availability(False, refusal) if refusal else Availability(True)


def _write_refusal(session: Session, fixture: Fixture, verb: MenuVerb) -> str:
    """Return why a writing verb cannot act: the link refuses writes, or no daemon verb exists.

    The connection state is judged first, so an offline console names the state that
    stops every write rather than one verb's missing mutator.
    """
    if not can_mutate(session):
        return mut_reason(session, fixture)
    return binding_refusal(session.route, verb.verb)


def gated(session: Session, fixture: Fixture, *, key: str, verb: str, row: Action | None) -> bool:
    """Return whether a verb must refuse, logging the live reason when it does.

    Args:
        session: The session whose connection state and key log are used.
        fixture: The registers the refusal reason is read from.
        key: The key that asked for the verb.
        verb: The verb's name, as the refusal names it.
        row: The action the verb would act on, when it acts on one.
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
    if not can_mutate(session):
        session.log_key(key, f"{verb} is unavailable — {mut_reason(session, fixture)}")
        return True
    unbound = binding_refusal(ATTENTION_ROUTE, VERB[key].name) if key in VERB else ""
    if unbound:
        session.log_key(key, f"{verb} is unavailable — {unbound}")
        return True
    return False


def menu_verbs(session: Session, fixture: Fixture) -> tuple[MenuVerb, ...]:
    """Return the route's verbs in menu order: light verbs first."""
    return fixture.menus.verbs(session.route)


def is_light(verb: MenuVerb | None) -> bool:
    """Return whether ``verb`` acts at once instead of previewing its consequence."""
    return verb is not None and verb.weight == VerbWeight.LIGHT
