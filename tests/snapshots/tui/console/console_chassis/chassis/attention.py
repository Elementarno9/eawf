"""The attention reducer: eight severity-first exception buckets, one open count.

One ordering, one label table, one match; the header's ``!N``, scope home, the Attention
route, the verbs and the consequence card all read the same list. A notice is excluded
from the open count because it never blocks and never needs the operator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..chassis.seam import can_mutate, mut_reason

if TYPE_CHECKING:
    from ..chassis.fixture import Action, Fixture
    from ..chassis.session import Session

VERB: dict[str, dict[str, str]] = {
    "a": {"name": "answer", "state": "ANSWERED"},
    "x": {"name": "deny", "state": "DECLINED"},
    "z": {"name": "snooze", "state": "SNOOZED"},
    "v": {"name": "resolve", "state": "SEALED"},
}


def xflat(fixture: Fixture) -> list[str]:
    out: list[str] = []
    for b in fixture.proto.xbuckets:
        if b.sub:
            out.extend(s.key for s in b.sub)
        else:
            out.append(b.key)
    return out


def xtop(key: str | None) -> str:
    return str(key or "").split(".")[0]


def xlabel(fixture: Fixture, key: str) -> str:
    lab = key
    for b in fixture.proto.xbuckets:
        if b.key == key:
            lab = b.label
        for s in b.sub or ():
            if s.key == key:
                lab = f"{b.label} ↳ {s.label}"
    return lab


def xcount(fixture: Fixture, key: str) -> int:
    return sum(
        1
        for a in fixture.proto.attention
        if a.state == "OPEN" and (xtop(a.bucket) == "needs" if key == "needs" else a.bucket == key)
    )


def xmatch(session: Session, a: Action) -> bool:
    if not session.bucket:
        return True
    return xtop(a.bucket) == "needs" if session.bucket == "needs" else a.bucket == session.bucket


def is_notice(row: Action | None) -> bool:
    return bool(row and (row.kind or "").startswith("notice"))


def verbs_for(row: Action | None) -> list[str]:
    return ["z", "v"] if is_notice(row) else ["a", "x", "z", "v"]


def attn_list0(fixture: Fixture) -> list[Action]:
    order = {k: i for i, k in enumerate(xflat(fixture))}
    return sorted(
        fixture.proto.attention,
        key=lambda a: (0 if a.state == "OPEN" else 1, order.get(a.bucket, 0)),
    )


def attn_list(session: Session, fixture: Fixture) -> list[Action]:
    """The rows the Attention route shows right now, so a verb can only read a visible row."""
    return [a for a in attn_list0(fixture) if xmatch(session, a)]


def open_actions(fixture: Fixture) -> list[Action]:
    return [a for a in attn_list0(fixture) if a.state == "OPEN" and not is_notice(a)]


def open_count(fixture: Fixture) -> int:
    return len(open_actions(fixture))


def attn_row(session: Session, fixture: Fixture) -> Action | None:
    rows = attn_list(session, fixture)
    if not rows:
        return None
    return rows[session.sel] if session.sel < len(rows) else rows[0]


def question_row(session: Session, fixture: Fixture) -> Action | None:
    """The question overlay follows the selected row, not a fixture index."""
    ordered = sorted(fixture.proto.attention, key=lambda a: 0 if a.state == "OPEN" else 1)
    sel = ordered[session.sel] if session.sel < len(ordered) else None
    if sel and "question" in (sel.kind or ""):
        return sel
    for x in fixture.proto.attention:
        if "question" in (x.kind or ""):
            return x
    return fixture.proto.attention[1] if len(fixture.proto.attention) > 1 else None


def is_mutation(session: Session, act: tuple[str, ...] | None) -> bool:
    if not act:
        return False
    return bool(len(act) > 4 and act[4]) or bool(act[0] in VERB and session.route == "attention")


def verb_available(
    session: Session, fixture: Fixture, row: tuple[str, ...] | None
) -> tuple[bool, str]:
    """(ok, why): the menu and the key path reach the same guards, so they cannot disagree."""
    if not row:
        return (False, "no verb")
    if session.route == "backlog" and row[0] == "m" and session.promote:
        missing = session.promote.get("missing", [])
        if missing:
            return (False, "needs " + ", ".join(missing))
        if not can_mutate(session):
            return (False, mut_reason(session, fixture))
        return (True, "")
    if row[2] != "yes":
        return (False, row[3])
    if session.route == "attention" and row[0] in VERB:
        ar = attn_row(session, fixture)
        if ar and row[0] not in verbs_for(ar):
            return (False, "a notice has nothing to " + ("answer" if row[0] == "a" else "deny"))
        if ar and ar.state != "OPEN":
            return (False, f"{ar.id} is already {ar.state.lower()}")
    if is_mutation(session, row) and not can_mutate(session):
        return (False, mut_reason(session, fixture))
    return (True, "")


def gated(session: Session, fixture: Fixture, key: str, verb: str, row: Action | None) -> bool:
    """True when a mutation must refuse; the refusal is logged with a live reason."""
    if row and key in VERB and key not in verbs_for(row):
        session.log_key(
            key,
            "a notice has nothing to "
            + ("answer" if key == "a" else "deny")
            + " — snooze or resolve it",
        )
        return True
    if row and row.state and row.state != "OPEN":
        session.log_key(
            key, f"{row.id} is already {row.state.lower()} — a resolved action is immutable"
        )
        return True
    if can_mutate(session):
        return False
    session.log_key(key, f"{verb} is unavailable — {mut_reason(session, fixture)}")
    return True


def menu_order(session: Session, fixture: Fixture) -> list[tuple[str, ...]]:
    """Light verbs first; everything that writes keeps its consequence card."""
    acts = list(fixture.proto.actions.get(session.route, ()))
    return [a for a in acts if len(a) > 7 and a[7] == "light"] + [
        a for a in acts if not (len(a) > 7 and a[7] == "light")
    ]


def is_light(a: tuple[str, ...] | None) -> bool:
    return bool(a and len(a) > 7 and a[7] == "light")


def collision_audit(fixture: Fixture) -> list[str]:
    """One letter, one meaning, per route: a duplicate key in an action menu is a defect."""
    out: list[str] = []
    for route, acts in fixture.proto.actions.items():
        seen: dict[str, str] = {}
        for a in acts:
            if a[0] in seen:
                out.append(f"{route}: {a[0]} → {seen[a[0]]} / {a[1]}")
            seen[a[0]] = a[1]
    return out
