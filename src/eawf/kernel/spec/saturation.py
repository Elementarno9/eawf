"""SaturationReport — the four-gate loop-until-dry reducer for a research campaign.

A research campaign accumulates :class:`~eawf.kernel.state.models.Claim` rows
and :class:`~eawf.kernel.state.models.OpenQuestion` rows as it surveys sources.
The round loop that drives the campaign needs a single typed bit to decide
whether to run another survey round or stop: is the campaign *saturated*?
:class:`SaturationReport` is that bit. It is a **pure reducer** over the two
ledgers — given the same ledgers it always returns the same report, performs no
I/O, mutates nothing, and never raises on the read path (a failing gate is data,
not an exception, exactly like the rung-1 EviBound gate in
:mod:`eawf.workflow.evidence.evibound`).

Four gates
----------
A campaign is saturated **iff all four** gates pass (and the ledger is
non-empty — an empty ledger has accumulated no evidence, so the campaign has
not started converging and is never saturated):

(a) **no open question** — every tracked question has reached a terminal
    state. An ``OPEN`` or ``BLOCKED`` question is an unclosed gap, so a
    campaign with any non-terminal question is not saturated. ``ANSWERED`` /
    ``DROPPED`` questions do not block.
(b) **novelty decay** — the rate of *newly arriving* claims has fallen to (or
    below) the novelty floor. Novelty is the count of live claims logged
    within the trailing ``novelty_window`` ending at ``now``; once that count
    is ``<= novelty_floor`` the survey has stopped turning up new assertions.
(c) **no contradiction** — no *live* claim (one that still counts toward
    coverage, i.e. not ``SUPERSEDED``) is ``REFUTED``. A live refuted claim is
    an open contradiction the campaign must resolve before it is dry.
(d) **integration closed** — the claim graph has no dangling edge: every claim
    that names an ``answers_question_id`` points at a question that is actually
    ``ANSWERED``, and every live claim carries at least one ``evidence_refs``
    entry so it integrates into the closed evidence graph (the rung-1 EviBound
    resolver scores *whether* those refs resolve; this gate scores only that a
    live claim is not evidence-less).

The reducer keys on the closed
:class:`~eawf.kernel.state.enums.ClaimStatus` /
:class:`~eawf.kernel.state.enums.OpenQuestionStatus` vocabularies and the
``created_at`` timestamps already carried by the entities — it consumes the
W12 state-resident ledgers, it does not redefine them.

Loop-until-dry
--------------
The round loop calls :meth:`SaturationReport.reduce` at the end of each round
and checks the single :attr:`SaturationReport.saturated` bit: ``True`` means
every gate held and the loop stops (the campaign is dry), ``False`` means at
least one gate is open and the loop runs another round.
:meth:`SaturationReport.blocking_gates` names the still-open gates so the loop
(or the operator) can see *why* the campaign is not yet dry.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion

logger = logging.getLogger(__name__)

#: Trailing window over which a claim counts as "new" for the novelty-decay
#: gate. A campaign whose live claims logged inside this window number at most
#: :data:`DEFAULT_NOVELTY_FLOOR` has stopped turning up fresh assertions.
DEFAULT_NOVELTY_WINDOW: timedelta = timedelta(hours=24)

#: Maximum count of in-window claims for the novelty-decay gate to read as
#: decayed. Zero means "no new claims in the trailing window"; a small positive
#: floor tolerates a residual trickle without holding the loop open forever.
DEFAULT_NOVELTY_FLOOR: int = 0

#: The :class:`ClaimStatus` values that still count toward live coverage. A
#: ``SUPERSEDED`` claim stays in the ledger for traceability but is excluded
#: from every gate's "live" view (it can neither contradict nor dangle).
_LIVE_CLAIM_STATUSES: frozenset[ClaimStatus] = frozenset(
    {ClaimStatus.OPEN, ClaimStatus.SUPPORTED, ClaimStatus.REFUTED}
)

#: The :class:`OpenQuestionStatus` values that leave a question unclosed. A
#: campaign with any question in one of these states fails gate (a).
_UNCLOSED_QUESTION_STATUSES: frozenset[OpenQuestionStatus] = frozenset(
    {OpenQuestionStatus.OPEN, OpenQuestionStatus.BLOCKED}
)


@dataclass(frozen=True)
class SaturationGateResult:
    """One gate's pass/fail outcome plus the rows that explain it.

    Attributes:
        name: Stable gate identifier — one of ``"no_open_question"``,
            ``"novelty_decay"``, ``"no_contradiction"``,
            ``"integration_closed"``.
        passed: ``True`` iff the gate held over the ledger.
        offenders: Ids of the ledger rows that kept the gate open, in
            ledger order. Empty when :attr:`passed` is ``True``. For the
            novelty-decay gate the offenders are the in-window claim ids;
            for the question gate the unclosed question ids; for the
            contradiction / integration gates the offending claim ids.
        detail: Short human-readable line the loop / operator can surface
            alongside the gate name.
    """

    name: str
    passed: bool
    offenders: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class SaturationReport:
    """Typed verdict of the four-gate saturation reducer.

    Produced only by :meth:`reduce` — never hand-constructed on the call
    path. The :attr:`saturated` bit is what the loop-until-dry round loop
    checks: ``True`` stops the loop (the campaign is dry), ``False`` runs
    another round.

    Attributes:
        saturated: ``True`` iff the ledger is non-empty AND every one of
            the four gates in :attr:`gates` passed. An empty Claim ledger
            is never saturated (no evidence has accrued, so the campaign
            has not begun to converge).
        gates: One :class:`SaturationGateResult` per gate, in the fixed
            order (a) no_open_question, (b) novelty_decay,
            (c) no_contradiction, (d) integration_closed.
        live_claim_count: Number of claims that still count toward
            coverage (status in :data:`_LIVE_CLAIM_STATUSES`).
        empty_ledger: ``True`` when the Claim ledger had zero rows — the
            short-circuit reason :attr:`saturated` is ``False`` even
            though the gates over an empty ledger trivially pass.
    """

    saturated: bool
    gates: tuple[SaturationGateResult, ...]
    live_claim_count: int
    empty_ledger: bool

    def gate(self, name: str) -> SaturationGateResult:
        """Return the :class:`SaturationGateResult` named *name*.

        Args:
            name: One of the four stable gate identifiers.

        Returns:
            The matching gate result.

        Raises:
            KeyError: when *name* is not one of the four gate identifiers
                in :attr:`gates`.
        """
        for result in self.gates:
            if result.name == name:
                return result
        raise KeyError(f"unknown saturation gate: {name!r}")

    def blocking_gates(self) -> tuple[str, ...]:
        """Return the names of the gates that did NOT pass, in gate order.

        Returns:
            A tuple of gate names whose :attr:`SaturationGateResult.passed`
            is ``False``. Empty when every gate passed (note: an empty
            ledger still reports no blocking gates because the gates
            themselves pass trivially — :attr:`saturated` carries the
            empty-ledger short-circuit, not this list).
        """
        return tuple(result.name for result in self.gates if not result.passed)

    @classmethod
    def reduce(
        cls,
        claims: Iterable[Claim],
        open_questions: Iterable[OpenQuestion],
        *,
        now: datetime,
        novelty_window: timedelta = DEFAULT_NOVELTY_WINDOW,
        novelty_floor: int = DEFAULT_NOVELTY_FLOOR,
    ) -> SaturationReport:
        """Reduce the two ledgers to a four-gate :class:`SaturationReport`.

        Pure: the same ledgers + ``now`` always yield the same report; no
        I/O, no mutation, no raise on the read path. A failing gate is
        recorded as data on the returned report.

        The four gates are evaluated independently over the materialised
        ledgers and folded into :attr:`SaturationReport.saturated` by
        logical AND, with one short-circuit: an empty Claim ledger is
        never saturated regardless of the (trivially passing) gates,
        because a campaign that has logged no claims has accumulated no
        evidence to be dry about.

        Args:
            claims: The Claim ledger — every row, including ``SUPERSEDED``
                rows (kept for traceability, excluded from the live view).
            open_questions: The OpenQuestion ledger — every tracked
                question, terminal or not.
            now: The reference instant the novelty window is measured back
                from. Passed in (not read from the clock) so the reducer
                stays pure and the loop controls the window edge.
            novelty_window: Trailing duration a claim's ``created_at`` must
                fall within to count as "new" for gate (b). Defaults to
                :data:`DEFAULT_NOVELTY_WINDOW`.
            novelty_floor: Maximum in-window live-claim count for gate (b)
                to read as decayed. Defaults to
                :data:`DEFAULT_NOVELTY_FLOOR` (zero new claims).

        Returns:
            A :class:`SaturationReport` whose :attr:`SaturationReport.gates`
            carries the four per-gate results and whose
            :attr:`SaturationReport.saturated` is the loop-until-dry bit.
        """
        claim_list: list[Claim] = list(claims)
        question_list: list[OpenQuestion] = list(open_questions)
        live_claims = [c for c in claim_list if c.status in _LIVE_CLAIM_STATUSES]

        gate_no_open_question = _gate_no_open_question(question_list)
        gate_novelty_decay = _gate_novelty_decay(
            live_claims,
            now=now,
            novelty_window=novelty_window,
            novelty_floor=novelty_floor,
        )
        gate_no_contradiction = _gate_no_contradiction(live_claims)
        gate_integration_closed = _gate_integration_closed(live_claims, question_list)

        gates = (
            gate_no_open_question,
            gate_novelty_decay,
            gate_no_contradiction,
            gate_integration_closed,
        )
        empty_ledger = not claim_list
        all_gates_pass = all(g.passed for g in gates)
        saturated = all_gates_pass and not empty_ledger
        logger.debug(
            f"reduce claims={len(claim_list)} live={len(live_claims)} "
            f"questions={len(question_list)} saturated={saturated} "
            f"empty_ledger={empty_ledger} blocking={[g.name for g in gates if not g.passed]}"
        )
        return cls(
            saturated=saturated,
            gates=gates,
            live_claim_count=len(live_claims),
            empty_ledger=empty_ledger,
        )


def _gate_no_open_question(questions: Sequence[OpenQuestion]) -> SaturationGateResult:
    """Gate (a): no question is left in an unclosed (``OPEN`` / ``BLOCKED``) state."""
    offenders = tuple(q.id for q in questions if q.status in _UNCLOSED_QUESTION_STATUSES)
    passed = not offenders
    detail = (
        "all tracked questions terminal"
        if passed
        else f"{len(offenders)} question(s) still open/blocked"
    )
    return SaturationGateResult(
        name="no_open_question",
        passed=passed,
        offenders=offenders,
        detail=detail,
    )


def _gate_novelty_decay(
    live_claims: Sequence[Claim],
    *,
    now: datetime,
    novelty_window: timedelta,
    novelty_floor: int,
) -> SaturationGateResult:
    """Gate (b): in-window new-claim count has decayed to ``<= novelty_floor``."""
    cutoff = now - novelty_window
    in_window = tuple(c.id for c in live_claims if c.created_at >= cutoff)
    passed = len(in_window) <= novelty_floor
    detail = (
        f"{len(in_window)} new claim(s) in window <= floor {novelty_floor}"
        if passed
        else f"{len(in_window)} new claim(s) in window > floor {novelty_floor}"
    )
    # When the gate passes there is nothing to act on, so report no
    # offenders even if the window held a few claims under the floor; the
    # offenders list is the *why-still-open* set, not the in-window set.
    offenders = () if passed else in_window
    return SaturationGateResult(
        name="novelty_decay",
        passed=passed,
        offenders=offenders,
        detail=detail,
    )


def _gate_no_contradiction(live_claims: Sequence[Claim]) -> SaturationGateResult:
    """Gate (c): no live claim is ``REFUTED`` (an unresolved contradiction)."""
    offenders = tuple(c.id for c in live_claims if c.status is ClaimStatus.REFUTED)
    passed = not offenders
    detail = "no live refuted claim" if passed else f"{len(offenders)} live claim(s) refuted"
    return SaturationGateResult(
        name="no_contradiction",
        passed=passed,
        offenders=offenders,
        detail=detail,
    )


def _gate_integration_closed(
    live_claims: Sequence[Claim],
    questions: Sequence[OpenQuestion],
) -> SaturationGateResult:
    """Gate (d): claim graph has no dangling answer-edge and no evidence-less live claim.

    Two ways a live claim leaves the integration open:

    * It names an ``answers_question_id`` pointing at a question that is
      not ``ANSWERED`` (a dangling answer-edge — the claim asserts it
      closes a question the question ledger does not agree is closed).
    * It carries an empty ``evidence_refs`` list (an evidence-less live
      claim does not integrate into the closed evidence graph).
    """
    answered_question_ids = {q.id for q in questions if q.status is OpenQuestionStatus.ANSWERED}
    offenders: list[str] = []
    for claim in live_claims:
        dangling_edge = (
            claim.answers_question_id is not None
            and claim.answers_question_id not in answered_question_ids
        )
        evidence_less = not claim.evidence_refs
        if dangling_edge or evidence_less:
            offenders.append(claim.id)
    passed = not offenders
    detail = (
        "every live claim evidence-backed and answer-edges closed"
        if passed
        else f"{len(offenders)} live claim(s) dangling or evidence-less"
    )
    return SaturationGateResult(
        name="integration_closed",
        passed=passed,
        offenders=tuple(offenders),
        detail=detail,
    )


__all__ = [
    "DEFAULT_NOVELTY_FLOOR",
    "DEFAULT_NOVELTY_WINDOW",
    "SaturationGateResult",
    "SaturationReport",
]
