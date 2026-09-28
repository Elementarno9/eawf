"""How every lifecycle state of every entity family renders: its chip, its meaning, its class.

A family is one entity's closed set of states. The set is never authored here: it is read
off the kernel's own status machines -- the transition registry's status enums, its
ambiguity labels, and the pending-action edge table -- so a state the kernel adds is an
import failure here until it is given a treatment, and a treatment for a state the kernel
does not have is refused the same way. What is authored is how each state reads: a meaning
in plain words, one of four severity classes, and one elapsed treatment.

The four severity classes have fixed meanings and are the only thing colour carries: a
settled good outcome, a state needing a person, a bad or unknown outcome, and waiting with
nothing wrong. The elapsed treatment is fixed per state -- none before work starts, live
while it runs, frozen while it waits, partial where an attempt ended before it reported,
final at an ending -- so no clock on a frame implies a liveness its state denies.

Two properties are derived rather than authored, because the registry already answers
them. A state with no outgoing edge is an ending, whose frame states its final value and
offers no lifecycle verb. A state the registry labels ambiguous -- a Batch merging with no
observed host outcome, a Run that stopped answering -- is an unknown, whose frame is two
panes, what is true and what is not known, and whose recovery never offers a retry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from eawf.kernel.state.epoch2.pending_action import PENDING_ACTION_EDGES
from eawf.kernel.state.epoch2.transitions import (
    AMBIGUOUS_STATES,
    RECOVERY_STATES,
    SUCCESS_TERMINALS,
    TERMINAL_STATUSES,
    LifecycleEntity,
    statuses_of,
)


class Family(StrEnum):
    """The entity families whose states the console renders, by the collection they live in."""

    TRACK = "track"
    MILESTONE = "milestone"
    BATCH = "batch"
    TASK = "task"
    RUN = "run"
    RELEASE = "release"
    PENDING_ACTION = "pending_action"


#: The lifecycle entity whose status machine each family's states are read off. The
#: pending action keeps its own edge table outside the registry, so it maps to none.
FAMILY_ENTITY: Final[Mapping[Family, LifecycleEntity | None]] = MappingProxyType(
    {
        Family.TRACK: LifecycleEntity.TRACK,
        Family.MILESTONE: LifecycleEntity.MILESTONE,
        Family.BATCH: LifecycleEntity.DELIVERY_BATCH,
        Family.TASK: LifecycleEntity.TASK,
        Family.RUN: LifecycleEntity.RUN,
        Family.RELEASE: LifecycleEntity.RELEASE,
        Family.PENDING_ACTION: None,
    }
)


class SeverityClass(StrEnum):
    """The four classes a state renders through; the value is the surface its chip is painted."""

    SETTLED = "ok"
    NEEDS_PERSON = "warn"
    BAD_OR_UNKNOWN = "err"
    WAITING = "info"


#: What each class means, as a legend states it.
SEVERITY_MEANING: Final[Mapping[SeverityClass, str]] = MappingProxyType(
    {
        SeverityClass.SETTLED: "a settled good outcome",
        SeverityClass.NEEDS_PERSON: "needs a person",
        SeverityClass.BAD_OR_UNKNOWN: "a bad outcome or an unknown one",
        SeverityClass.WAITING: "waiting, nothing wrong",
    }
)


class Elapsed(StrEnum):
    """How a state's clock reads; the value is the words a gallery row renders it as."""

    NONE = "no elapsed"
    LIVE = "live elapsed"
    FROZEN = "elapsed frozen"
    PARTIAL = "partial elapsed"
    FINAL = "final elapsed"


#: What a detail frame says about the clock under each treatment.
ELAPSED_WORDS: Final[Mapping[Elapsed, str]] = MappingProxyType(
    {
        Elapsed.NONE: "no elapsed · nothing has started",
        Elapsed.LIVE: "elapsed live · it counts while work moves",
        Elapsed.FROZEN: "elapsed frozen · it stops while this waits",
        Elapsed.PARTIAL: "elapsed partial · to the last thing heard",
        Elapsed.FINAL: "elapsed final · this state is an ending",
    }
)


