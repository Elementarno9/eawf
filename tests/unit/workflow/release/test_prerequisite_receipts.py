"""Opening rung n+1 re-validates every gate receipt of rung n.

A gate receipt is only worth anything while the build it was earned on
is still the build the checkpoint claims. These tests pin the three ways
that stops being true -- a required gate with no receipt at all, a
receipt bound to different source or a different manifest, and a receipt
past its expiry -- and that each refusal names the gate, so the operator
learns which gate to re-run rather than that "a receipt" was bad.

The receipt and record builders come from the sibling advance module
rather than the shared ``conftest``: they belong to this pair of test
modules, and a package-wide fixture would invite unrelated modules to
depend on the exact dev1 receipt shape.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.advance import (
    CheckpointGateReceipt,
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    assert_prerequisite_receipts,
)
from eawf.workflow.release.train import V07_TRAIN
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    SOURCE_SHA,
    dev1_config,
)
from tests.unit.workflow.release.test_train_advance import (
    advance,
    finished,
    gate_receipts,
)

#: A commit that is not the one the dev1 record pinned.
OTHER_SHA = "d" * 40

#: A manifest digest that is not the one the dev1 record approved.
OTHER_DIGEST = f"sha256:{'e' * 64}"

#: The train id every refusal is raised against.
TRAIN_ID = V07_TRAIN.train_id

#: The eight gates the authored dev1 configuration requires.
DEV1_GATES = dev1_config().gates.required


def check_bound(
    *,
    release_key: str = "REL-0.7.0.dev1",
    source_sha: str = SOURCE_SHA,
    manifest_digest: str = MANIFEST_DIGEST,
) -> tuple[str, ...]:
    """Validate dev1 receipts built against the supplied binding."""
    return check_receipts(
        gate_receipts(
            release_key=release_key,
            source_sha=source_sha,
            manifest_digest=manifest_digest,
        )
    )


def check_receipts(receipts: list[CheckpointGateReceipt]) -> tuple[str, ...]:
    """Validate an explicit receipt list against the dev1 checkpoint."""
    return assert_prerequisite_receipts(
        finished(), dev1_config(), receipts, now=NOW, train_id=TRAIN_ID
    )


# --- the fresh, bound case ---------------------------------------------------


def test_fresh_bound_receipts_return_the_refs_in_gate_order() -> None:
    """Every required gate carries a receipt; the refs come back ordered."""
    assert check_bound() == tuple(f"receipt://gate/{gate.value}" for gate in DEV1_GATES)


def test_only_the_gates_the_configuration_requires_are_read() -> None:
    """A receipt for a gate the profile does not require is surplus."""
    trimmed = dev1_config(
        gates={"profile": "dev1", "required": ["version_consistency", "changelog_entry"]}
    )

    refs = assert_prerequisite_receipts(
        finished(), trimmed, gate_receipts(), now=NOW, train_id=TRAIN_ID
    )

    assert refs == ("receipt://gate/version_consistency", "receipt://gate/changelog_entry")


# --- a required gate with no receipt -----------------------------------------


@pytest.mark.parametrize("dropped", DEV1_GATES, ids=lambda gate: gate.value)
def test_a_missing_gate_receipt_names_the_gate(dropped: ReleaseGateName) -> None:
    """Dropping any single gate refuses, naming exactly that gate."""
    kept = [gate for gate in DEV1_GATES if gate is not dropped]

    with pytest.raises(TrainAdvanceError) as excinfo:
        check_receipts(gate_receipts(gates=kept))

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING
    assert excinfo.value.gate is dropped
    assert dropped.value in str(excinfo.value)


def test_no_receipts_at_all_refuses_on_the_first_required_gate() -> None:
    """The empty offering fails closed, not vacuously green."""
    with pytest.raises(TrainAdvanceError) as excinfo:
        check_receipts([])

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_MISSING
    assert excinfo.value.gate is DEV1_GATES[0]


# --- a receipt bound to something else ---------------------------------------


@pytest.mark.parametrize(
    ("binding", "value"),
    [
        ({"source_sha": OTHER_SHA}, OTHER_SHA),
        ({"manifest_digest": OTHER_DIGEST}, OTHER_DIGEST),
        ({"release_key": "REL-0.7.0.dev2"}, "REL-0.7.0.dev2"),
    ],
    ids=["source_sha", "manifest_digest", "release_key"],
)
def test_a_receipt_bound_elsewhere_is_stale(binding: dict[str, str], value: str) -> None:
    """Source, manifest and key each have to match exactly."""
    with pytest.raises(TrainAdvanceError) as excinfo:
        check_bound(**binding)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    assert excinfo.value.gate is DEV1_GATES[0]
    assert value in str(excinfo.value)


def test_a_stale_binding_names_the_gate_that_carries_it() -> None:
    """One rotten receipt among eight names its own gate."""
    receipts = gate_receipts()
    receipts[3] = receipts[3].model_copy(update={"source_sha": OTHER_SHA})

    with pytest.raises(TrainAdvanceError) as excinfo:
        check_receipts(receipts)

    assert excinfo.value.gate is DEV1_GATES[3]
    assert DEV1_GATES[3].value in str(excinfo.value)


# --- freshness ---------------------------------------------------------------


def test_a_receipt_past_its_expiry_is_stale() -> None:
    """An expired receipt proves the gate passed, not that it still holds."""
    receipts = gate_receipts()
    receipts[1] = receipts[1].model_copy(update={"expires_at": NOW - timedelta(seconds=1)})

    with pytest.raises(TrainAdvanceError) as excinfo:
        check_receipts(receipts)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    assert excinfo.value.gate is DEV1_GATES[1]


def test_a_receipt_expiring_exactly_now_is_stale() -> None:
    """The window is half-open: expiry at the instant judged is expired."""
    receipts = gate_receipts()
    receipts[0] = receipts[0].model_copy(update={"expires_at": NOW})

    with pytest.raises(TrainAdvanceError) as excinfo:
        check_receipts(receipts)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE


def test_a_receipt_expiring_one_microsecond_later_is_fresh() -> None:
    """The off-by-one on the other side of the boundary still passes."""
    receipts = gate_receipts()
    receipts[0] = receipts[0].model_copy(update={"expires_at": NOW + timedelta(microseconds=1)})

    assert len(check_receipts(receipts)) == len(DEV1_GATES)


# --- error paths -------------------------------------------------------------


def test_a_naive_instant_is_refused() -> None:
    """Freshness against a naive clock is a caller bug, not a verdict."""
    with pytest.raises(ValueError, match="timezone-aware"):
        assert_prerequisite_receipts(
            finished(),
            dev1_config(),
            gate_receipts(),
            now=NOW.replace(tzinfo=None),
            train_id=TRAIN_ID,
        )


def test_a_configuration_for_another_checkpoint_is_refused() -> None:
    """The receipts of one rung are never read against another's gate list."""
    with pytest.raises(ValueError, match="configuration describes"):
        assert_prerequisite_receipts(
            finished(),
            dev1_config().model_copy(update={"version": "0.7.0.dev2"}),
            gate_receipts(),
            now=NOW,
            train_id=TRAIN_ID,
        )


