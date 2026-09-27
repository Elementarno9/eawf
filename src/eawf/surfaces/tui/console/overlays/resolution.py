"""The resolution card: why a target no longer resolves, and what of it is still addressable.

A target that no longer resolves ends in exactly one of five ways -- missing, moved,
retired, denied or purged -- and the card names which, what that means and what still
reads. The target and its ending are captured when the card opens; a card opened with
neither is the prototype registers' purged Run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.overlays.chassis import crumb

NAME = "resolution"
TARGET = pt.PURGED_RUN
# The revision the prototype registers purged their Run at.
_PURGED_AT = "41,002"


class Ending(StrEnum):
    """The five ways a target stops resolving, in the order the card lists them."""

    MISSING = "missing"
    MOVED = "moved"
    RETIRED = "retired"
    DENIED = "denied"
    PURGED = "purged"


@dataclass(frozen=True, slots=True, kw_only=True)
class EndingText:
    """What the card says for one ending.

    Attributes:
        fact: What happened to the target, after its id.
        ending: The ending row: the ending and its qualifier.
        means: What the ending means.
        works: What of the target is still addressable.
        not_works: What of it is not.
    """

    fact: str
    ending: str
    means: str
    works: str
    not_works: str


ENDINGS: Mapping[Ending, EndingText] = MappingProxyType(
    {
        Ending.MISSING: EndingText(
            fact="was never recorded",
            ending="missing · no record was written under this id",
            means="nothing was recorded, so nothing can be read",
            works="The link that named it stays, so the broken reference is reported.",
            not_works="there is no record behind the id, and there never was",
        ),
        Ending.MOVED: EndingText(
            fact="moved to a new address",
            ending="moved · the same record, at its new address",
            means="the record is whole; only its address changed",
            works="The record reads in full at its new address.",
            not_works="this address points there and holds no copy of its own",
        ),
        Ending.RETIRED: EndingText(
            fact="was retired by an explicit verb",
            ending="retired · closed, with its history sealed",
            means="the record is sealed, not deleted",
            works="Its record, its history and its lineage stay readable.",
            not_works="no verb acts on it again",
        ),
        Ending.DENIED: EndingText(
            fact="is not readable to your class",
            ending="denied · it exists, and your class may not read it",
            means="it is neither missing nor gone; you cannot see it",
            works="Its id, and the fact that it exists, stay visible.",
            not_works="its fields do not, while your class stays as it is",
        ),
        Ending.PURGED: EndingText(
            fact="events purged",
            ending="purged · retention 7d",
            means="the fact remains; only its events are gone",
            works="The run, its result and its lineage stay readable.",
            not_works="its timeline and raw segment do not, and never will",
        ),
    }
)


def _others(ending: Ending) -> str:
    """Return the four endings ``ending`` is not, joined as a list."""
    rest = [e.value for e in Ending if e is not ending]
    return ", ".join(rest[:-1]) + " or " + rest[-1]


def render(view: View) -> list[str]:
    """Return the resolution card.

    Raises:
        ValueError: the captured ending is not one of the five.
    """
    s, w = view.session, view.w
    word, subject = s.resolution_ending, s.ov_subject
    if word is not None and subject is not None:
        target, ending, at = subject, Ending(word), ""
    else:
        target, ending, at = TARGET, Ending.PURGED, f" · revision {_PURGED_AT}"
    text = ENDINGS[ending]
    rows = [
        header(view, crumb(NAME, target)),
        " this target no longer resolves",
        bar(w),
        f" FACT      {target} {text.fact}{at}",
        thin(w),
        f" ENDING    {text.ending}",
        f"           {text.means}",
        thin(w),
        f" WHAT WORKS  {text.works}",
        f"             {text.not_works}",
        thin(w),
        f" NOT       This is not {_others(ending)} — four other",
        "           endings, each with its own card and its own remedy.",
    ]
    return build(view, rows, keybar([("Y", "copy URN"), ("Esc", "back")], w))