class Layout(StrEnum):
    """What a state changes about its frame beyond the chip."""

    CHIP = "chip"
    FINAL = "final"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class StateTreatment:
    """How one state of one family renders.

    Attributes:
        family: The family the state belongs to.
        state: The chip word: the state's upper-case name.
        meaning: What the state means, in plain words.
        severity: The one class its chip is painted in.
        elapsed: How its clock reads.
        note: What else the frame shows in this state, or empty.
        layout: What the state changes about the frame; derived from the registry.
    """

    family: Family
    state: str
    meaning: str
    severity: SeverityClass
    elapsed: Elapsed
    note: str = ""
    layout: Layout = Layout.CHIP

    @property
    def renders_as(self) -> str:
        """Return the renders-as rule a gallery row states."""
        rule = f"{self.severity.value} chip · {self.elapsed.value}"
        return f"{rule} · {self.note}" if self.note else rule


_S = SeverityClass
_E = Elapsed

#: The authored part of every treatment: meaning, class, clock and note, by family and state.
_AUTHORED: Final[Mapping[Family, Mapping[str, tuple[str, SeverityClass, Elapsed, str]]]] = {
    Family.TRACK: {
        "ACTIVE": ("work may be placed under it", _S.SETTLED, _E.LIVE, ""),
        "RETIRED": ("closed to new work, readable forever", _S.WAITING, _E.FINAL, ""),
    },
    Family.MILESTONE: {
        "PLANNED": ("shaped, no batch started", _S.WAITING, _E.NONE, ""),
        "ACTIVE": ("batches in flight", _S.SETTLED, _E.LIVE, ""),
        "ACCEPTANCE_REVIEW": (
            "bundle sealed, a person accepts",
            _S.NEEDS_PERSON,
            _E.FROZEN,
            "bundle shown",
        ),
        "COMPLETED": ("accepted at a sealed bundle", _S.SETTLED, _E.FINAL, ""),
        "CANCELLED": ("abandoned on an operator's word", _S.WAITING, _E.FINAL, ""),
    },
    Family.BATCH: {
        "PLANNED": ("no work dispatched yet", _S.WAITING, _E.NONE, ""),
        "ACTIVE": ("tasks in flight", _S.SETTLED, _E.LIVE, ""),
        "READY_TO_MERGE": ("exact head, checks passed", _S.SETTLED, _E.FROZEN, "head shown"),
        "MERGING": (
            "the host was asked to merge, no outcome observed",
            _S.BAD_OR_UNKNOWN,
            _E.PARTIAL,
            "we do not know",
        ),
        "MERGED_PENDING_RECONCILIATION": (
            "the host merged, not yet reconciled",
            _S.NEEDS_PERSON,
            _E.FROZEN,
            "reconcile shown",
        ),
        "COMPLETED": ("merged and reconciled", _S.SETTLED, _E.FINAL, ""),
        "CANCELLED": ("abandoned on an operator's word", _S.WAITING, _E.FINAL, ""),
        "FAILED": ("the merge was refused", _S.BAD_OR_UNKNOWN, _E.FINAL, "reason named"),
    },
    Family.TASK: {
        "DRAFT": ("an idea, not yet promoted", _S.WAITING, _E.NONE, ""),
        "DEFERRED": ("set aside for later", _S.WAITING, _E.NONE, ""),
        "DROPPED": ("dropped before delivery", _S.WAITING, _E.FINAL, ""),
        "PLANNED": ("placed in a batch, unclaimed", _S.WAITING, _E.NONE, ""),
        "CLAIMED": ("claimed, no run reporting yet", _S.WAITING, _E.NONE, ""),
        "RUNNING": ("a run is working it", _S.SETTLED, _E.LIVE, ""),
        "READY_TO_INTEGRATE": ("a candidate accepted", _S.SETTLED, _E.FROZEN, ""),
        "COMPLETED": ("integrated", _S.SETTLED, _E.FINAL, ""),
        "CANCELLED": ("stopped on an operator's word", _S.WAITING, _E.FINAL, ""),
        "FAILED": ("no candidate could be accepted", _S.BAD_OR_UNKNOWN, _E.FINAL, ""),
    },
    Family.RUN: {
        "QUEUED": ("waiting for a slot", _S.WAITING, _E.NONE, ""),
        "RUNNING": ("run active", _S.SETTLED, _E.LIVE, ""),
        "SUSPENDED": ("waiting on a named clearing fact", _S.NEEDS_PERSON, _E.FROZEN, ""),
        "LOST": ("stopped responding", _S.BAD_OR_UNKNOWN, _E.PARTIAL, "we do not know"),
        "COMPLETED": ("final report accepted", _S.SETTLED, _E.FINAL, ""),
        "FAILED": ("final report rejected", _S.BAD_OR_UNKNOWN, _E.FINAL, ""),
        "CANCELLED": ("stopped on an operator's word", _S.WAITING, _E.FINAL, ""),
    },
    Family.RELEASE: {
        "DRAFT": ("membership still being chosen", _S.WAITING, _E.NONE, ""),
        "CANDIDATE": ("membership fixed, not approved", _S.NEEDS_PERSON, _E.NONE, ""),
        "PREFLIGHT_FAILED": (
            "a readiness signal failed",
            _S.BAD_OR_UNKNOWN,
            _E.FROZEN,
            "signal named",
        ),
        "APPROVED": ("approved at an exact digest", _S.SETTLED, _E.NONE, "digest shown"),
        "CANCELLED": ("withdrawn before publishing", _S.WAITING, _E.FINAL, ""),
        "PUBLISHING": ("publication in flight", _S.WAITING, _E.LIVE, "targets listed"),
        "PUBLISH_TIMEOUT": (
            "a target has not answered",
            _S.BAD_OR_UNKNOWN,
            _E.PARTIAL,
            "we do not know",
        ),
        "VERIFYING": ("checking what actually landed", _S.WAITING, _E.LIVE, ""),
        "RECOVERING": ("repairing a partial publication", _S.NEEDS_PERSON, _E.LIVE, "per target"),
        "BAKED": ("published and observed stable", _S.SETTLED, _E.FINAL, ""),
        "RELEASED": ("every target confirmed", _S.SETTLED, _E.FINAL, ""),
        "PARTIALLY_RELEASED": (
            "some targets, not all; the version is spent",
            _S.NEEDS_PERSON,
            _E.FINAL,
            "per-target truth",
        ),
    },
    Family.PENDING_ACTION: {
        "CREATED": ("asked, not yet shown to anyone", _S.WAITING, _E.NONE, ""),
        "WAITING": ("shown, a person answers", _S.NEEDS_PERSON, _E.LIVE, ""),
        "SEALED": ("answered; a changed mind is a new question", _S.SETTLED, _E.FINAL, ""),
    },
}


