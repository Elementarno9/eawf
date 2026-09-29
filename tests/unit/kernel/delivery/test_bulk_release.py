"""Releasing Task leases in bulk: the verb's item kind, its confirmation and its record.

The daemon-side proofs that each Task is judged, released and replayed on its own are in
``tests/integration/runtime/daemon/test_task_release_bulk.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.bulk import (
    BULK_CONTROLS,
    BULK_ITEM_KINDS,
    INVALIDATION_RULE,
    BulkConfirmation,
    BulkVerb,
    confirm_bulk,
    require_item_kinds,
)
from eawf.kernel.identity import EntityKind
from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.state.epoch2.task import TaskReleaseRecord
from tests.unit.kernel.delivery.test_bulk import operation, run

_TASKS: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task"
BATCH: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def task(n: int) -> str:
    """Return the URN of the *n*-th Task."""
    return f"{_TASKS}/EAWF-{n:04d}"


def record(**overrides: Any) -> TaskReleaseRecord:
    """Return a release record, with *overrides* applied."""
    fields: dict[str, Any] = {
        "task_ref": task(1),
        "cause": "operator-abort",
        "actor": "OP-0001",
        "prior_revision": 3,
        "new_revision": 4,
        "target_batch_ref": BATCH,
        "recorded_at": AT,
    }
    return TaskReleaseRecord.model_validate({**fields, **overrides})


# ---- DEL-021: each verb names one kind of item -------------------------------


def test_del_021_release_names_tasks_and_every_run_control_names_runs() -> None:
    assert BULK_ITEM_KINDS[BulkVerb.RELEASE] is EntityKind.TASK
    assert {BULK_ITEM_KINDS[verb] for verb in BULK_CONTROLS} == {EntityKind.RUN}
    assert set(BULK_ITEM_KINDS) == set(BulkVerb)


def test_del_021_release_is_a_lease_release_and_never_a_run_control() -> None:
    assert BulkVerb.RELEASE not in BULK_CONTROLS


@pytest.mark.parametrize(
    ("verb", "item"),
    [(BulkVerb.RELEASE, run(1)), (BulkVerb.CANCEL, task(1)), (BulkVerb.RETRY, task(1))],
)
def test_del_021_an_item_of_the_other_kind_is_refused(verb: BulkVerb, item: str) -> None:
    with pytest.raises(ValueError, match=f"{verb.value} takes"):
        require_item_kinds(verb, [parse_qualified_urn(item)])


def test_del_021_an_empty_item_set_has_no_foreign_item() -> None:
    require_item_kinds(BulkVerb.RELEASE, [])


def test_del_021_a_confirmation_mixing_kinds_is_refused() -> None:
    with pytest.raises(ValidationError, match="release takes task items"):
        confirm_bulk(BulkVerb.RELEASE, [parse_qualified_urn(task(1)), parse_qualified_urn(run(1))])


def test_del_021_an_operation_whose_items_do_not_fit_its_verb_is_refused() -> None:
    with pytest.raises(ValidationError, match="release takes task items"):
        operation([], verb="release", item_refs=[run(1)])


def test_del_021_a_bulk_item_is_a_run_or_a_task_and_nothing_else() -> None:
    batch = parse_qualified_urn(BATCH)
    with pytest.raises(ValidationError):
        BulkConfirmation.model_validate(
            {
                "verb": "release",
                "target_count": 1,
                "item_refs": [str(batch)],
                "effects": ["x"],
                "non_effects": ["y"],
                "invalidation_rule": INVALIDATION_RULE,
            }
        )


# ---- DEL-024: the release confirmation states its non-effects ------------------


def test_del_024_release_confirms_count_effects_non_effects_and_the_rule() -> None:
    shown = confirm_bulk(BulkVerb.RELEASE, [parse_qualified_urn(task(n)) for n in (2, 1)])

    assert shown.target_count == 2
    assert [str(ref) for ref in shown.item_refs] == [task(1), task(2)]
    assert any("back to PLANNED" in line for line in shown.effects)
    assert any("release record" in line for line in shown.effects)
    assert any("does not cancel" in line for line in shown.non_effects)
    assert any("another principal's claim" in line for line in shown.non_effects)
    assert shown.invalidation_rule == INVALIDATION_RULE


def test_del_024_a_single_task_release_is_still_confirmed() -> None:
    assert confirm_bulk(BulkVerb.RELEASE, [parse_qualified_urn(task(1))]).target_count == 1


def test_del_024_release_and_cancel_confirm_to_different_digests() -> None:
    tasks = [parse_qualified_urn(task(1))]
    runs = [parse_qualified_urn(run(1))]

    assert (
        confirm_bulk(BulkVerb.RELEASE, tasks).digest != confirm_bulk(BulkVerb.CANCEL, runs).digest
    )


# ---- the release record ----------------------------------------------------------


def test_del_024_a_release_record_names_its_cause_actor_and_revisions() -> None:
    held = record()

    assert held.payload_kind == "task_release"
    assert (held.prior_revision, held.new_revision) == (3, 4)


def test_del_024_a_release_record_that_does_not_advance_the_revision_is_refused() -> None:
    with pytest.raises(ValidationError, match="does not follow"):
        record(new_revision=3)


def test_del_024_a_release_record_moving_backwards_is_refused() -> None:
    with pytest.raises(ValidationError, match="does not follow"):
        record(new_revision=2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cause", ""),
        ("cause", "Operator Abort"),
        ("cause", "a" * 65),
        ("actor", "op-1"),
        ("prior_revision", 0),
        ("task_ref", run(1)),
        ("target_batch_ref", task(1)),
    ],
)
def test_del_024_a_release_record_with_an_invalid_field_is_refused(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        record(**{field: value})


def test_del_024_a_release_record_carries_no_unknown_field() -> None:
    with pytest.raises(ValidationError):
        record(run_dispositions=[])


def test_del_024_a_release_record_without_its_batch_is_refused() -> None:
    with pytest.raises(ValidationError):
        record(target_batch_ref=None)
