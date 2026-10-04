"""The subject section of an entity detail frame, and the two-pane frame of an unknown state.

A detail route drawn with a subject says what state that subject is in before it lists any
row: the state's chip word and its meaning, the clock its state allows, where the record
is filed, and the facts the projection read off it. Every word of that section comes from
:mod:`~eawf.surfaces.tui.console.lifecycle`, so the frame and the family gallery cannot
disagree about what a state means.

Two states change the whole frame instead. A Batch merging with no observed host outcome
and a Run that stopped answering are neither success nor failure, so they are drawn as two
panes: what is true, every line a fact with the instant it was recorded, and what is not
known, every line a question the console will not answer for the operator. Recovery is
named in words and retry is never offered, because a retry claims the first attempt is
known to have failed -- exactly what these two states deny.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.projection.truth import TruthState
from eawf.surfaces.tui.console.format import clock_time, group, instant
from eawf.surfaces.tui.console.frame import (
    View,
    bar,
    build,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import KeyEntry
from eawf.surfaces.tui.console.lifecycle import (
    ELAPSED_WORDS,
    Family,
    StateTreatment,
    recovery_landing,
    treatment,
)
from eawf.surfaces.tui.console.reads import attached, reads
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    label,
    more,
    native_header,
    route_crumb,
)
from eawf.surfaces.tui.console.width import cell_len, clip_words

#: The sentence both unknown frames open their close with.
UNKNOWN_CLOSE = "This state means we do not know. It is not success and not failure,"

#: What each unknown state waits on. A merging Batch waits for a reconcile; over a lost Run
#: the stall sweep opens a pause citing the stall, which ends when the Run answers again.
UNKNOWN_WAITS: Final[Mapping[Family, str]] = MappingProxyType(
    {
        Family.BATCH: "and it waits for a reconcile rather than resolving itself.",
        Family.RUN: "and a pause holds it on its stall until it answers or is let go.",
    }
)

#: Why a merging Batch is offered no retry.
NO_MERGE_RETRY = "because a second merge could duplicate the first"

#: Why a lost Run is offered no retry.
NO_RUN_RETRY = "retry is not offered: it would claim the first attempt failed"


def family_of(row: SpineRow) -> Family | None:
    """Return the entity family a row belongs to, or ``None`` for a record with no states."""
    return next((f for f in Family if f.value == row.collection.value), None)


def subject_line(row: SpineRow, what: str, w: int) -> str:
    """Return the line under a detail frame's header: the subject named, then ``what``.

    A detail frame is about one record, so its summary names that record -- its kind, its
    key and its title -- where a list frame would count its rows. The title gives way
    first, so the state after it is never cut off.

    Args:
        row: The subject.
        what: What is said of it after the name, such as its state word.
        w: The frame width the line, with its leading gutter, is drawn in.
    """
    family = family_of(row)
    kind = (family.value if family is not None else row.collection.value).replace("_", " ")
    head, tail = f"{kind.capitalize()} {row.key}", f" · {what}"
    room = w - 2 - cell_len(head) - cell_len(tail)
    title = f" {clip_words(row.title, room)}" if row.title and room > 1 else ""
    return head + title + tail


def subject_of(view: View, spine: SpineView) -> SpineRow | None:
    """Return the frame's subject row, when the session names one the read model holds."""
    found = spine.index_of(view.session.subj_id)
    return spine.rows[found] if found is not None else None


def state_of(row: SpineRow) -> StateTreatment | None:
    """Return the treatment of the row's stored status, or ``None`` when it states none."""
    family = family_of(row)
    status = row.field("status")
    if family is None or status.state is not TruthState.KNOWN or status.value is None:
        return None
    try:
        return treatment(family, status.value)
    except KeyError:
        return None


def at(text: str | None) -> str:
    """Return a stored instant as the clock time a fact is stamped with, or the unknown token."""
    stamped = instant(text)
    return clock_time(stamped) if stamped is not None else UNKNOWN_WORD


def subject_rows(row: SpineRow, state: StateTreatment, w: int) -> list[str]:
    """Return the subject section: its state, its clock, where it is filed and its facts.

    Args:
        row: The subject.
        state: The treatment of the subject's stored status.
        w: The frame width, for the rule under the section.
    """
    facts = row.facts
    moved = f" · last moved {at(facts['updated_at'])}" if "updated_at" in facts else ""
    stated = [
        f"{name} {facts[key]}"
        for key, name in (
            ("priority", "priority"),
            ("criteria", "criteria"),
            ("run", "run"),
            ("target_branch", "branch"),
            ("head", "head"),
            ("tasks", "tasks"),
            ("runs", "runs"),
            ("due", "due"),
            ("failure", "failed:"),
        )
        if key in facts
    ]
    unstated = f"{UNKNOWN_WORD} · the record states nothing more"
    return [
        label("SUBJECT", f"{row.key}{f' {row.title}' if row.title else ''}"),
        label("STATE", f"{state.state} · {state.meaning}"),
        label("CLOCK", f"{ELAPSED_WORDS[state.elapsed]}{moved}"),
        label("FILED IN", row.parent_key or "nothing above it"),
        label("FACTS", " · ".join(stated) or unstated),
        thin(w),
    ]


