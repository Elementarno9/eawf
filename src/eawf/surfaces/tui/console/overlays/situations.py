"""What a decision record renders as: its rendered name and what would end it.

The persisted states are the records' own enums; the frame shows a projection of them,
derived here at render time and never stored beside them. Every situation names what ends
it, so a decision overlay's ``ENDS WHEN`` row is a fact of the record rather than copy, and
two situations never render alike. A raw enum word never reaches a frame.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.state.epoch2.pause import PauseSituation
from eawf.kernel.state.epoch2.question import QuestionSituation
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.surfaces.tui.console.decisions import (
    PERSON_REASONS,
    UNKNOWN_OUTCOME_REASONS,
    ClaimRecord,
    DropReason,
    PauseReason,
    PauseRecord,
    PauseStatus,
    QuestionRecord,
    QuestionStatus,
    RungOutcome,
    RungRecord,
    short_time,
)

#: The ender of a terminal situation: nothing can end it.
TERMINAL: Final = "nothing — terminal"
#: The Run states an asking Run cannot answer from until it is recovered.
UNANSWERABLE_RUN_STATES: Final = frozenset({"LOST", "SUSPENDED:context_boundary"})
#: The state a lost Run is in, which leaves a pause's control outcome unknown.
LOST: Final = "LOST"
#: The rung whose negative is advisory and routes the claim to rung 4.
SCREEN_RUNG: Final = 3
#: The one rung whose pass certifies a claim.
CERTIFYING_RUNG: Final = 4


def stopped_answering(key: str | None, status: object, run_states: Mapping[str, str]) -> bool:
    """Return whether a Run stored as running is known to have stopped answering.

    The Run register carries no heartbeat, so this is read off the Run states the held
    decision records carry -- the stall read's -- and only for a Run still stored as
    running: every route that states a Run's condition asks this one question.

    Args:
        key: The Run's key; ``None`` for a frame about no Run.
        status: The Run's stored status, as its row states it.
        run_states: The Run states the held decision records carry, by Run key.
    """
    return key is not None and status == RunStatus.RUNNING.value and run_states.get(key) == LOST


@dataclass(frozen=True, slots=True)
class Situation:
    """A record's rendered name and what would end it.

    Attributes:
        name: The state in words, as the ``STATE`` row prints it.
        ends: What would end it, as the ``ENDS WHEN`` row prints it.
    """

    name: str
    ends: str


# ---------- the question ----------

_OPEN_QUESTION_ENDS = "an operator or evidence answers it"
#: The situations in which an answer, or an override of a default, may still be given.
_ANSWERABLE: Final = frozenset(
    {
        QuestionSituation.OPEN,
        QuestionSituation.OPEN_BLOCKING,
        QuestionSituation.OPEN_ESCALATED,
        QuestionSituation.DEFAULTED_OVERRIDE_OPEN,
    }
)


def question_kind(
    q: QuestionRecord, *, principal: str | None, run_state: str | None
) -> QuestionSituation:
    """Return which situation of the question projection ``q`` is in.

    A record the daemon read carries the situation its projection computed; one that
    arrived without it -- a waiting operator decision, a replayed record -- has it
    derived here from the same persisted fields.

    Args:
        q: The question.
        principal: Who the console acts as, which tells an answer by you from one elsewhere.
        run_state: The asking Run's state, which makes an open question unanswerable.
    """
    if q.situation is not None:
        return q.situation
    live = q.status in (QuestionStatus.OPEN, QuestionStatus.BLOCKED)
    if live and run_state in UNANSWERABLE_RUN_STATES:
        return QuestionSituation.UNANSWERABLE
    if q.status is QuestionStatus.OPEN:
        return QuestionSituation.OPEN
    if q.status is QuestionStatus.BLOCKED:
        return (
            QuestionSituation.OPEN_ESCALATED if q.escalated_by else QuestionSituation.OPEN_BLOCKING
        )
    if q.status is QuestionStatus.ANSWERED:
        mine = principal is not None and q.resolution_actor == principal
        return QuestionSituation.ANSWERED_BY_YOU if mine else QuestionSituation.ANSWERED_ELSEWHERE
    if q.status is QuestionStatus.AUTO_RESOLVED:
        return QuestionSituation.DEFAULTED_OVERRIDE_OPEN
    if q.status is QuestionStatus.SEALED:
        return QuestionSituation.DEFAULTED_SEALED
    if q.drop_reason is DropReason.SUPERSEDED:
        return QuestionSituation.REPLACED
    return QuestionSituation.WITHDRAWN


def question_situation(
    q: QuestionRecord, *, principal: str | None, run_state: str | None
) -> Situation:
    """Return the rendered name of the situation ``q`` is in, and what would end it.

    Args:
        q: The question.
        principal: Who the console acts as, which tells an answer by you from one elsewhere.
        run_state: The asking Run's state, which makes an open question unanswerable.
    """
    kind = question_kind(q, principal=principal, run_state=run_state)
    renders: dict[QuestionSituation, Situation] = {
        QuestionSituation.UNANSWERABLE: Situation(
            f"unanswerable · recover {q.run} first", "the asking Run is recovered or let go"
        ),
        QuestionSituation.OPEN: Situation(
            "open",
            "an operator or claim evidence answers it, or policy selects its declared default",
        ),
        QuestionSituation.OPEN_BLOCKING: Situation("open · blocking", _OPEN_QUESTION_ENDS),
        QuestionSituation.OPEN_ESCALATED: Situation("open · escalated", _OPEN_QUESTION_ENDS),
        QuestionSituation.ANSWERED_BY_YOU: Situation("answered by you", TERMINAL),
        QuestionSituation.ANSWERED_ELSEWHERE: Situation("answered elsewhere", TERMINAL),
        QuestionSituation.DEFAULTED_OVERRIDE_OPEN: Situation(
            f"defaulted · override open until {short_time(q.override_until)}",
            "the override window closes, or an operator overrides inside it",
        ),
        QuestionSituation.DEFAULTED_SEALED: Situation("defaulted · sealed", TERMINAL),
        QuestionSituation.REPLACED: Situation(f"replaced by {q.superseded_by}", TERMINAL),
        QuestionSituation.WITHDRAWN: Situation(
            f"withdrawn · by {q.resolution_actor or 'the system'}", TERMINAL
        ),
    }
    return renders[kind]


def answerable(q: QuestionRecord, run_state: str | None) -> bool:
    """Return whether an answer to ``q`` may be given now, by option or by reply.

    An open or blocking question takes an answer, and a defaulted one takes an override
    inside its window; an asking Run that cannot hear the answer takes none.
    """
    return question_kind(q, principal=None, run_state=run_state) in _ANSWERABLE


def reply_legal(q: QuestionRecord, run_state: str | None) -> bool:
    """Return whether a reply in the operator's own words is legal on ``q`` now.

    A reply is the answer of a question that offers no option; beside options the answer
    is one of them.
    """
    return answerable(q, run_state) and not q.options


# ---------- the pause ----------

REASON_WORDS: Final[Mapping[PauseReason, str]] = MappingProxyType(
    {
        PauseReason.PERMISSION: "a permission is waiting on a person",
        PauseReason.USER: "a question is waiting on a person",
        PauseReason.PROVIDER: "the provider stopped answering",
        PauseReason.LEASE: "the lease on the work is ambiguous",
        PauseReason.POLICY: "a policy predicate holds the work",
        PauseReason.INFRASTRUCTURE: "the infrastructure is degraded",
        PauseReason.EXTERNAL_TRUTH_AMBIGUOUS: "what happened outside is ambiguous",
    }
)


def pause_kind(p: PauseRecord, run_state: str | None) -> PauseSituation:
    """Return which situation of the pause projection ``p`` is in.

    A record the daemon read carries the situation its projection computed; one that
    arrived without it has it derived here from the same persisted fields.

    Args:
        p: The pause.
        run_state: The affected Run's state, which leaves a control outcome unknown.
    """
    if p.situation is not None:
        return p.situation
    if p.status is PauseStatus.HELD:
        return PauseSituation.HELD
    if p.status is PauseStatus.ESCALATED and p.escalation is not None:
        return PauseSituation.ESCALATED
    if p.status is PauseStatus.RESOLVED:
        return PauseSituation.RESOLVED
    if p.status is PauseStatus.CANCELLED:
        return PauseSituation.CANCELLED
    if p.reason in PERSON_REASONS:
        return PauseSituation.WAITING_ON_PERSON
    if p.reason in UNKNOWN_OUTCOME_REASONS and run_state == LOST:
        return PauseSituation.CONTROL_OUTCOME_UNKNOWN
    return PauseSituation.WAITING_ON_CHECK


def unknown_outcome(p: PauseRecord, run_state: str | None) -> bool:
    """Return whether ``p`` is the lost-Run card, whose control outcome is unknown."""
    return pause_kind(p, run_state) is PauseSituation.CONTROL_OUTCOME_UNKNOWN


def pause_situation(p: PauseRecord, run_state: str | None) -> Situation:
    """Return the rendered name of the situation ``p`` is in, and what would end it.

    Args:
        p: The pause.
        run_state: The affected Run's state, which leaves a control outcome unknown.
    """
    kind = pause_kind(p, run_state)
    if kind is PauseSituation.HELD:
        return Situation(
            f"held · by {p.held_by} · Hold {p.hold_id}",
            "the holder, or a principal of the same authority class, releases the Hold — "
            "no predicate will",
        )
    if kind is PauseSituation.ESCALATED and p.escalation is not None:
        raised = p.escalation.raised_ref
        return Situation(
            f"escalated · {p.escalation.cause.value} · now waiting on {raised}",
            f"this pause never ends; {raised} is answered",
        )
    if kind is PauseSituation.RESOLVED:
        return Situation(
            f"resolved · {p.resume_predicate} observed at {short_time(p.resolved_at)}", TERMINAL
        )
    if kind is PauseSituation.CANCELLED:
        return Situation("cancelled · enclosing work cancelled", TERMINAL)
    if kind is PauseSituation.WAITING_ON_PERSON:
        return Situation("waiting on a person", f"an eligible principal answers {p.waiting_on}")
    if kind is PauseSituation.CONTROL_OUTCOME_UNKNOWN:
        return Situation(
            "waiting on a check · the control outcome is unknown",
            "the predicate is observed, or you reconcile or let go",
        )
    return Situation("waiting on a check", "the predicate is observed")


# ---------- the claim ----------


def claim_standing(c: ClaimRecord) -> Situation:
    """Return the claim's standing, derived from its ladder and never stored.

    ``certified`` needs rungs 1, 2 and 4 passed on the automated path; an attestation at
    rung 4 is ``attested``; a returned negative at rung 1, 2 or 4 refutes it; a rung 1 that
    cannot fetch a digest leaves a reference unresolved. A rung 3 negative is advisory and
    refutes nothing.
    """
    by = {r.rung: r for r in c.rungs}
    if by[1].digest_unavailable:
        return Situation(
            "unresolved reference · ∅ unavailable", "rung 1 can fetch the digest again"
        )
    if any(by[n].outcome is RungOutcome.FAIL for n in (1, 2, CERTIFYING_RUNG)):
        return Situation("refuted", "nothing — a refuted claim is superseded by a new claim")
    if all(by[n].outcome is RungOutcome.PASS for n in (1, 2, CERTIFYING_RUNG)):
        return Situation("certified", "a later rung outcome refutes it")
    if by[CERTIFYING_RUNG].outcome is RungOutcome.ATTESTED:
        return Situation("attested", "rung 4 runs automated and passes or refutes it")
    return Situation("uncertified", "rungs 1, 2 and 4 pass on the automated path")


def rung_outcome(c: ClaimRecord, r: RungRecord) -> str:
    """Return rung ``r``'s outcome in words, never inferred from the rungs around it."""
    if r.outcome is RungOutcome.PASS:
        return "pass"
    if r.outcome is RungOutcome.ATTESTED:
        return "attested"
    if r.outcome is RungOutcome.FAIL:
        return "advisory negative · routed to rung 4" if r.rung == SCREEN_RUNG else "FAILED"
    if r.outcome is RungOutcome.UNKNOWN:
        return "? unknown"
    below = next(
        (x.rung for x in c.rungs if x.rung < r.rung and x.outcome is not RungOutcome.PASS),
        r.rung,
    )
    return f"not run · ∅ awaiting rung {below}"


