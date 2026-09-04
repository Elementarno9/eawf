"""The eight-state per-target publication machine.

Under test: the exact edge set, the deadline that admits ``unknown``,
the ``retry_limit`` that bounds a re-queue, the derivation of
``attempt_count`` from the persisted rows, and the typed errors an
illegal edge or an over-budget retry raise.

Every test drives the *authored* ``0.7.0.dev1`` target configurations
rather than a hand-built stand-in, so a change to the checkpoint file
reds these tests instead of leaving them agreeing with a fixture nobody
ships.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.publication import (
    PublicationOperation,
    PublicationOperationKind,
    attempt_count,
    latest_attempt,
)
from eawf.kernel.spec.release import ReleaseTargetStatus
from eawf.kernel.spec.release_config import ReleaseTargetConfig
from eawf.workflow.release.target_machine import (
    TARGET_DENIALS,
    TARGET_TRANSITIONS,
    TERMINAL_TARGET_STATUSES,
    TargetDenialCode,
    TargetGuardContext,
    TargetGuardName,
    TargetTransitionError,
    advance_target_attempt,
    current_target_status,
    deadline_elapsed,
    next_target_statuses,
    open_target_attempt,
    retry_budget_remaining,
    validate_target_transition,
)
from tests.unit.kernel.release.conftest import dev1_config

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
REQUEST_DIGEST = f"sha256:{'2' * 64}"
PROOF_DIGEST = f"sha256:{'1' * 64}"
EFFECT = "receipt://pypi/upload"
OBSERVATION = "receipt://pypi/read-back"


def pypi_target(**overrides: object) -> ReleaseTargetConfig:
    """Return the authored ``pypi`` target configuration with *overrides*."""
    target = next(t for t in dev1_config().targets if t.target_id == "pypi")
    if not overrides:
        return target
    return ReleaseTargetConfig.model_validate({**target.model_dump(mode="json"), **overrides})


def empty_operation() -> PublicationOperation:
    """Return an open ``publish`` operation with no attempts yet."""
    return PublicationOperation(
        operation_id=UUID(int=27),
        release_ref="REL-0.7.0.dev1",
        kind=PublicationOperationKind.PUBLISH,
        proof_digest=PROOF_DIGEST,
        idempotency_key="publish-0.7.0.dev1-01",
        opened_at=NOW,
    )


def queued(target: ReleaseTargetConfig | None = None) -> PublicationOperation:
    """Return an operation whose ``pypi`` leg has a queued attempt 1."""
    return open_target_attempt(
        empty_operation(),
        target=target or pypi_target(),
        now=NOW,
        request_digest=REQUEST_DIGEST,
    )


def in_flight(target: ReleaseTargetConfig | None = None) -> PublicationOperation:
    """Return an operation whose ``pypi`` leg is in flight on attempt 1."""
    config = target or pypi_target()
    return advance_target_attempt(
        queued(config),
        target=config,
        to=ReleaseTargetStatus.IN_FLIGHT,
        now=NOW,
    )


def reported(
    status: ReleaseTargetStatus,
    target: ReleaseTargetConfig | None = None,
) -> PublicationOperation:
    """Return an operation whose ``pypi`` leg reported *status*."""
    config = target or pypi_target()
    return advance_target_attempt(
        in_flight(config),
        target=config,
        to=status,
        now=NOW + timedelta(seconds=10),
        effect_receipt_ref=EFFECT,
    )


def timed_out(target: ReleaseTargetConfig | None = None) -> PublicationOperation:
    """Return an operation whose ``pypi`` leg timed out into ``unknown``."""
    config = target or pypi_target()
    return advance_target_attempt(
        in_flight(config),
        target=config,
        to=ReleaseTargetStatus.UNKNOWN,
        now=NOW + timedelta(seconds=config.timeout_seconds),
    )


# --- the edge table ------------------------------------------------------


def test_table_covers_every_target_status() -> None:
    assert set(TARGET_TRANSITIONS) == set(ReleaseTargetStatus)


def test_table_admits_only_the_declared_edges() -> None:
    edges = {
        (frm.value, to.value)
        for frm, targets in TARGET_TRANSITIONS.items()
        for to, _guard in targets
    }
    assert edges == {
        ("not_started", "queued"),
        ("queued", "in_flight"),
        ("in_flight", "reported_success"),
        ("in_flight", "reported_failure"),
        ("in_flight", "unknown"),
        ("reported_success", "observed_success"),
        ("reported_success", "observed_mismatch"),
        ("reported_failure", "queued"),
        ("unknown", "queued"),
        ("unknown", "observed_success"),
        ("unknown", "observed_mismatch"),
    }


def test_only_the_observed_statuses_are_terminal() -> None:
    assert {
        ReleaseTargetStatus.OBSERVED_SUCCESS,
        ReleaseTargetStatus.OBSERVED_MISMATCH,
    } == TERMINAL_TARGET_STATUSES


def test_every_guarded_edge_declares_a_denial() -> None:
    guarded = {
        (frm, to)
        for frm, targets in TARGET_TRANSITIONS.items()
        for to, guard in targets
        if guard is not TargetGuardName.NONE
    }
    assert guarded == set(TARGET_DENIALS)


def test_next_target_statuses_is_empty_for_a_terminal_status() -> None:
    assert next_target_statuses(ReleaseTargetStatus.OBSERVED_SUCCESS) == frozenset()


def test_illegal_edge_raises_the_typed_error() -> None:
    with pytest.raises(TargetTransitionError) as excinfo:
        validate_target_transition(
            "pypi", ReleaseTargetStatus.QUEUED, ReleaseTargetStatus.REPORTED_SUCCESS
        )
    assert excinfo.value.code is TargetDenialCode.ILLEGAL_TARGET_TRANSITION
    assert excinfo.value.target_id == "pypi"
    assert "illegal target transition" in str(excinfo.value)


def test_observed_success_is_denied_on_a_mismatched_read_back() -> None:
    with pytest.raises(TargetTransitionError) as excinfo:
        validate_target_transition(
            "pypi",
            ReleaseTargetStatus.REPORTED_SUCCESS,
            ReleaseTargetStatus.OBSERVED_SUCCESS,
            TargetGuardContext(observation_matched=False),
        )
    assert excinfo.value.code is TargetDenialCode.OBSERVATION_MISMATCHED


def test_observed_mismatch_is_unguarded() -> None:
    assert (
        validate_target_transition(
            "pypi",
            ReleaseTargetStatus.REPORTED_SUCCESS,
            ReleaseTargetStatus.OBSERVED_MISMATCH,
            TargetGuardContext(observation_matched=False),
        )
        is None
    )


# --- the happy path ------------------------------------------------------


def test_leg_starts_not_started() -> None:
    assert current_target_status(empty_operation(), "pypi") is ReleaseTargetStatus.NOT_STARTED


def test_first_attempt_queues_and_stamps_the_deadline() -> None:
    op = queued()
    row = latest_attempt(op, "pypi")
    assert row is not None
    assert row.attempt == 1
    assert row.status is ReleaseTargetStatus.QUEUED
    assert row.deadline_at == NOW + timedelta(seconds=pypi_target().timeout_seconds)
    assert op.revision == 1


def test_queued_advances_to_in_flight_then_reported_success() -> None:
    op = reported(ReleaseTargetStatus.REPORTED_SUCCESS)
    row = latest_attempt(op, "pypi")
    assert row is not None
    assert row.status is ReleaseTargetStatus.REPORTED_SUCCESS
    assert row.effect_receipt_ref == EFFECT
    assert row.settled_at == NOW + timedelta(seconds=10)


def test_reported_success_advances_to_observed_success_on_a_match() -> None:
    op = advance_target_attempt(
        reported(ReleaseTargetStatus.REPORTED_SUCCESS),
        target=pypi_target(),
        to=ReleaseTargetStatus.OBSERVED_SUCCESS,
        now=NOW + timedelta(seconds=20),
        observation_receipt_ref=OBSERVATION,
        observation_matched=True,
    )
    row = latest_attempt(op, "pypi")
    assert row is not None
    assert row.observation_receipt_ref == OBSERVATION


def test_reported_success_is_denied_observed_success_on_a_mismatch() -> None:
    with pytest.raises(TargetTransitionError) as excinfo:
        advance_target_attempt(
            reported(ReleaseTargetStatus.REPORTED_SUCCESS),
            target=pypi_target(),
            to=ReleaseTargetStatus.OBSERVED_SUCCESS,
            now=NOW + timedelta(seconds=20),
            observation_receipt_ref=OBSERVATION,
            observation_matched=False,
        )
    assert excinfo.value.code is TargetDenialCode.OBSERVATION_MISMATCHED


def test_unknown_reconciles_straight_to_observed_success() -> None:
    op = advance_target_attempt(
        timed_out(),
        target=pypi_target(),
        to=ReleaseTargetStatus.OBSERVED_SUCCESS,
        now=NOW + timedelta(seconds=2000),
        effect_receipt_ref=EFFECT,
        observation_receipt_ref=OBSERVATION,
    )
    row = latest_attempt(op, "pypi")
    assert row is not None
    assert row.status is ReleaseTargetStatus.OBSERVED_SUCCESS


# --- the timeout ---------------------------------------------------------


def test_unknown_is_denied_one_second_before_the_deadline() -> None:
    target = pypi_target()
    with pytest.raises(TargetTransitionError) as excinfo:
        advance_target_attempt(
            in_flight(target),
            target=target,
            to=ReleaseTargetStatus.UNKNOWN,
            now=NOW + timedelta(seconds=target.timeout_seconds - 1),
        )
    assert excinfo.value.code is TargetDenialCode.TARGET_DEADLINE_NOT_ELAPSED


def test_unknown_is_admitted_at_the_deadline_instant() -> None:
    target = pypi_target()
    op = advance_target_attempt(
        in_flight(target),
        target=target,
        to=ReleaseTargetStatus.UNKNOWN,
        now=NOW + timedelta(seconds=target.timeout_seconds),
    )
    row = latest_attempt(op, "pypi")
    assert row is not None
    assert row.status is ReleaseTargetStatus.UNKNOWN
    assert row.effect_receipt_ref is None


def test_deadline_elapsed_is_false_before_and_true_at_the_deadline() -> None:
    target = pypi_target()
    op = in_flight(target)
    assert not deadline_elapsed(op, "pypi", NOW)
    assert not deadline_elapsed(op, "pypi", NOW + timedelta(seconds=target.timeout_seconds - 1))
    assert deadline_elapsed(op, "pypi", NOW + timedelta(seconds=target.timeout_seconds))


def test_deadline_elapsed_rejects_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        deadline_elapsed(in_flight(), "pypi", datetime(2026, 9, 4, 12, 0))


def test_deadline_elapsed_raises_key_error_for_an_unattempted_target() -> None:
    with pytest.raises(KeyError, match="no attempt for"):
        deadline_elapsed(empty_operation(), "npm", NOW)


# --- the retry budget ----------------------------------------------------


def test_attempt_count_equals_the_highest_persisted_attempt() -> None:
    target = pypi_target()
    op = open_target_attempt(
        reported(ReleaseTargetStatus.REPORTED_FAILURE, target),
        target=target,
        now=NOW + timedelta(seconds=30),
        request_digest=REQUEST_DIGEST,
    )
    assert attempt_count(op, "pypi") == 2
    assert len([row for row in op.publication_receipts if row.target_id == "pypi"]) == 2


def test_authored_retry_limit_admits_exactly_three_attempts() -> None:
    target = pypi_target()
    assert target.retry_limit == 2
    op = reported(ReleaseTargetStatus.REPORTED_FAILURE, target)
    for _attempt in range(target.retry_limit):
        op = open_target_attempt(
            op, target=target, now=NOW + timedelta(seconds=30), request_digest=REQUEST_DIGEST
        )
        op = advance_target_attempt(
            op, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=NOW + timedelta(seconds=31)
        )
        op = advance_target_attempt(
            op,
            target=target,
            to=ReleaseTargetStatus.REPORTED_FAILURE,
            now=NOW + timedelta(seconds=32),
            effect_receipt_ref=EFFECT,
        )
    assert attempt_count(op, "pypi") == 3
    assert not retry_budget_remaining(op, target)
    with pytest.raises(TargetTransitionError) as excinfo:
        open_target_attempt(
            op, target=target, now=NOW + timedelta(seconds=40), request_digest=REQUEST_DIGEST
        )
    assert excinfo.value.code is TargetDenialCode.RETRY_LIMIT_EXHAUSTED


def test_retry_limit_zero_admits_one_attempt_and_no_retry() -> None:
    target = pypi_target(retry_limit=0)
    op = reported(ReleaseTargetStatus.REPORTED_FAILURE, target)
    assert attempt_count(op, "pypi") == 1
    assert not retry_budget_remaining(op, target)
    with pytest.raises(TargetTransitionError) as excinfo:
        open_target_attempt(
            op, target=target, now=NOW + timedelta(seconds=30), request_digest=REQUEST_DIGEST
        )
    assert excinfo.value.code is TargetDenialCode.RETRY_LIMIT_EXHAUSTED


def test_unknown_leg_may_be_requeued_within_budget() -> None:
    target = pypi_target()
    op = open_target_attempt(
        timed_out(target),
        target=target,
        now=NOW + timedelta(seconds=2000),
        request_digest=REQUEST_DIGEST,
    )
    assert current_target_status(op, "pypi") is ReleaseTargetStatus.QUEUED
    assert attempt_count(op, "pypi") == 2


def test_requeue_from_reported_success_is_illegal() -> None:
    with pytest.raises(TargetTransitionError) as excinfo:
        open_target_attempt(
            reported(ReleaseTargetStatus.REPORTED_SUCCESS),
            target=pypi_target(),
            now=NOW + timedelta(seconds=30),
            request_digest=REQUEST_DIGEST,
        )
    assert excinfo.value.code is TargetDenialCode.ILLEGAL_TARGET_TRANSITION


# --- error paths ---------------------------------------------------------


def test_open_target_attempt_rejects_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        open_target_attempt(
            empty_operation(),
            target=pypi_target(),
            now=datetime(2026, 9, 4, 12, 0),
            request_digest=REQUEST_DIGEST,
        )


def test_advance_target_attempt_rejects_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        advance_target_attempt(
            queued(),
            target=pypi_target(),
            to=ReleaseTargetStatus.IN_FLIGHT,
            now=datetime(2026, 9, 4, 12, 0),
        )


def test_advance_target_attempt_raises_key_error_for_an_unattempted_target() -> None:
    npm = next(t for t in dev1_config().targets if t.target_id == "npm")
    with pytest.raises(KeyError, match="no attempt for target"):
        advance_target_attempt(queued(), target=npm, to=ReleaseTargetStatus.IN_FLIGHT, now=NOW)


def test_reported_success_without_an_effect_receipt_fails_validation() -> None:
    with pytest.raises(ValidationError, match="requires effect_receipt_ref"):
        advance_target_attempt(
            in_flight(),
            target=pypi_target(),
            to=ReleaseTargetStatus.REPORTED_SUCCESS,
            now=NOW + timedelta(seconds=10),
        )


def test_observed_success_without_an_observation_receipt_fails_validation() -> None:
    with pytest.raises(ValidationError, match="requires observation_receipt_ref"):
        advance_target_attempt(
            reported(ReleaseTargetStatus.REPORTED_SUCCESS),
            target=pypi_target(),
            to=ReleaseTargetStatus.OBSERVED_SUCCESS,
            now=NOW + timedelta(seconds=20),
        )


def test_advance_from_a_terminal_leg_is_illegal() -> None:
    op = advance_target_attempt(
        reported(ReleaseTargetStatus.REPORTED_SUCCESS),
        target=pypi_target(),
        to=ReleaseTargetStatus.OBSERVED_SUCCESS,
        now=NOW + timedelta(seconds=20),
        observation_receipt_ref=OBSERVATION,
    )
    with pytest.raises(TargetTransitionError) as excinfo:
        advance_target_attempt(
            op,
            target=pypi_target(),
            to=ReleaseTargetStatus.OBSERVED_MISMATCH,
            now=NOW + timedelta(seconds=30),
            observation_receipt_ref=OBSERVATION,
        )
    assert excinfo.value.code is TargetDenialCode.ILLEGAL_TARGET_TRANSITION
