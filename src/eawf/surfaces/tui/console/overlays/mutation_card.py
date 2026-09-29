"""The linked consequence card's frame: six panes before confirmation, one row per target after.

Before confirmation a single target draws ACTION, TARGET, EFFECTS, NOT, IF STALE and
AUTHORITY in that order, every time, whatever the verb. A bulk card lists the selection
by identifier above the six panes and adds the UNKNOWN pane below them, which names in
advance the targets that may not confirm. A card refused before sending keeps its six
panes: EFFECTS says nothing will change and why, and the keybar offers only the way back.

After confirmation the card draws the results table -- one row in, one row out -- with the
requested, accepted and confirmed stamps and the outcome each target reached, the detail of
the row under the cursor, and the UNKNOWN pane for any row the daemon has not answered. A
superseded answer draws its conflict: the revision the card was built at, the revision
the action was answered at and by whom, and the trace of stamps. The word ``superseded``
is written in lower case, which the painter leaves plain: a negated outcome is not
coloured as though it were one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from eawf.kernel.runtime.control import ControlDisposition
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import NO_VALUE
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import Pair, keybar
from eawf.surfaces.tui.console.mutation import NO_STAMP, RECONCILABLE, Card, Item, Result
from eawf.surfaces.tui.console.operations import OUTCOME_SENTENCES
from eawf.surfaces.tui.console.overlays.chassis import crumb
from eawf.surfaces.tui.console.width import pad

NAME = "consequence"
_ID = 16
_STAMP = 11


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s" if not noun.endswith("s") else f"{n} {noun}"


def _pane(label: str, lines: Sequence[str], w: int) -> list[str]:
    """Return one pane: its label on the first line, every line wrapped, never clipped."""
    out: list[str] = []
    for i, text in enumerate(line for line in lines if line):
        wrapped = dv.wrap_pane(label if i == 0 else "", text, w)
        out.extend(wrapped)
    return out or dv.wrap_pane(label, "—", w)


def _target(item: Item) -> list[str]:
    title = f" {item.title}" if item.title and item.revision is not None else ""
    if item.revision is None:
        return [f"{item.key}", item.title or ""]
    status = f" · {item.status}" if item.status else ""
    return [f"{item.key}{title}", f"revision {item.revision}{status} · exact"]


def _effects(item: Item) -> list[str]:
    if item.refusal is None:
        return list(item.effects)
    return [
        f"nothing will change · {item.refusal.code} · {item.refusal.reason}",
        f"to proceed: {item.refusal.remediation}",
    ]


def _selected(card: Card, w: int, room: int) -> list[str]:
    """Return the selection by identifier: a row each where they fit, else wrapped ids."""
    rows = []
    for item in card.items:
        fate = (
            f"refused · {item.refusal.code}"
            if item.refusal is not None
            else "unknown · may not confirm"
            if item.unknown
            else "will move"
        )
        rows.append(f"{pad(item.key, _ID)} {pad(item.status or '? unstated', 22)} {fate}")
    if len(rows) <= room:
        return _pane("SELECTED", rows, w)
    return _pane("SELECTED", [", ".join(item.key for item in card.items)], w)


def _unknown(card: Card) -> list[str]:
    unsure = [item for item in card.items if item.unknown]
    if not unsure:
        return ["none · every target states its status at an exact revision"]
    named = ", ".join(f"{item.key} {item.unknown}" for item in unsure)
    return [f"{named}. They will read unknown until the daemon answers or you reconcile."]


def preview_frame(view: View, card: Card) -> list[str]:
    """Return the card before confirmation: the six panes, with the bulk panes around them."""
    w, h = view.w, view.h
    first = card.items[0]
    refused = not card.sendable
    subject = first.key if not card.bulk else _plural(len(card.items), card.noun)
    subtitle = card.note or (
        f"refused before sending · {first.refusal.code}"
        if refused and first.refusal is not None
        else "nothing has happened yet"
        if not card.bulk
        else f"{_plural(len(card.items), card.noun)} selected · one operation, "
        f"{len(card.items)} results"
    )
    top = [header(view, crumb(NAME, subject)), f" {subtitle}", bar(w)]
    if card.bulk:
        action = [f"{card.action} · {_plural(len(card.items), card.noun)}"]
        target = [
            f"{_plural(len(card.items), card.noun)} · each at its own revision · exact",
        ]
        effects = [
            f"each {card.noun} that will move: {first.effects[0].split(' moves ', 1)[-1]}",
            *first.effects[1:2],
        ]
        panes = [
            *_pane("ACTION", action, w),
            *_pane("TARGET", target, w),
            *_pane("EFFECTS", effects, w),
            *_pane("NOT", ["; ".join(first.not_effects)], w),
            *_pane("IF STALE", [card.if_stale], w),
            *_pane("AUTHORITY", [card.authority], w),
        ]
        unknown = _pane("UNKNOWN", _unknown(card), w)
        room = h - 1 - len(top) - len(panes) - len(unknown) - 2
        body = [*_selected(card, w, room), thin(w), *panes, thin(w), *unknown]
        pairs: tuple[Pair, ...] = (
            ("Enter", f"confirm all {len(card.sendable)}"),
            ("Esc", "cancel — nothing happens"),
        )
    else:
        body = [
            *_pane("ACTION", [card.action], w),
            *_pane("TARGET", _target(first), w),
            thin(w),
            *_pane("EFFECTS", _effects(first), w),
            thin(w),
            *_pane("NOT", list(first.not_effects), w),
            thin(w),
            *_pane("IF STALE", [card.if_stale], w),
            thin(w),
            *_pane("AUTHORITY", [card.authority], w),
        ]
        pairs = (
            (("Esc", "back — nothing happens"),)
            if refused
            else (("Enter", "confirm"), ("Esc", "cancel — nothing happens"))
        )
    return build(view, [*top, *body], keybar(pairs, w))


def _counts(card: Card) -> str:
    words: dict[str, int] = {}
    for row in card.results:
        words[row.disposition.value] = words.get(row.disposition.value, 0) + 1
    tally = " · ".join(f"{n} {word}" for word, n in words.items())
    return f"{card.action} · {len(card.results)} requested · {tally}"


def _result_row(row: Result, selected: bool) -> str:
    caret = "▸" if selected else " "
    return (
        f" {caret} {pad(row.key, _ID)}{pad(row.requested, _STAMP)}{pad(row.accepted, _STAMP)}"
        f"{pad(row.confirmed, _STAMP)}{row.disposition.value}"
    )


def _conflict(card: Card, item: Item, row: Result, w: int) -> list[str]:
    """Return the conflict a superseded answer draws: both revisions exact, and the trace."""
    built = f"preview built at revision {item.revision} · exact"
    answered = (
        f"answered at revision {row.revision} · exact · by {row.answered_by or 'another principal'}"
    )
    trace = (
        f"previewed +0.0s · requested {row.requested} · answered {row.answered} · "
        "neither applied nor refused · no ledger entry and no receipt from you"
    )
    return [
        *_pane("CONFLICT", [built, answered], w),
        *_pane("TRACE", [trace], w),
    ]


#: Which of the request, accepted and confirmed stamps each outcome shows. An outcome shows
#: a stamp only for a phase the request reached: unknown and recovery sit at the
#: effected phase, so they keep the accepted stamp; invalidated and rejected ended
#: at the request; superseded shows none, its keypress belonging to the conflict pane.
LEDGER_STAMPS: Mapping[ControlDisposition, tuple[bool, bool, bool]] = MappingProxyType(
    {
        ControlDisposition.IDLE: (False, False, False),
        ControlDisposition.REQUESTING: (True, False, False),
        ControlDisposition.ACCEPTED: (True, True, False),
        ControlDisposition.CONFIRMED: (True, True, True),
        ControlDisposition.REJECTED: (True, False, False),
        ControlDisposition.INVALIDATED: (True, False, False),
        ControlDisposition.UNKNOWN: (True, True, False),
        ControlDisposition.RECOVERY: (True, True, False),
        ControlDisposition.SUPERSEDED: (False, False, False),
    }
)


def ledger_cell(
    disposition: ControlDisposition,
    *,
    control: str,
    target: str,
    issued_by: str,
    stamps: tuple[str, str, str],
    note: str,
) -> tuple[str, str]:
    """Return one control-ledger cell: the chip, control and issuer, then its stamps and note.

    Args:
        disposition: The outcome the request reached; its upper-case name is the chip.
        control: The control asked for, as the operator reads it.
        target: The record it was asked of.
        issued_by: Who issued it.
        stamps: The request, accepted and confirmed instants, in that order; a stamp the
            outcome does not show is drawn as the no-value dash whatever is passed.
        note: What is true about the outcome, in words.

    Returns:
        The cell's two lines.
    """
    shown = LEDGER_STAMPS[disposition]
    req, acc, cfm = (
        at if on and at != NO_STAMP else NO_VALUE for at, on in zip(stamps, shown, strict=True)
    )
    return (
        f"{disposition.name}  {control} · {target}  issued by {issued_by}",
        f"req {req}  acc {acc}  cfm {cfm}  {note}",
    )


def results_frame(view: View, card: Card) -> list[str]:
    """Return the card after confirmation: one row per target, never one verdict."""
    w = view.w
    subject = card.items[0].key if not card.bulk else _plural(len(card.items), card.noun)
    top = [header(view, crumb(NAME, subject)), f" {_counts(card)}", bar(w)]
    table = [
        f" {pad('RESULTS', _ID + 2)}{pad('REQUESTED', _STAMP)}{pad('ACCEPTED', _STAMP)}"
        f"{pad('CONFIRMED', _STAMP)}OUTCOME",
        *(_result_row(row, i == card.sel) for i, row in enumerate(card.results)),
    ]
    row = card.results[min(card.sel, len(card.results) - 1)]
    item = next(item for item in card.items if item.key == row.key)
    if row.disposition is ControlDisposition.SUPERSEDED:
        detail = _conflict(card, item, row, w)
    else:
        detail = _pane("DETAIL", [row.detail or f"{row.key} · waiting for the daemon's answer"], w)
    cell = ledger_cell(
        row.disposition,
        control=card.action,
        target=row.key,
        issued_by=card.issuer or "this console",
        stamps=(row.requested, row.accepted, row.confirmed),
        note=OUTCOME_SENTENCES[row.disposition].split(" · ", 1)[-1],
    )
    # the cell keeps its own spacing, so it is set under its label rather than re-wrapped
    body = [*table, thin(w), *detail, f" {pad('LEDGER', 10)}{cell[0]}", f" {pad('', 10)}{cell[1]}"]
    unknown = [r.key for r in card.results if r.disposition in RECONCILABLE]
    if unknown:
        body += [
            thin(w),
            *_pane(
                "UNKNOWN",
                [
                    f"{', '.join(unknown)} accepted nothing the console heard back. They are "
                    "not applied and not refused; these rows stay until reconcile answers them."
                ],
                w,
            ),
        ]
    pairs: tuple[Pair, ...] = (("↑↓", "row"),)
    if unknown:
        pairs += (("n", "reconcile the unknown"),)
    pairs += (("Esc", "back"),)
    return build(view, [*top, *body], keybar(pairs, w))


def render(view: View) -> list[str]:
    """Return the linked card's frame for the session's card.

    Raises:
        TypeError: the session holds no linked card, which the overlay binding makes
            unreachable: it draws this frame only while one is held.
    """
    card = view.session.mutation
    if not isinstance(card, Card):
        raise TypeError("the linked consequence card is drawn only while a card is held")
    return results_frame(view, card) if card.results else preview_frame(view, card)


__all__ = [
    "LEDGER_STAMPS",
    "NAME",
    "NO_STAMP",
    "ledger_cell",
    "preview_frame",
    "render",
    "results_frame",
]