def rung_means(r: RungRecord) -> str:
    """Return what rung ``r``'s outcome means for the claim."""
    if r.outcome is RungOutcome.PASS:
        if r.rung == CERTIFYING_RUNG:
            return "This rung certifies the claim."
        return "This rung holds — it does not certify the claim on its own."
    if r.outcome is RungOutcome.ATTESTED:
        return "This rung attests — no automated check stands behind it."
    if r.outcome is RungOutcome.FAIL:
        if r.rung == SCREEN_RUNG:
            return "An advisory negative — it routes the claim to rung 4 and blocks nothing."
        return "This rung refutes the claim."
    return "No outcome yet — unknown, not failed."


# ---------- the release ----------

_IN_FLIGHT = "in flight · nothing is released yet"
_UNKNOWN = "the outcome is unknown · nothing is confirmed released"
#: The states from which the release's one protected approval exists.
APPROVED_ONWARD: Final = frozenset(
    {
        ReleaseStatus.APPROVED,
        ReleaseStatus.PUBLISHING,
        ReleaseStatus.PUBLISH_TIMEOUT,
        ReleaseStatus.VERIFYING,
        ReleaseStatus.RECOVERING,
        ReleaseStatus.BAKED,
        ReleaseStatus.RELEASED,
        ReleaseStatus.PARTIALLY_RELEASED,
    }
)