def census(family: Family) -> tuple[str, ...]:
    """Return every state ``family`` has, as the kernel's machines state them, in their order.

    A registry status is listed by its name; an ambiguity label the registry carries beside
    a status -- a Run that stopped answering is still stored ``RUNNING`` -- is listed after
    the statuses when no status already has its name, because it renders as its own state.
    """
    entity = FAMILY_ENTITY[family]
    if entity is None:
        return tuple(status.name for status in PENDING_ACTION_EDGES)
    names = [status.name for status in statuses_of(entity)]
    labels = [label.name for (e, _s), label in AMBIGUOUS_STATES.items() if e is entity]
    return (*names, *(label for label in labels if label not in names))


def _terminal(family: Family) -> frozenset[str]:
    """Return the names of the states nothing leaves."""
    entity = FAMILY_ENTITY[family]
    if entity is None:
        return frozenset(s.name for s, out in PENDING_ACTION_EDGES.items() if not out)
    by_value = {str(status): status.name for status in statuses_of(entity)}
    return frozenset(by_value[value] for value in TERMINAL_STATUSES[entity])


def _names(family: Family, table: Mapping[LifecycleEntity, frozenset[str]]) -> frozenset[str]:
    """Return the names of the states ``table`` lists for ``family``'s entity."""
    entity = FAMILY_ENTITY[family]
    if entity is None:
        return frozenset()
    by_value = {str(status): status.name for status in statuses_of(entity)}
    return frozenset(by_value[value] for value in table[entity])


