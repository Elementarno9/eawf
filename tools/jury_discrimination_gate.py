"""Deterministic canned-ballot jury discrimination gate.

The cross-vendor jury exists to DISCRIMINATE: a faithful surface must clear,
a surface that violates a rubric item must be vetoed, and -- the hard case --
a surface that mostly passes but carries one credible refutation on a single
rubric item must still fail. The trap this repo keeps falling into is
verifying a verifier by running ANOTHER verifier over it (a jury certifying a
jury), which is meta-circular: the second jury can share the first's blind
spot, so the chain never bottoms out in something un-gameable.

This gate breaks the circularity. The jury's discrimination is proven by a
purely DETERMINISTIC canned-ballot probe, never by another jury. The gate
builds three in-process ballot fixtures -- a faithful frame, a planted-
violation frame, and a hard near-miss frame -- feeds each through the pure
per-item reducer
(:func:`eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`),
and asserts the reducer discriminates across all three:

- **faithful frame** -- every juror passes every rubric item -> wave ``PASS``.
- **planted-violation frame** -- one juror casts a credible-refutation veto on
  one rubric item -> wave ``FAIL`` that CITES the violated item id AND surfaces
  the non-empty refutation text (a bare ``FAIL`` with no citation does not
  satisfy the contract).
- **hard near-miss frame** -- the subtle de-link regression: a surface that
  LOOKS right (most rubric items pass) but whose behaviour check still resolves,
  modelled as a mostly-passing frame where exactly ONE rubric item carries a
  juror veto. It must still resolve to wave ``FAIL`` and cite the offending item
  -- discrimination on a non-stark case, not just an all-fail one.

A future regression that makes the reducer rubber-stamp (return ``PASS``
regardless of the ballots) is exactly what this gate catches: the violation
and near-miss frames would no longer fail, so the gate fails CI.

The reducer is injectable: :func:`check_jury_discrimination` takes the reducer
as a parameter defaulting to the real
:func:`~eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`, so
the negative-control test can pass a rubber-stamp stub and confirm the gate
catches a non-discriminating jury. The check is pure -- it builds canned
:class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot` objects
in-process, never spawns a live model, never mutates state, never writes a
file. :func:`check_jury_discrimination` returns a typed :class:`GateResult`
and the thin :func:`main` CLI maps it onto an exit code.

Invocation:

    python3 tools/jury_discrimination_gate.py

Exit codes:
- ``0`` -- the reducer discriminates: faithful -> PASS, violation -> FAIL+cite,
  near-miss -> FAIL+cite.
- ``1`` -- discrimination did not hold (the failure is named on stderr).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from eawf.observability.eval.cross_vendor_jury import (
    PerItemJurorBallot,
    PerItemJuryResult,
    RubricItemVote,
    reduce_per_item_ballots,
)
from eawf.observability.eval.jury import JuryAggregateOutcome

#: A reducer with the shape of
#: :func:`eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`.
#: Injected so the rubber-stamp failure mode is testable with a stub reducer
#: that always returns a clean ``PASS`` regardless of the ballots.
type ReduceFn = Callable[
    [tuple[PerItemJurorBallot, ...], tuple[str, ...]],
    PerItemJuryResult,
]

#: The three disjoint juror ids the canned frames vote with. They mirror the
#: real :data:`~eawf.observability.eval.cross_vendor_jury.JURY_RUNTIME_FAMILIES`
#: so the fixtures read like a genuine cross-vendor ballot, but the values are
#: load-bearing only as distinct labels -- the reducer is vendor-agnostic.
_JURORS: tuple[str, str, str] = ("claude-code", "codex", "opencode")

#: The canned rubric the faithful + violation frames score. Three behaviour ids
#: so the violation frame can fail exactly one while the other two pass.
_RUBRIC: tuple[str, str, str] = ("B1", "B2", "B3")

#: The rubric item the planted-violation frame vetoes.
_VIOLATED_ITEM = "B2"

#: The refutation text the planted-violation veto carries. The gate reads it
#: back to prove the FAIL cites WHY the item failed, not merely THAT it did.
_VIOLATION_REFUTATION = (
    "planted violation: rubric item B2 requires the surface to render as plain "
    "text, but the de-linked path still resolves the behaviour check"
)

#: The rubric the hard near-miss frame scores. A longer rubric so "mostly
#: passing" is unambiguous -- four items pass and exactly one is vetoed, the
#: subtle single-item regression rather than a stark all-fail.
_NEAR_MISS_RUBRIC: tuple[str, ...] = ("B1", "B2", "B3", "B4", "B5")

#: The single near-miss item that carries the veto. The other four pass, so the
#: frame LOOKS right -- the discrimination test is whether one credible
#: refutation on one item still sinks an otherwise-passing wave.
_NEAR_MISS_ITEM = "B4"

#: The refutation text the near-miss veto carries -- the subtle de-link the
#: frame models (a surface that renders right but whose check resolves).
_NEAR_MISS_REFUTATION = (
    "hard near-miss: rubric item B4's surface looks correct (renders as plain "
    "text) but its behaviour check still resolves -- the de-link regressed"
)


class GateFailure(StrEnum):
    """The mutually exclusive ways the jury discrimination gate can fail.

    The order encodes precedence: a faithful frame that does NOT pass is the
    most fundamental break (the reducer vetoes a clean surface), reported
    before the under-veto failures where the reducer rubber-stamps a bad one.
    """

    FAITHFUL_NOT_PASS = "faithful_not_pass"
    VIOLATION_NOT_FAIL = "violation_not_fail"
    VIOLATION_NOT_CITED = "violation_not_cited"
    NEAR_MISS_NOT_FAIL = "near_miss_not_fail"
    NEAR_MISS_NOT_CITED = "near_miss_not_cited"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one jury discrimination check.

    Attributes:
        passed: Whether the reducer discriminated across all three frames
            (faithful -> PASS, violation -> FAIL+cite, near-miss -> FAIL+cite).
        failure: The failure kind when ``passed`` is ``False``; ``None`` on a
            pass.
        message: A human-readable line; on failure it names the violated
            contract and the offending frame.
    """

    passed: bool
    failure: GateFailure | None
    message: str


