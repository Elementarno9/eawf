"""The bulk-operation record: per-item ledgers, their legal moves and the confirmation."""

from __future__ import annotations

from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.bulk import (
    DISPOSITION_STATES,
    INVALIDATION_RULE,
    ITEM_EDGES,
    SETTLED_ITEM_STATES,
    BulkConfirmation,
    BulkItemResult,
    BulkItemState,
    BulkOperation,
    BulkVerb,
    advance,
    aggregate_of,
    canonical_items,
    confirm_bulk,
)
from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.runtime.control import PROJECTED_DISPOSITIONS, ControlDisposition

_RUNS: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run"
DIGEST: Final = f"sha256:{'0' * 64}"


def run(n: int) -> str:
    """Return the URN of the *n*-th Run."""
    return f"{_RUNS}/RUN-{n:08d}"


def result(state: BulkItemState) -> BulkItemResult:
    """Return one item result in *state*, carrying a code when it must."""
    code = "superseded" if state is BulkItemState.REJECTED else None
    return BulkItemResult(state=state, code=code)


def operation(states: list[BulkItemState], **overrides: Any) -> BulkOperation:
    """Return an operation whose items stand in *states*, one Run each."""
    items = [run(n) for n in range(1, len(states) + 1)]
    results = {urn: result(state) for urn, state in zip(items, states, strict=True)}
    fields: dict[str, Any] = {
        "verb": "cancel",
        "item_refs": items,
        "expected_revisions": dict.fromkeys(items, 1),
        "item_results": results,
        "aggregate": aggregate_of(results.values()),
        "idempotency_key": "bulk-1",
        "actor": "OP-0001",
        "confirmation_digest": DIGEST,
    }
    return BulkOperation.model_validate({**fields, **overrides})


# ---- DEL-021: per-item authorization over a canonical item set ---------------


def test_del_021_canonical_items_orders_by_urn_whatever_the_selection_order() -> None:
    refs = [parse_qualified_urn(run(n)) for n in (3, 1, 2)]

    assert [str(ref) for ref in canonical_items(refs)] == [run(1), run(2), run(3)]


def test_del_021_canonical_items_of_one_and_of_none() -> None:
    assert canonical_items([]) == ()
    assert [str(ref) for ref in canonical_items([parse_qualified_urn(run(1))])] == [run(1)]


def test_del_021_an_item_named_twice_is_refused() -> None:
    ref = parse_qualified_urn(run(1))

    with pytest.raises(ValueError, match="more than once"):
        canonical_items([ref, ref])


def test_del_021_a_rejected_item_must_say_why() -> None:
    with pytest.raises(ValidationError, match="code it was refused with"):
        BulkItemResult(state=BulkItemState.REJECTED)


def test_del_021_an_operation_needs_a_result_and_an_anchor_per_item() -> None:
    with pytest.raises(ValidationError, match="item_results must name exactly"):
        operation([BulkItemState.ACCEPTED], item_results={})
    with pytest.raises(ValidationError, match="expected_revisions must name exactly"):
        operation([BulkItemState.ACCEPTED], expected_revisions={run(9): 1})


def test_del_021_an_operation_lists_its_items_canonically() -> None:
    with pytest.raises(ValidationError, match="canonical order"):
        operation([BulkItemState.ACCEPTED, BulkItemState.ACCEPTED], item_refs=[run(2), run(1)])


def test_del_021_an_operation_names_at_least_one_item() -> None:
    with pytest.raises(ValidationError, match="item_refs"):
        operation([], item_refs=[])


def test_del_021_integration_and_merge_are_never_bulk_verbs() -> None:
    assert {verb.value for verb in BulkVerb} == {"cancel", "interrupt", "retry"}
    with pytest.raises(ValidationError, match="verb"):
        operation([BulkItemState.ACCEPTED], verb="merge")


# ---- DEL-022: partial outcome is first-class ---------------------------------


def test_del_022_twenty_four_confirmed_two_unknown_one_rejected_is_a_normal_result() -> None:
    states = [BulkItemState.CONFIRMED] * 24 + [BulkItemState.UNKNOWN] * 2 + [BulkItemState.REJECTED]

    record = operation(states)

    assert record.aggregate[BulkItemState.CONFIRMED] == 24
    assert record.aggregate[BulkItemState.UNKNOWN] == 2
    assert record.aggregate[BulkItemState.REJECTED] == 1
    assert "success" not in BulkOperation.model_fields


def test_del_022_the_aggregate_lists_every_state_zero_included() -> None:
    assert aggregate_of([]) == dict.fromkeys(BulkItemState, 0)


