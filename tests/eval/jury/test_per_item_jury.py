"""Unit tests for the per-rubric-item jury reducer (P29-I08-W04).

Exercises :func:`eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`
-- the PURE reducer that lifts the holistic one-ballot-per-juror minority-veto
to one vote per rubric item per juror, then folds the per-item verdicts into a
wave-level outcome. Every test feeds CANNED :class:`PerItemJurorBallot` rows --
no live model, no spawn, no I/O -- so the reducer's properties are deterministic
and explicit:

- unanimous PASS on every item -> wave PASS;
- one juror vetoes item B2 with a refutation -> item B2 FAIL -> wave FAIL, the
  refutation surfaced;
- jurors split (pass / pass-without-refutation) on item B1 -> item B1
  NEEDS_USER -> wave NEEDS_USER (when no item outright fails);
- a fail outranks a split in the wave-level fold;
- boundary: empty rubric -> PASS; an item no juror voted on -> NEEDS_USER;
- error path: a vote on an unknown rubric item id raises ValueError.
"""

from __future__ import annotations

import pytest

from eawf.observability.eval.cross_vendor_jury import (
    PerItemJurorBallot,
    PerItemJuryResult,
    RubricItemVote,
    reduce_per_item_ballots,
)
from eawf.observability.eval.jury import JuryAggregateOutcome

_RUBRIC = ("B1", "B2", "B3")


def _ballot(juror: str, votes: dict[str, tuple[bool, str | None]]) -> PerItemJurorBallot:
    """Build a per-item ballot from an ``item_id -> (passed, refutation)`` map."""
    return PerItemJurorBallot(
        juror=juror,
        votes=tuple(
            RubricItemVote(item_id=item_id, passed=passed, refutation=refutation)
            for item_id, (passed, refutation) in votes.items()
        ),
    )


def _all_pass(juror: str, item_ids: tuple[str, ...] = _RUBRIC) -> PerItemJurorBallot:
    """A juror that passes every rubric item."""
    return _ballot(juror, dict.fromkeys(item_ids, (True, None)))


# --------------------------------------------------------------------------- #
# Unanimous PASS on all items -> wave PASS.
# --------------------------------------------------------------------------- #