def test_two_receipts_for_one_gate_are_refused() -> None:
    """Ambiguity is a refusal; picking one would let a stale receipt hide."""
    with pytest.raises(ValueError, match="two receipts offered"):
        check_receipts([*gate_receipts(), gate_receipts()[0]])


def test_a_receipt_expiring_before_it_was_issued_is_rejected_at_the_model() -> None:
    """A window that does not move forward never becomes a record."""
    with pytest.raises(ValidationError, match="expires_at must be after issued_at"):
        CheckpointGateReceipt(
            gate=ReleaseGateName.CHANGELOG_ENTRY,
            release_key="REL-0.7.0.dev1",
            source_sha=SOURCE_SHA,
            manifest_digest=MANIFEST_DIGEST,
            issued_at=NOW,
            expires_at=NOW,
            receipt_ref="receipt://gate/changelog_entry",
        )


# --- the advance surfaces the refusal ----------------------------------------


def test_advance_train_refuses_a_stale_prerequisite_and_holds_the_index() -> None:
    """The guard is wired into the advance, not merely available beside it."""
    receipts = gate_receipts()
    receipts[2] = receipts[2].model_copy(update={"manifest_digest": OTHER_DIGEST})

    with pytest.raises(TrainAdvanceError) as excinfo:
        advance(finished(), receipts=receipts)

    assert excinfo.value.code is TrainAdvanceDenialCode.PREREQUISITE_RECEIPT_STALE
    assert V07_TRAIN.current_checkpoint_index == 0