def _unknown(family: Family) -> frozenset[str]:
    """Return the names of the states whose outcome the registry says is not known.

    The label names the rendered state: a merging Batch is its own status, while a Run
    that stopped answering is the label beside a status that still reads as running.
    """
    entity = FAMILY_ENTITY[family]
    return frozenset(label.name for (e, _value), label in AMBIGUOUS_STATES.items() if e is entity)


def _build() -> Mapping[Family, tuple[StateTreatment, ...]]:
    """Return every family's treatments in census order, refusing a table the kernel disowns.

    Raises:
        ValueError: A family's authored states differ from its census; an ending is not
            drawn with its final clock; an asserted success is not a settled class; an
            unknown or a recovery landing reads as a settled one; or one chip word takes
            two classes across families, which the painter colours by word.
    """
    defects: list[str] = []
    out: dict[Family, tuple[StateTreatment, ...]] = {}
    for family in Family:
        states = census(family)
        authored = _AUTHORED[family]
        missing = [s for s in states if s not in authored]
        extra = [s for s in authored if s not in states]
        defects += [f"{family.value} {s} has no treatment" for s in missing]
        defects += [f"{family.value} {s} is not a state the kernel has" for s in extra]
        terminal, unknown = _terminal(family), _unknown(family)
        success = _names(family, SUCCESS_TERMINALS)
        recovery = _names(family, RECOVERY_STATES)
        rows = []
        for state in states:
            if state not in authored:
                continue
            meaning, severity, elapsed, note = authored[state]
            layout = (
                Layout.UNKNOWN
                if state in unknown
                else Layout.FINAL
                if state in terminal
                else Layout.CHIP
            )
            if state in terminal and elapsed is not Elapsed.FINAL:
                defects.append(f"{family.value} {state} is an ending without its final clock")
            if state in success and severity is not SeverityClass.SETTLED:
                defects.append(f"{family.value} {state} asserts success but is not settled")
            if (state in unknown or state in recovery) and severity is SeverityClass.SETTLED:
                defects.append(f"{family.value} {state} would read as a settled good outcome")
            rows.append(
                StateTreatment(
                    family=family,
                    state=state,
                    meaning=meaning,
                    severity=severity,
                    elapsed=elapsed,
                    note=note,
                    layout=layout,
                )
            )
        out[family] = tuple(rows)
    classes: dict[str, set[SeverityClass]] = {}
    for treated in out.values():
        for row in treated:
            classes.setdefault(row.state, set()).add(row.severity)
    defects += [f"{word} takes two classes" for word, found in classes.items() if len(found) > 1]
    if defects:
        raise ValueError(f"lifecycle treatments disagree with the kernel: {'; '.join(defects)}")
    return MappingProxyType(out)


#: Every family's states, each with its treatment, in the kernel's own order.
TREATMENTS: Final[Mapping[Family, tuple[StateTreatment, ...]]] = _build()

#: The class each chip word is painted in, whatever family it belongs to.
WORD_CLASSES: Final[Mapping[str, SeverityClass]] = MappingProxyType(
    {row.state: row.severity for rows in TREATMENTS.values() for row in rows}
)


def treatment(family: Family, state: str) -> StateTreatment:
    """Return how ``state`` of ``family`` renders; a stored lower-case value is accepted.

    Raises:
        KeyError: ``state`` is not a state of ``family``.
    """
    name = state.upper()
    for row in TREATMENTS[family]:
        if row.state == name:
            return row
    raise KeyError(f"{family.value} has no state {state!r}")


def recovery_landing(family: Family) -> tuple[str, ...]:
    """Return where an operator may land a record whose outcome is unknown, by name."""
    return tuple(sorted(_names(family, RECOVERY_STATES)))


__all__ = [
    "ELAPSED_WORDS",
    "FAMILY_ENTITY",
    "SEVERITY_MEANING",
    "TREATMENTS",
    "WORD_CLASSES",
    "Elapsed",
    "Family",
    "Layout",
    "SeverityClass",
    "StateTreatment",
    "census",
    "recovery_landing",
    "treatment",
]