def release_situation(
    status: ReleaseStatus, *, head: str | None, red: str | None, cause: str | None
) -> Situation:
    """Return how release ``status`` renders on the readiness matrix and what ends it.

    Args:
        status: The release's state.
        head: The head the approval binds, when one does.
        red: The readiness signal that failed preflight, when one is known.
        cause: Why a candidate returned to draft, when one did.
    """
    s = ReleaseStatus
    if status is s.DRAFT:
        name = f"draft · invalidated · {cause}" if cause else "draft"
        return Situation(name, "an operator pins the manifest with accepted membership")
    if status is s.CANDIDATE:
        return Situation(
            "candidate · pinned · awaiting approval",
            "an operator approves against the current head, or preflight fails",
        )
    if status is s.PREFLIGHT_FAILED:
        return Situation(
            f"preflight failed · {red or '? unknown which signal'} is red",
            "the red signal is repaired and the manifest is pinned again",
        )
    if status is s.APPROVED:
        return Situation(
            f"approved · bound to head {head or '? unknown'} · exact",
            "publication starts, or the bound head moves and it returns to DRAFT",
        )
    return _PUBLICATION_SITUATIONS[status]


_PUBLICATION_SITUATIONS: Final[Mapping[ReleaseStatus, Situation]] = MappingProxyType(
    {
        ReleaseStatus.CANCELLED: Situation("cancelled", TERMINAL),
        ReleaseStatus.PUBLISHING: Situation(
            f"publishing · {_IN_FLIGHT}", "every target reports, or one times out"
        ),
        ReleaseStatus.PUBLISH_TIMEOUT: Situation(
            f"publish timed out · {_UNKNOWN}", "each target's final state is observed"
        ),
        ReleaseStatus.VERIFYING: Situation(
            f"verifying · {_IN_FLIGHT}", "observation confirms every target, or finds a failure"
        ),
        ReleaseStatus.RECOVERING: Situation(
            f"recovering · {_UNKNOWN}", "recovery settles each target"
        ),
        ReleaseStatus.BAKED: Situation("baked", TERMINAL),
        ReleaseStatus.RELEASED: Situation("released", TERMINAL),
        ReleaseStatus.PARTIALLY_RELEASED: Situation(
            "partially released · its own state — never success, never failure; "
            "recovery names each target",
            TERMINAL,
        ),
    }
)