def _faithful_ballots() -> tuple[PerItemJurorBallot, ...]:
    """Build the faithful frame: every juror passes every rubric item.

    Returns:
        One :class:`PerItemJurorBallot` per juror, each voting ``passed=True``
        on every id in :data:`_RUBRIC` -- a clean unanimous all-pass.
    """
    return tuple(
        PerItemJurorBallot(
            juror=juror,
            votes=tuple(RubricItemVote(item_id=item_id, passed=True) for item_id in _RUBRIC),
        )
        for juror in _JURORS
    )


def _violation_ballots() -> tuple[PerItemJurorBallot, ...]:
    """Build the planted-violation frame: one juror vetoes one rubric item.

    The first juror casts a ``passed=False`` vote carrying
    :data:`_VIOLATION_REFUTATION` on :data:`_VIOLATED_ITEM`; every other vote
    (that juror's other items, and both other jurors' whole ballots) passes. A
    single credible refutation is a minority-veto, so the reducer must sink the
    item -- and therefore the wave -- to ``FAIL`` and surface the refutation.

    Returns:
        One :class:`PerItemJurorBallot` per juror, exactly one carrying the
        planted veto.
    """
    ballots: list[PerItemJurorBallot] = []
    for index, juror in enumerate(_JURORS):
        votes: list[RubricItemVote] = []
        for item_id in _RUBRIC:
            if index == 0 and item_id == _VIOLATED_ITEM:
                votes.append(
                    RubricItemVote(
                        item_id=item_id,
                        passed=False,
                        refutation=_VIOLATION_REFUTATION,
                    )
                )
            else:
                votes.append(RubricItemVote(item_id=item_id, passed=True))
        ballots.append(PerItemJurorBallot(juror=juror, votes=tuple(votes)))
    return tuple(ballots)


def _near_miss_ballots() -> tuple[PerItemJurorBallot, ...]:
    """Build the hard near-miss frame: mostly passing, one credible veto.

    Models the subtle de-link regression. Across :data:`_NEAR_MISS_RUBRIC`
    (five items) every vote passes EXCEPT one juror's veto on
    :data:`_NEAR_MISS_ITEM`, carrying :data:`_NEAR_MISS_REFUTATION`. The
    surface looks right -- four of five items clear -- so a non-discriminating
    reducer would wave it through; a discriminating one still fails on the one
    credible refutation.

    Returns:
        One :class:`PerItemJurorBallot` per juror, exactly one carrying the
        single-item near-miss veto.
    """
    ballots: list[PerItemJurorBallot] = []
    for index, juror in enumerate(_JURORS):
        votes: list[RubricItemVote] = []
        for item_id in _NEAR_MISS_RUBRIC:
            if index == 1 and item_id == _NEAR_MISS_ITEM:
                votes.append(
                    RubricItemVote(
                        item_id=item_id,
                        passed=False,
                        refutation=_NEAR_MISS_REFUTATION,
                    )
                )
            else:
                votes.append(RubricItemVote(item_id=item_id, passed=True))
        ballots.append(PerItemJurorBallot(juror=juror, votes=tuple(votes)))
    return tuple(ballots)


def _cites_item(result: PerItemJuryResult, *, item_id: str) -> bool:
    """Return whether *result* cites *item_id* as failed with a refutation.

    A FAIL that satisfies the discrimination contract must do more than fold
    to ``FAIL``: it must name WHICH rubric item failed (the item id appears in
    :attr:`~eawf.observability.eval.cross_vendor_jury.PerItemJuryResult.failed_item_ids`)
    AND surface a non-empty refutation on that item's verdict. A bare FAIL with
    no cited item or an empty refutation does not.

    Args:
        result: The reduced per-item jury result to inspect.
        item_id: The rubric item id the frame planted a veto on.

    Returns:
        ``True`` when *item_id* is among the failed item ids and its per-item
        verdict carries at least one non-empty refutation.
    """
    if item_id not in result.failed_item_ids:
        return False
    for item in result.items:
        if item.item_id == item_id:
            return any(text.strip() for text in item.refutations)
    return False


