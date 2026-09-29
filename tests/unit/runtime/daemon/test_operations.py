"""SURF-084: the operation store a long verb's reference names.

Under test: the reference shape and its parse, the stable id a verb and key
name, the trail's append and read-back, each state move and the outcomes it
admits, the projection that reads an abandoned non-terminal row as unknown
without writing, and the publication ledger stated as an operation trail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.publication import (
    OperationAttempt,
    PublicationOperation,
    PublicationOperationKind,
    PublicationOperationStatus,
)
from eawf.kernel.spec.release import ReleaseTargetStatus
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.operation import (
    OperationError,
    OperationRecord,
    OperationState,
)
from eawf.runtime.daemon import operations

pytestmark = pytest.mark.unit

AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
INSTANCE = "41@2026-09-29T12:00:00+00:00"
DIGEST = "sha256:" + "a" * 64


def _queued(**updates: object) -> OperationRecord:
    return OperationRecord.model_validate(
        {
            "operation_id": operations.operation_id_for("runtime.delivery.prove_task", "k-1"),
            "verb": "runtime.delivery.prove_task",
            "subject": "eawf://WS/PRJ/REP/task/T-0001",
            "idempotency_key": "k-1",
            "request_fingerprint": DIGEST,
            "state": OperationState.QUEUED,
            "revision": 0,
            "submitted_at": AT,
            "updated_at": AT,
            "daemon_instance": INSTANCE,
            **updates,
        }
    )


# ---- the record ---------------------------------------------------------------


def test_surf_084_the_operation_store_kind_carries_operation_records() -> None:
    assert PAYLOAD_MODELS[StoreKind.OPERATION] is OperationRecord


def test_surf_084_a_success_without_its_result_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries its result"):
        _queued(state=OperationState.SUCCEEDED)


def test_surf_084_an_error_on_a_running_operation_is_refused() -> None:
    with pytest.raises(ValidationError, match="cannot sit on a running"):
        _queued(state=OperationState.RUNNING, error=OperationError(code=-32002, message="x"))


def test_surf_084_a_record_refuses_an_unknown_field_and_a_negative_revision() -> None:
    with pytest.raises(ValidationError):
        _queued(extra="field")
    with pytest.raises(ValidationError):
        _queued(revision=-1)


# ---- references -----------------------------------------------------------------


def test_surf_084_the_same_verb_and_key_name_the_same_operation() -> None:
    first = operations.operation_id_for("runtime.delivery.prove_task", "k-1")
    assert first == operations.operation_id_for("runtime.delivery.prove_task", "k-1")
    assert first != operations.operation_id_for("runtime.delivery.integrate", "k-1")
    assert first != operations.operation_id_for("runtime.delivery.prove_task", "k-2")


def test_surf_084_a_reference_round_trips_through_its_parse() -> None:
    record = _queued()
    reference = operations.operation_reference(record)
    assert reference == f"operation://{record.operation_id}"
    assert operations.parse_reference(reference) == (None, record.operation_id)


def test_surf_084_a_release_reference_parses_to_its_release_key() -> None:
    operation_id = uuid.uuid4()
    assert operations.parse_reference(f"operation://REL-0.7.0/{operation_id}") == (
        "REL-0.7.0",
        operation_id,
    )


@pytest.mark.parametrize(
    "reference",
    ["", "operation://", "not-a-reference", "operation://nope", "operation://TASK-1/" + "0" * 32],
)
def test_surf_084_a_malformed_reference_is_refused(reference: str) -> None:
    with pytest.raises(ValueError, match="operation_ref_invalid"):
        operations.parse_reference(reference)


# ---- the trail ------------------------------------------------------------------


def test_surf_084_an_unwritten_trail_reads_empty(tmp_path: Path) -> None:
    assert operations.read_trail(tmp_path / "state.json", uuid.uuid4()) == ()


def test_surf_084_the_trail_lives_in_the_machine_local_tier(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    operations.append_record(state_path, _queued())
    assert operations.operation_store_path(state_path) == (
        tmp_path / ".ea" / "local" / "operation.jsonl"
    )
    assert operations.operation_store_path(state_path).is_file()
    assert not (tmp_path / ".ea" / "store").exists()


def test_surf_084_the_trail_reads_back_one_operation_in_revision_order(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    queued = _queued()
    running = operations.advance(queued, OperationState.RUNNING, at=AT)
    other = _queued(operation_id=uuid.uuid4())
    for record in (running, other, queued):
        operations.append_record(state_path, record)
    assert operations.read_trail(state_path, queued.operation_id) == (queued, running)


def test_surf_084_a_corrupt_trail_line_is_refused(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    operations.append_record(state_path, _queued())
    path = operations.operation_store_path(state_path)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2 is not an operation"):
        operations.read_trail(state_path, uuid.uuid4())


# ---- moves ----------------------------------------------------------------------


def test_surf_084_each_move_is_the_next_revision() -> None:
    running = operations.advance(_queued(), OperationState.RUNNING, at=AT + timedelta(seconds=1))
    assert (running.state, running.revision) == (OperationState.RUNNING, 1)
    assert running.updated_at == AT + timedelta(seconds=1)


def test_surf_084_nothing_follows_a_terminal_state() -> None:
    done = operations.settle(_queued(), {"result": {"passed": True}}, at=AT)
    with pytest.raises(ValueError, match="no state follows it"):
        operations.advance(done, OperationState.RUNNING, at=AT)


def test_surf_084_a_result_frame_settles_succeeded_carrying_the_answer() -> None:
    done = operations.settle(_queued(), {"jsonrpc": "2.0", "result": {"passed": True}}, at=AT)
    assert (done.state, done.result, done.error) == (
        OperationState.SUCCEEDED,
        {"passed": True},
        None,
    )


def test_surf_084_an_error_frame_settles_failed_carrying_the_error_unchanged() -> None:
    error = {"code": -32002, "message": "validation_failed: revision_conflict: stale"}
    done = operations.settle(_queued(), {"error": error}, at=AT)
    assert done.state is OperationState.FAILED
    assert done.error == OperationError(**error)


# ---- the follower's projection ----------------------------------------------------


def test_surf_084_a_live_operation_reads_as_recorded() -> None:
    record = _queued()
    assert operations.observed(record, instance=INSTANCE, live=True) is record


@pytest.mark.parametrize(
    ("instance", "live"), [("7@another-boot", True), (INSTANCE, False)], ids=["gone", "idle"]
)
def test_surf_084_a_row_nothing_runs_any_more_reads_unknown(instance: str, live: bool) -> None:
    seen = operations.observed(_queued(), instance=instance, live=live)
    assert (seen.state, seen.revision) == (OperationState.UNKNOWN, 1)


def test_surf_084_a_terminal_row_is_never_reprojected() -> None:
    done = operations.settle(_queued(), {"result": {}}, at=AT)
    assert operations.observed(done, instance="7@another-boot", live=False) is done


# ---- the publication ledger as a trail -----------------------------------------------


def _attempt(target: str, status: ReleaseTargetStatus) -> OperationAttempt:
    settled = status not in {ReleaseTargetStatus.QUEUED, ReleaseTargetStatus.IN_FLIGHT}
    return OperationAttempt(
        target_id=target,
        attempt=1,
        status=status,
        request_digest=DIGEST,
        effect_receipt_ref="receipt://effect/1" if settled else None,
        observation_receipt_ref=(
            "receipt://observed/1" if status.value.startswith("observed_") else None
        ),
        started_at=AT,
        deadline_at=AT + timedelta(minutes=5),
        settled_at=AT + timedelta(minutes=1) if settled else None,
    )


def _publication(
    status: PublicationOperationStatus, *legs: OperationAttempt, revision: int = 0
) -> PublicationOperation:
    return PublicationOperation(
        operation_id=uuid.UUID(int=7),
        release_ref="REL-0.7.0",
        kind=PublicationOperationKind.PUBLISH,
        proof_digest=DIGEST,
        idempotency_key="publish-1",
        status=status,
        publication_receipts=legs,
        opened_at=AT,
        revision=revision,
    )


@pytest.mark.parametrize(
    ("status", "legs", "expected"),
    [
        (PublicationOperationStatus.OPEN, (), OperationState.RUNNING),
        (PublicationOperationStatus.ABANDONED, (), OperationState.FAILED),
        (
            PublicationOperationStatus.SETTLED,
            (ReleaseTargetStatus.REPORTED_SUCCESS, ReleaseTargetStatus.OBSERVED_SUCCESS),
            OperationState.SUCCEEDED,
        ),
        (
            PublicationOperationStatus.SETTLED,
            (ReleaseTargetStatus.REPORTED_SUCCESS, ReleaseTargetStatus.REPORTED_FAILURE),
            OperationState.FAILED,
        ),
        (
            PublicationOperationStatus.SETTLED,
            (ReleaseTargetStatus.REPORTED_FAILURE, ReleaseTargetStatus.UNKNOWN),
            OperationState.UNKNOWN,
        ),
    ],
)
def test_surf_084_a_publication_episode_states_in_the_operation_vocabulary(
    status: PublicationOperationStatus,
    legs: tuple[ReleaseTargetStatus, ...],
    expected: OperationState,
) -> None:
    attempts = tuple(_attempt(f"t{index}", leg) for index, leg in enumerate(legs))
    (record,) = operations.publication_trail([_publication(status, *attempts)], uuid.UUID(int=7))
    assert record.state is expected
    assert (record.verb, record.subject) == ("release.publish", "REL-0.7.0")


def test_surf_084_a_publication_trail_keeps_only_its_episode_in_order() -> None:
    later = _publication(PublicationOperationStatus.OPEN, revision=2)
    first = _publication(PublicationOperationStatus.OPEN, revision=0)
    stranger = first.model_copy(update={"operation_id": uuid.UUID(int=8)})
    trail = operations.publication_trail([later, stranger, first], uuid.UUID(int=7))
    assert [record.revision for record in trail] == [0, 2]
    assert operations.publication_trail([], uuid.UUID(int=7)) == ()