def test_reduce_per_item_unanimous_pass_is_wave_pass() -> None:
    """Every juror passing every item reduces to a clean wave PASS."""
    ballots = (
        _all_pass("claude-code"),
        _all_pass("codex"),
        _all_pass("opencode"),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    assert isinstance(result, PerItemJuryResult)
    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.needs_user is False
    assert result.reasons == ()
    # One per-item verdict per rubric id, all PASS, no vetoes, no refutations.
    assert tuple(item.item_id for item in result.items) == _RUBRIC
    assert all(item.outcome is JuryAggregateOutcome.PASS for item in result.items)
    assert all(item.veto_count == 0 for item in result.items)
    assert all(item.refutations == () for item in result.items)
    assert result.failed_item_ids == ()


# --------------------------------------------------------------------------- #
# One juror vetoes B2 with a refutation -> item B2 FAIL -> wave FAIL.
# --------------------------------------------------------------------------- #


def test_reduce_per_item_one_veto_fails_only_that_item() -> None:
    """A single refuted FAIL on B2 vetoes B2 (not B1/B3) and sinks the wave."""
    refutation = "B2's egress allowlist is never consulted on the spawn path"
    ballots = (
        _all_pass("claude-code"),
        _ballot(
            "codex",
            {"B1": (True, None), "B2": (False, refutation), "B3": (True, None)},
        ),
        _all_pass("opencode"),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    # Wave-level fold: the single item fail dominates.
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.failed_item_ids == ("B2",)
    assert any("per-item veto" in reason for reason in result.reasons)
    assert any("B2" in reason for reason in result.reasons)

    by_id = {item.item_id: item for item in result.items}
    # Only B2 fails; the credible refutation killed it (minority-veto).
    assert by_id["B2"].outcome is JuryAggregateOutcome.FAIL
    assert by_id["B2"].veto_count == 1
    assert by_id["B2"].refutations == (refutation,)
    # The untouched items still pass.
    assert by_id["B1"].outcome is JuryAggregateOutcome.PASS
    assert by_id["B3"].outcome is JuryAggregateOutcome.PASS


def test_reduce_per_item_fail_without_refutation_is_not_a_veto() -> None:
    """A ``passed=False`` vote with no refutation is a split, not a veto.

    Refute-first: only a CREDIBLE refutation kills an item. A bare non-pass
    mixed with passes is an unresolved split -> the item (and the wave, absent a
    real fail) routes to NEEDS_USER, mirroring the holistic split semantics.
    """
    ballots = (
        _all_pass("claude-code"),
        _ballot(
            "codex",
            {"B1": (False, None), "B2": (True, None), "B3": (True, None)},
        ),
        _all_pass("opencode"),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    by_id = {item.item_id: item for item in result.items}
    assert by_id["B1"].outcome is JuryAggregateOutcome.NEEDS_USER
    assert by_id["B1"].veto_count == 0
    assert by_id["B1"].refutations == ()
    # No outright fail anywhere -> the split escalates the wave to NEEDS_USER.
    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.failed_item_ids == ()


# --------------------------------------------------------------------------- #
# Split on B1 -> item B1 NEEDS_USER -> wave NEEDS_USER (no item fails).
# --------------------------------------------------------------------------- #


def test_reduce_per_item_split_routes_item_and_wave_to_needs_user() -> None:
    """A pass / non-refuted-fail split on B1 surfaces NEEDS_USER per item + wave."""
    ballots = (
        _ballot(
            "claude-code",
            {"B1": (True, None), "B2": (True, None), "B3": (True, None)},
        ),
        _ballot(
            "codex",
            {"B1": (False, None), "B2": (True, None), "B3": (True, None)},
        ),
        _ballot(
            "opencode",
            {"B1": (True, None), "B2": (True, None), "B3": (True, None)},
        ),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.needs_user is True
    assert result.failed_item_ids == ()
    assert any("per-item split" in reason for reason in result.reasons)
    by_id = {item.item_id: item for item in result.items}
    assert by_id["B1"].outcome is JuryAggregateOutcome.NEEDS_USER
    assert by_id["B2"].outcome is JuryAggregateOutcome.PASS
    assert by_id["B3"].outcome is JuryAggregateOutcome.PASS


def test_reduce_per_item_fail_outranks_split_in_wave_fold() -> None:
    """When one item fails and another splits, the wave-level fold is FAIL.

    The wave-level fold mirrors the holistic escalation order: a per-item FAIL
    dominates a per-item NEEDS_USER, so a wave with both folds to FAIL.
    """
    ballots = (
        _ballot(
            "claude-code",
            {"B1": (True, None), "B2": (True, None), "B3": (True, None)},
        ),
        _ballot(
            "codex",
            {"B1": (False, None), "B2": (False, "B2 regresses the close gate"), "B3": (True, None)},
        ),
        _ballot(
            "opencode",
            {"B1": (True, None), "B2": (True, None), "B3": (True, None)},
        ),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    by_id = {item.item_id: item for item in result.items}
    assert by_id["B1"].outcome is JuryAggregateOutcome.NEEDS_USER  # split
    assert by_id["B2"].outcome is JuryAggregateOutcome.FAIL  # refuted veto
    # FAIL dominates the fold.
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.failed_item_ids == ("B2",)


def test_reduce_per_item_collects_refutations_from_all_vetoing_jurors() -> None:
    """Every refutation on a failed item is surfaced, in juror order."""
    ballots = (
        _ballot("claude-code", {"B1": (False, "claude: B1 leaks the token")}),
        _ballot("codex", {"B1": (True, None)}),
        _ballot("opencode", {"B1": (False, "opencode: B1 skips the scrub")}),
    )

    result = reduce_per_item_ballots(ballots, ("B1",))

    assert result.outcome is JuryAggregateOutcome.FAIL
    (item,) = result.items
    assert item.veto_count == 2
    assert item.refutations == (
        "claude: B1 leaks the token",
        "opencode: B1 skips the scrub",
    )


# --------------------------------------------------------------------------- #
# Boundary cases.
# --------------------------------------------------------------------------- #


def test_reduce_per_item_empty_rubric_is_pass() -> None:
    """Boundary: no jury-scorable items -> a clean wave PASS, no per-item rows.

    A wave with nothing to score has nothing to veto, so the safe boundary is
    PASS rather than NEEDS_USER.
    """
    result = reduce_per_item_ballots((), ())

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.items == ()
    assert result.reasons == ()
    assert result.needs_user is False


def test_reduce_per_item_empty_ballots_with_rubric_needs_user() -> None:
    """Boundary: a rubric but zero ballots -> every item is unresolved.

    No votes is an unresolved item, never a silent pass -- so each item (and the
    wave) routes to NEEDS_USER.
    """
    result = reduce_per_item_ballots((), _RUBRIC)

    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert tuple(item.item_id for item in result.items) == _RUBRIC
    assert all(item.outcome is JuryAggregateOutcome.NEEDS_USER for item in result.items)


def test_reduce_per_item_unvoted_item_routes_to_needs_user() -> None:
    """An item present in the rubric but voted on by no juror -> NEEDS_USER.

    The other items still reduce normally; only the unscored item is unresolved.
    """
    ballots = (
        _ballot("claude-code", {"B1": (True, None), "B2": (True, None)}),
        _ballot("codex", {"B1": (True, None), "B2": (True, None)}),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    by_id = {item.item_id: item for item in result.items}
    assert by_id["B1"].outcome is JuryAggregateOutcome.PASS
    assert by_id["B2"].outcome is JuryAggregateOutcome.PASS
    # B3 got zero votes -> unresolved.
    assert by_id["B3"].outcome is JuryAggregateOutcome.NEEDS_USER
    assert any("no juror voted" in reason for reason in by_id["B3"].reasons)
    # No fail anywhere, but an unresolved item escalates the wave.
    assert result.outcome is JuryAggregateOutcome.NEEDS_USER


def test_reduce_per_item_single_juror_resolves_each_item() -> None:
    """Boundary: a lone juror's ballot resolves every item on its own votes."""
    ballots = (
        _ballot(
            "claude-code",
            {"B1": (True, None), "B2": (False, "B2 unimplemented"), "B3": (True, None)},
        ),
    )

    result = reduce_per_item_ballots(ballots, _RUBRIC)

    by_id = {item.item_id: item for item in result.items}
    assert by_id["B1"].outcome is JuryAggregateOutcome.PASS
    assert by_id["B2"].outcome is JuryAggregateOutcome.FAIL
    assert by_id["B3"].outcome is JuryAggregateOutcome.PASS
    assert result.outcome is JuryAggregateOutcome.FAIL


# --------------------------------------------------------------------------- #
# Error path + model surface.
# --------------------------------------------------------------------------- #


def test_reduce_per_item_unknown_item_id_raises() -> None:
    """Error path: a vote on an item id absent from the rubric raises ValueError."""
    ballots = (_ballot("claude-code", {"B1": (True, None), "B99": (True, None)}),)

    with pytest.raises(ValueError, match="unknown rubric item: 'B99'"):
        reduce_per_item_ballots(ballots, _RUBRIC)


def test_reduce_per_item_unknown_item_id_names_the_juror() -> None:
    """The unknown-item ValueError names the offending juror for triage."""
    ballots = (
        _all_pass("claude-code"),
        _ballot("codex", {"BX": (True, None)}),
    )

    with pytest.raises(ValueError, match="juror 'codex'"):
        reduce_per_item_ballots(ballots, _RUBRIC)


def test_rubric_item_vote_rejects_extra_keys() -> None:
    """Error path: an unknown vote key fails the extra='forbid' guard."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RubricItemVote(item_id="B1", passed=True, weight=2.0)  # type: ignore[call-arg]


def test_per_item_juror_ballot_rejects_extra_keys() -> None:
    """Error path: an unknown ballot key fails the extra='forbid' guard."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PerItemJurorBallot(juror="claude-code", votes=(), surprise=True)  # type: ignore[call-arg]


def test_per_item_jury_result_rejects_extra_keys() -> None:
    """Error path: an unknown result key fails the extra='forbid' guard."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PerItemJuryResult(outcome=JuryAggregateOutcome.PASS, extra=1)  # type: ignore[call-arg]