def test_del_022_an_aggregate_that_miscounts_is_refused() -> None:
    wrong = {**aggregate_of([result(BulkItemState.ACCEPTED)]), BulkItemState.CONFIRMED: 1}

    with pytest.raises(ValidationError, match="aggregate must count"):
        operation([BulkItemState.ACCEPTED], aggregate=wrong)


# ---- DEL-023: an unknown item stays unknown until reconciled -----------------


def test_del_023_only_an_observation_moves_an_item() -> None:
    for state in BulkItemState:
        assert advance(state, state) is state


def test_del_023_unknown_leaves_only_for_an_observed_outcome() -> None:
    assert ITEM_EDGES[BulkItemState.UNKNOWN] == {
        BulkItemState.ACCEPTED,
        BulkItemState.CONFIRMED,
        BulkItemState.INVALIDATED,
    }
    assert advance(BulkItemState.UNKNOWN, BulkItemState.CONFIRMED) is BulkItemState.CONFIRMED


def test_del_023_a_refusal_found_while_reconciling_unknown_reads_as_invalidated() -> None:
    assert advance(BulkItemState.UNKNOWN, BulkItemState.REJECTED) is BulkItemState.INVALIDATED


@pytest.mark.parametrize("settled", sorted(SETTLED_ITEM_STATES))
def test_del_023_a_settled_item_is_never_rewritten(settled: BulkItemState) -> None:
    other = next(state for state in BulkItemState if state is not settled)

    with pytest.raises(ValueError, match="cannot become"):
        advance(settled, other)


def test_del_023_an_accepted_item_is_not_rejected_after_the_fact() -> None:
    with pytest.raises(ValueError, match="cannot become"):
        advance(BulkItemState.ACCEPTED, BulkItemState.REJECTED)


def test_del_023_every_persisted_disposition_reads_as_an_item_state() -> None:
    persisted = set(ControlDisposition) - PROJECTED_DISPOSITIONS

    assert set(DISPOSITION_STATES) == persisted
    assert DISPOSITION_STATES[ControlDisposition.RECOVERY] is BulkItemState.UNKNOWN


# ---- DEL-024: the confirmation names count, effects, non-effects and the rule --


def test_del_024_every_verb_confirms_its_non_effects_and_the_invalidation_rule() -> None:
    refs = [parse_qualified_urn(run(n)) for n in (2, 1)]

    for verb in BulkVerb:
        shown = confirm_bulk(verb, refs)
        assert shown.target_count == 2
        assert [str(ref) for ref in shown.item_refs] == [run(1), run(2)]
        assert shown.effects
        assert any("Tasks" in line for line in shown.non_effects)
        assert shown.invalidation_rule == INVALIDATION_RULE


def test_del_024_cancel_says_it_does_not_release_the_tasks() -> None:
    shown = confirm_bulk(BulkVerb.CANCEL, [parse_qualified_urn(run(1))])

    assert any("does not release the Runs' Tasks" in line for line in shown.non_effects)


def test_del_024_the_digest_binds_the_items_and_the_verb() -> None:
    one = [parse_qualified_urn(run(1))]
    two = [*one, parse_qualified_urn(run(2))]

    assert (
        confirm_bulk(BulkVerb.CANCEL, two).digest
        == confirm_bulk(BulkVerb.CANCEL, list(reversed(two))).digest
    )
    assert confirm_bulk(BulkVerb.CANCEL, one).digest != confirm_bulk(BulkVerb.CANCEL, two).digest
    assert confirm_bulk(BulkVerb.CANCEL, one).digest != confirm_bulk(BulkVerb.RETRY, one).digest


def test_del_024_a_confirmation_of_nothing_is_refused() -> None:
    with pytest.raises(ValidationError, match="item_refs"):
        confirm_bulk(BulkVerb.CANCEL, [])


def test_del_024_a_count_that_disagrees_with_the_items_is_refused() -> None:
    shown = confirm_bulk(BulkVerb.CANCEL, [parse_qualified_urn(run(1))]).model_dump(mode="json")

    with pytest.raises(ValidationError, match="does not count"):
        BulkConfirmation.model_validate({**shown, "target_count": 2})


# ---- DEL-025: the record replays exactly -------------------------------------


def test_del_025_an_operation_round_trips_through_its_json_answer() -> None:
    record = operation([BulkItemState.ACCEPTED, BulkItemState.REJECTED])

    assert BulkOperation.model_validate(record.model_dump(mode="json")) == record


def test_del_025_an_idempotency_key_is_bounded() -> None:
    with pytest.raises(ValidationError, match="idempotency_key"):
        operation([BulkItemState.ACCEPTED], idempotency_key="")
    with pytest.raises(ValidationError, match="idempotency_key"):
        operation([BulkItemState.ACCEPTED], idempotency_key="k" * 129)
    assert operation([BulkItemState.ACCEPTED], idempotency_key="k" * 128)