def _pane(title: str, lines: Sequence[tuple[str, str]]) -> list[str]:
    """Return one pane: its title, then each line as a name and what is stated about it."""
    return [f" {title}", *(f"   {name:<26}{text}" for name, text in lines)]


def _batch_panes(row: SpineRow) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Return what is true of a merging Batch, what is not known, and its recovery."""
    facts = row.facts
    moved = at(facts.get("updated_at"))
    head = facts.get("head")
    true = [
        ("merge authorized", f"stored MERGING at revision {row.revision} · at {moved}"),
        (
            "bound head",
            f"{head[:7]} on {facts.get('target_branch', UNKNOWN_WORD)} · at {moved}"
            if head
            else f"{UNKNOWN_WORD} · no head binding is stated",
        ),
        ("host outcome", f"none observed · as of {moved}"),
    ]
    unknown = [
        "whether the merge landed",
        "whether a retry would duplicate it",
        "whether the head will move",
    ]
    recovery = ["reconcile asks the host what is true · retry is not offered", NO_MERGE_RETRY]
    return true, unknown, recovery


def _run_panes(row: SpineRow) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Return what is true of a Run that stopped answering, what is not known, its recovery."""
    facts = row.facts
    landing = " or ".join(recovery_landing(Family.RUN)) or UNKNOWN_WORD
    true = [
        ("stored status", f"RUNNING at revision {row.revision} · at {at(facts.get('updated_at'))}"),
        ("started", at(facts.get("started_at"))),
        ("last recorded", at(facts.get("updated_at"))),
        ("final report", f"none recorded · as of {at(facts.get('updated_at'))}"),
    ]
    unknown = [
        "whether the work completed",
        "whether a write it began finished",
        "whether a report will still arrive",
    ]
    recovery = [
        f"resume waits for the same Run · let go closes it as {landing}",
        "neither is a guess about what happened",
        NO_RUN_RETRY,
    ]
    return true, unknown, recovery


def unknown_frame(
    view: View, spine: SpineView, row: SpineRow, keys: Sequence[KeyEntry]
) -> list[str]:
    """Return the two-pane frame of a subject whose outcome is not known.

    The cursor is put on the subject, so the action menu acts on the record the frame is
    about and on nothing else.

    Args:
        view: The render being built.
        spine: The read model the subject was found in.
        row: The subject: a merging Batch, or a Run that stopped answering.
        keys: The route's key table; it carries no retry, because no route binds one.

    Raises:
        ValueError: ``row`` is neither a Batch nor a Run, the only families the registry
            names an unknown for.
    """
    session, w = view.session, view.w
    family = family_of(row)
    if family is Family.BATCH:
        true, unknown, recovery = _batch_panes(row)
        what = "MERGING · host outcome unknown"
    elif family is Family.RUN:
        true, unknown, recovery = _run_panes(row)
        # the same words Activity and Needs you use for a Run whose stall stands
        what = "RUNNING · stalled · stopped responding"
    else:
        raise ValueError(f"{row.key} is not a record whose outcome the registry calls unknown")
    session.sel_id = row.key
    session.sel = spine.index_of(row.key) or 0
    steps = [*([row.parent_key] if row.parent_key else []), row.key]
    rows: list[str] = [
        native_header(view, route_crumb(view, spine, *steps), spine.scope_id),
        f" {subject_line(row, what, w)}",
        bar(w),
    ]
    rd = reads(session)
    if not rd.complete:
        rows += [label("ATTACHED", attached(rd, revision=group(int(spine.source_cursor)))), thin(w)]
    rows += [
        *_pane("WHAT IS TRUE", true),
        thin(w),
        *_pane("WHAT IS NOT known", [(line, "") for line in unknown]),
        thin(w),
        label("RECOVERY", recovery[0]),
        *(more(line) for line in recovery[1:]),
        thin(w),
        f"   {UNKNOWN_CLOSE}",
        f"   {UNKNOWN_WAITS[family]}",
        thin(w),
    ]
    return build(view, rows, route_keys_bar(view, keys))


__all__ = [
    "NO_MERGE_RETRY",
    "NO_RUN_RETRY",
    "UNKNOWN_CLOSE",
    "UNKNOWN_WAITS",
    "at",
    "family_of",
    "state_of",
    "subject_line",
    "subject_of",
    "subject_rows",
    "unknown_frame",
]