def check_jury_discrimination(
    *,
    reduce_fn: ReduceFn = reduce_per_item_ballots,
) -> GateResult:
    """Assert the per-item reducer discriminates across three canned frames.

    The three contracts are checked in precedence order:

    1. **faithful -> PASS** -- the all-pass frame (:func:`_faithful_ballots`)
       must reduce to wave
       :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.PASS`. A
       reducer that vetoes a clean surface fails
       :attr:`GateFailure.FAITHFUL_NOT_PASS`.
    2. **violation -> FAIL + cite** -- the planted-violation frame
       (:func:`_violation_ballots`) must reduce to wave ``FAIL``
       (:attr:`GateFailure.VIOLATION_NOT_FAIL` otherwise) AND cite the violated
       item id with a non-empty refutation
       (:attr:`GateFailure.VIOLATION_NOT_CITED` otherwise).
    3. **near-miss -> FAIL + cite** -- the hard near-miss frame
       (:func:`_near_miss_ballots`) must likewise reduce to wave ``FAIL``
       (:attr:`GateFailure.NEAR_MISS_NOT_FAIL` otherwise) and cite the
       offending item with a refutation
       (:attr:`GateFailure.NEAR_MISS_NOT_CITED` otherwise) -- discrimination on
       a mostly-passing case, not just a stark one.

    A reducer that rubber-stamps every frame to ``PASS`` (the regression this
    gate guards) fails at step 2 because the violation frame would no longer
    reach ``FAIL``.

    Args:
        reduce_fn: The per-item reducer under test. Defaults to
            :func:`eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`;
            the negative-control test injects a rubber-stamp stub to prove the
            gate catches a non-discriminating jury.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the
        reducer discriminates across all three frames; otherwise ``failure``
        names the first violated contract.
    """
    faithful = reduce_fn(_faithful_ballots(), _RUBRIC)
    if faithful.outcome is not JuryAggregateOutcome.PASS:
        return GateResult(
            passed=False,
            failure=GateFailure.FAITHFUL_NOT_PASS,
            message=(
                "jury discrimination broken: the faithful frame (every juror passes "
                f"every rubric item) reduced to {faithful.outcome.value!r}, expected "
                "'pass' -- the reducer vetoes a clean surface"
            ),
        )

    violation = reduce_fn(_violation_ballots(), _RUBRIC)
    if violation.outcome is not JuryAggregateOutcome.FAIL:
        return GateResult(
            passed=False,
            failure=GateFailure.VIOLATION_NOT_FAIL,
            message=(
                "jury discrimination broken: the planted-violation frame (a credible "
                f"refutation veto on item {_VIOLATED_ITEM!r}) reduced to "
                f"{violation.outcome.value!r}, expected 'fail' -- the reducer "
                "rubber-stamps a rubric violation"
            ),
        )
    if not _cites_item(violation, item_id=_VIOLATED_ITEM):
        return GateResult(
            passed=False,
            failure=GateFailure.VIOLATION_NOT_CITED,
            message=(
                "jury discrimination weak: the planted-violation frame failed but did "
                f"not cite item {_VIOLATED_ITEM!r} with a non-empty refutation "
                f"(failed_item_ids={violation.failed_item_ids}) -- a FAIL must name "
                "which rubric item failed and why"
            ),
        )

    near_miss = reduce_fn(_near_miss_ballots(), _NEAR_MISS_RUBRIC)
    if near_miss.outcome is not JuryAggregateOutcome.FAIL:
        return GateResult(
            passed=False,
            failure=GateFailure.NEAR_MISS_NOT_FAIL,
            message=(
                "jury discrimination broken: the hard near-miss frame (mostly passing, "
                f"one credible veto on item {_NEAR_MISS_ITEM!r}) reduced to "
                f"{near_miss.outcome.value!r}, expected 'fail' -- one refutation on a "
                "mostly-passing surface must still sink the wave"
            ),
        )
    if not _cites_item(near_miss, item_id=_NEAR_MISS_ITEM):
        return GateResult(
            passed=False,
            failure=GateFailure.NEAR_MISS_NOT_CITED,
            message=(
                "jury discrimination weak: the hard near-miss frame failed but did not "
                f"cite item {_NEAR_MISS_ITEM!r} with a non-empty refutation "
                f"(failed_item_ids={near_miss.failed_item_ids}) -- a near-miss FAIL "
                "must still name the offending rubric item"
            ),
        )

    return GateResult(
        passed=True,
        failure=None,
        message=(
            "jury-discrimination gate: ok (faithful frame -> pass; planted-violation "
            f"frame -> fail citing {_VIOLATED_ITEM!r}; hard near-miss frame -> fail "
            f"citing {_NEAR_MISS_ITEM!r})"
        ),
    )


def main(argv: list[str]) -> int:
    del argv  # the gate reads no arguments; the canned frames are the input
    result = check_jury_discrimination()
    if result.passed:
        print(result.message)
        return 0
    print(result.message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
