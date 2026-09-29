"""AUTH-022: mass operations are selection-based with per-item results.

One operation carries an explicit item set, judges each item on its own,
and reports each item's outcome; a partial outcome is a normal terminal
answer. Only recoverable control is a bulk verb. The daemon-side proof
that a denied item is rejected alone while the others proceed is
``test_del_021_a_denied_item_is_rejected_alone_and_the_operation_still_answers``
in ``tests/integration/runtime/daemon/test_bulk_operation.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.bulk import (
    BULK_CONTROLS,
    SETTLED_ITEM_STATES,
    BulkItemState,
    BulkVerb,
)
from eawf.kernel.runtime.provider import ControlKind
from tests.unit.kernel.delivery.test_bulk import operation


def test_auth_022_bulk_verbs_are_the_recoverable_run_controls_and_the_task_release() -> None:
    assert set(BulkVerb) == {
        BulkVerb.CANCEL,
        BulkVerb.INTERRUPT,
        BulkVerb.RETRY,
        BulkVerb.RELEASE,
    }
    assert set(BULK_CONTROLS) == set(BulkVerb) - {BulkVerb.RELEASE}
    assert set(BULK_CONTROLS.values()) <= set(ControlKind)


@pytest.mark.parametrize(
    "verb", ["integrate", "merge", "accept", "approve", "publish", "complete", "hold"]
)
def test_auth_022_integration_merge_acceptance_and_publication_are_never_bulk_verbs(
    verb: str,
) -> None:
    with pytest.raises(ValueError, match="is not a valid BulkVerb"):
        BulkVerb(verb)


def test_auth_022_a_partial_outcome_is_a_normal_terminal_result() -> None:
    states = [BulkItemState.CONFIRMED, BulkItemState.REJECTED, BulkItemState.INVALIDATED]
    answered = operation(states)
    assert set(states) <= SETTLED_ITEM_STATES
    assert answered.aggregate[BulkItemState.CONFIRMED] == 1
    assert answered.aggregate[BulkItemState.REJECTED] == 1
    assert answered.aggregate[BulkItemState.INVALIDATED] == 1


def test_auth_022_every_item_carries_its_own_result() -> None:
    answered = operation([BulkItemState.CONFIRMED, BulkItemState.UNKNOWN])
    assert set(answered.item_results) == {str(ref) for ref in answered.item_refs}


def test_auth_022_an_operation_over_no_explicit_item_is_refused() -> None:
    with pytest.raises(ValidationError):
        operation([])


def test_auth_022_a_single_item_operation_is_still_an_operation() -> None:
    assert len(operation([BulkItemState.ACCEPTED]).item_refs) == 1
