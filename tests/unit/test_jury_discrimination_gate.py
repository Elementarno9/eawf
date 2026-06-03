"""Unit tests for ``tools/jury_discrimination_gate.py``.

Covers the deterministic canned-ballot gate that proves the cross-vendor
jury's per-item reducer DISCRIMINATES -- without verifying a verifier by
running another jury over it (the meta-circularity the gate exists to avoid):

- the gate PASSES on the REAL reducer: the faithful frame reduces to ``PASS``,
  the planted-violation frame to ``FAIL`` citing the violated item, and the
  hard near-miss frame to ``FAIL`` citing its offending item;
- the violation FAIL surfaces the failed-item id AND a non-empty refutation,
  not a bare ``FAIL``;
- the NEGATIVE control: a rubber-stamp stub reducer (always returns a clean
  ``PASS``) makes the gate FAIL, proving the gate catches a non-discriminating
  jury;
- boundary: an empty-rubric faithful frame reduces to ``PASS`` through the real
  reducer (a wave with nothing to score has nothing to veto).

The gate module is loaded via :mod:`importlib` because ``tools/`` is excluded
from the package and so is not importable by name. The reducer is injectable
(``reduce_fn``) so the rubber-stamp control never touches the real reducer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from eawf.observability.eval.cross_vendor_jury import (
    PerItemJurorBallot,
    PerItemJuryResult,
    PerItemVerdict,
    RubricItemVote,
    reduce_per_item_ballots,
)
from eawf.observability.eval.jury import JuryAggregateOutcome

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "jury_discrimination_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("jury_discrimination_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["jury_discrimination_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


# --------------------------------------------------------------------------- #
# Stub reducers for the negative controls (no real-reducer dependency).
# --------------------------------------------------------------------------- #


def _rubber_stamp_reduce(
    ballots: tuple[PerItemJurorBallot, ...],
    rubric_item_ids: tuple[str, ...],
) -> PerItemJuryResult:
    """A non-discriminating reducer: always a clean ``PASS``, ignoring ballots.

    Models the B091-style regression where the gate machinery degrades into a
    rubber stamp. Every item is reported ``PASS`` regardless of any veto, so a
    discriminating gate must reject it.
    """
    del ballots
    items = tuple(
        PerItemVerdict(item_id=item_id, outcome=JuryAggregateOutcome.PASS, veto_count=0)
        for item_id in rubric_item_ids
    )
    return PerItemJuryResult(outcome=JuryAggregateOutcome.PASS, items=items)


def _strip_refutations_reduce(
    ballots: tuple[PerItemJurorBallot, ...],
    rubric_item_ids: tuple[str, ...],
) -> PerItemJuryResult:
    """A reducer that fails items but drops every refutation.

    Reduces through the real reducer for the outcome, then rebuilds the result
    with empty ``refutations`` on every item. The wave still folds to ``FAIL``
    on a veto, but the FAIL no longer CITES why -- so the gate's "must cite a
    non-empty refutation" contract must reject it.
    """
    real = reduce_per_item_ballots(ballots, rubric_item_ids)
    items = tuple(
        PerItemVerdict(
            item_id=item.item_id,
            outcome=item.outcome,
            veto_count=item.veto_count,
            refutations=(),
            reasons=item.reasons,
        )
        for item in real.items
    )
    return PerItemJuryResult(outcome=real.outcome, items=items, reasons=real.reasons)


# --------------------------------------------------------------------------- #
# Pass path -- the real reducer discriminates.
# --------------------------------------------------------------------------- #


def test_gate_passes_on_real_reducer(mod) -> None:
    # Default arg binds the real reduce_per_item_ballots: faithful -> PASS,
    # violation -> FAIL+cite, near-miss -> FAIL+cite.
    result = mod.check_jury_discrimination()
    assert result.passed is True
    assert result.failure is None


def test_gate_message_names_cited_items_on_pass(mod) -> None:
    # The success message reports which items the violation + near-miss frames
    # cited, so a green run is legible.
    result = mod.check_jury_discrimination()
    assert mod._VIOLATED_ITEM in result.message
    assert mod._NEAR_MISS_ITEM in result.message


# --------------------------------------------------------------------------- #
# The three canned frames reduce as the doctrine requires (real reducer).
# --------------------------------------------------------------------------- #


def test_faithful_frame_reduces_to_pass(mod) -> None:
    result = reduce_per_item_ballots(mod._faithful_ballots(), mod._RUBRIC)
    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.failed_item_ids == ()


def test_violation_frame_fails_and_cites_item_and_refutation(mod) -> None:
    # The planted-violation frame must FAIL and surface the failed-item id +
    # the non-empty refutation text -- not a bare FAIL.
    result = reduce_per_item_ballots(mod._violation_ballots(), mod._RUBRIC)
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert mod._VIOLATED_ITEM in result.failed_item_ids
    vetoed = next(item for item in result.items if item.item_id == mod._VIOLATED_ITEM)
    assert vetoed.refutations
    assert any(text.strip() for text in vetoed.refutations)
    # The other rubric items in the frame are NOT failed -- the discrimination
    # is on the single planted item, not a blanket fail.
    other_failed = [fid for fid in result.failed_item_ids if fid != mod._VIOLATED_ITEM]
    assert other_failed == []


def test_near_miss_frame_fails_on_single_item_veto(mod) -> None:
    # Mostly-passing rubric with exactly one veto must still FAIL and cite the
    # one offending item -- the subtle de-link regression.
    result = reduce_per_item_ballots(mod._near_miss_ballots(), mod._NEAR_MISS_RUBRIC)
    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.failed_item_ids == (mod._NEAR_MISS_ITEM,)
    vetoed = next(item for item in result.items if item.item_id == mod._NEAR_MISS_ITEM)
    assert any(text.strip() for text in vetoed.refutations)


def test_near_miss_frame_is_mostly_passing(mod) -> None:
    # The frame must genuinely be "mostly passing" (more than half its items
    # clear) so it models a near-miss, not a stark all-fail.
    result = reduce_per_item_ballots(mod._near_miss_ballots(), mod._NEAR_MISS_RUBRIC)
    passed = [item for item in result.items if item.outcome is JuryAggregateOutcome.PASS]
    assert len(passed) > len(result.items) / 2


# --------------------------------------------------------------------------- #
# Negative control -- a rubber-stamp reducer makes the gate FAIL.
# --------------------------------------------------------------------------- #


def test_gate_fails_on_rubber_stamp_reducer(mod) -> None:
    # The core proof: inject a reducer that always returns PASS. The gate must
    # catch that the violation frame no longer fails -> non-discriminating jury.
    result = mod.check_jury_discrimination(reduce_fn=_rubber_stamp_reduce)
    assert result.passed is False
    assert result.failure is mod.GateFailure.VIOLATION_NOT_FAIL
    assert "rubber-stamp" in result.message


def test_gate_fails_when_violation_fails_but_drops_refutation(mod) -> None:
    # A reducer that folds to FAIL but cites no refutation is still a weak gate:
    # a bare FAIL does not satisfy "name which item failed and why".
    result = mod.check_jury_discrimination(reduce_fn=_strip_refutations_reduce)
    assert result.passed is False
    assert result.failure is mod.GateFailure.VIOLATION_NOT_CITED


def test_gate_fails_when_faithful_frame_is_vetoed(mod) -> None:
    # A reducer that sinks even the clean frame is the most fundamental break;
    # precedence reports it first.
    def _always_fail(
        ballots: tuple[PerItemJurorBallot, ...],
        rubric_item_ids: tuple[str, ...],
    ) -> PerItemJuryResult:
        del ballots
        items = tuple(
            PerItemVerdict(item_id=item_id, outcome=JuryAggregateOutcome.FAIL, veto_count=1)
            for item_id in rubric_item_ids
        )
        return PerItemJuryResult(outcome=JuryAggregateOutcome.FAIL, items=items)

    result = mod.check_jury_discrimination(reduce_fn=_always_fail)
    assert result.passed is False
    assert result.failure is mod.GateFailure.FAITHFUL_NOT_PASS


# --------------------------------------------------------------------------- #
# Boundary -- empty rubric faithful frame.
# --------------------------------------------------------------------------- #


def test_empty_rubric_faithful_frame_passes_through_real_reducer(mod) -> None:
    # Boundary: an empty rubric (no jury-scorable items) reduces to a clean
    # PASS -- a wave with nothing to score has nothing to veto. This pins the
    # faithful-frame contract at the degenerate edge the reducer documents.
    result = reduce_per_item_ballots((), ())
    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.items == ()
    assert result.failed_item_ids == ()


def test_empty_rubric_single_juror_no_votes_passes(mod) -> None:
    # A juror that casts no votes over an empty rubric is still a clean PASS --
    # the off-rubric-vote guard is not tripped (no votes to be off-rubric).
    ballot = PerItemJurorBallot(juror="claude-code", votes=())
    result = reduce_per_item_ballots((ballot,), ())
    assert result.outcome is JuryAggregateOutcome.PASS


# --------------------------------------------------------------------------- #
# CLI wrapper.
# --------------------------------------------------------------------------- #


def test_cli_returns_zero_on_pass(mod) -> None:
    code = mod.main(["jury_discrimination_gate.py"])
    assert code == 0


def test_cli_returns_one_on_failure(mod, monkeypatch) -> None:
    # Force a failed result by monkeypatching the module-level reducer default
    # is not possible (it is a parameter default), so patch the check to run the
    # rubber-stamp reducer and confirm the CLI maps a failed result onto exit 1.
    real_check = mod.check_jury_discrimination
    monkeypatch.setattr(
        mod,
        "check_jury_discrimination",
        lambda: real_check(reduce_fn=_rubber_stamp_reduce),
    )
    code = mod.main(["jury_discrimination_gate.py"])
    assert code == 1


# --------------------------------------------------------------------------- #
# Frame-construction sanity -- the fixtures are well-formed canned ballots.
# --------------------------------------------------------------------------- #


def test_frames_build_valid_ballots(mod) -> None:
    # Every frame is a tuple of validated PerItemJurorBallot with one vote per
    # rubric item per juror -- a malformed fixture would otherwise mask a
    # reducer regression behind a construction error.
    for ballots, rubric in (
        (mod._faithful_ballots(), mod._RUBRIC),
        (mod._violation_ballots(), mod._RUBRIC),
        (mod._near_miss_ballots(), mod._NEAR_MISS_RUBRIC),
    ):
        assert len(ballots) == 3
        for ballot in ballots:
            assert isinstance(ballot, PerItemJurorBallot)
            assert tuple(vote.item_id for vote in ballot.votes) == rubric
            for vote in ballot.votes:
                assert isinstance(vote, RubricItemVote)


def test_violation_frame_has_exactly_one_veto(mod) -> None:
    # Exactly one juror, one item carries the planted veto -- the rest pass.
    ballots = mod._violation_ballots()
    vetoes = [
        (ballot.juror, vote.item_id)
        for ballot in ballots
        for vote in ballot.votes
        if not vote.passed
    ]
    assert len(vetoes) == 1
    assert vetoes[0][1] == mod._VIOLATED_ITEM
