"""REL-024: an adapter deadline that elapses with no final result.

Under test: the one status a timeout may write is ``unknown``; the two
ways ``reported_failure`` is kept out of that slot; the deadline instant
itself granting permission to time a leg out without moving it; an
observation after the timeout being the only thing that resolves the
ambiguity; and the retry out of ``unknown`` staying inside the target's
``retry_limit``.

The ambiguity is the whole subject. A leg that timed out has produced no
receipt, so nothing can be asserted about what the registry did with the
call -- and the two ways of pretending otherwise are the failure modes:
writing ``reported_failure`` (an adapter's word nobody heard) or letting
the clock write a status by itself. The first is refused by the record
invariant, the second by there being no implicit transition at all.

The clock-sensitive moves are driven through the library with explicit
instants, because the handlers stamp wall-clock ``now`` and a
half-hour timeout cannot be waited out; the resolution itself goes
through the ``release.observe_target`` and ``release.retry_target``
RPCs, which is where an operator meets it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.publication import (
    OperationAttempt,
    PublicationOperation,
    attempt_count,
    require_attempt,
)
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import observe, publish, reconcile, retry_target
from eawf.workflow.release.observation import configured_target
from eawf.workflow.release.target_machine import (
    TargetDenialCode,
    TargetTransitionError,
    advance_target_attempt,
    current_target_status,
    deadline_elapsed,
    open_target_attempt,
)
from tests.integration.runtime.daemon.methods.conftest import (
    EFFECT,
    RELEASE_KEY,
    dev1_config,
    manifest_payload,
    moved,
    publish_params,
    record_snapshot,
    response_payload,
    run_async,
)

pytestmark = pytest.mark.integration

TARGET = "pypi"


def _in_flight(operation: PublicationOperation) -> PublicationOperation:
    """Dispatch every configured leg, leaving each awaiting its adapter."""
    for target in dev1_config().targets:
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
    return operation


def _timed_out(operation: PublicationOperation, *, target_id: str) -> PublicationOperation:
    """Time *target_id* out at its own deadline, with no adapter receipt."""
    target = configured_target(dev1_config(), target_id)
    return advance_target_attempt(
        operation,
        target=target,
        to=ReleaseTargetStatus.UNKNOWN,
        now=require_attempt(operation, target_id).deadline_at,
    )


async def _published(ctx: MethodContext) -> dict[str, Any]:
    """Open the episode and leave every leg awaiting its adapter."""
    result = await publish(ctx, publish_params())
    operation = _in_flight(PublicationOperation.model_validate(result["operation"]))
    record_snapshot(ctx, operation, key="seed-dispatch-33")
    return {"release": result["release"], "operation": operation}


async def _unknown(ctx: MethodContext) -> dict[str, Any]:
    """Leave *TARGET* timed out with nothing recorded about its call."""
    state = await _published(ctx)
    operation = _timed_out(state["operation"], target_id=TARGET)
    record_snapshot(ctx, operation, key="seed-unknown-33")
    return {"release": state["release"], "operation": operation}


# --- the deadline --------------------------------------------------------


def test_a_deadline_that_elapses_with_no_result_settles_unknown(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        row = require_attempt(state["operation"], TARGET)
        assert row.status is ReleaseTargetStatus.UNKNOWN
        assert row.effect_receipt_ref is None
        assert row.settled_at == row.deadline_at

    run_async(body)


def test_a_tick_at_deadline_equality_leaves_the_leg_in_flight(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _published(ctx)
        operation = state["operation"]
        deadline = require_attempt(operation, TARGET).deadline_at
        assert deadline_elapsed(operation, TARGET, deadline)
        assert current_target_status(operation, TARGET) is ReleaseTargetStatus.IN_FLIGHT
        ticked = _timed_out(operation, target_id=TARGET)
        assert current_target_status(ticked, TARGET) is ReleaseTargetStatus.UNKNOWN

    run_async(body)


def test_a_tick_one_second_before_the_deadline_is_denied(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _published(ctx)
        operation = state["operation"]
        target = configured_target(dev1_config(), TARGET)
        early = require_attempt(operation, TARGET).deadline_at - timedelta(seconds=1)
        assert not deadline_elapsed(operation, TARGET, early)
        with pytest.raises(TargetTransitionError) as err:
            advance_target_attempt(
                operation, target=target, to=ReleaseTargetStatus.UNKNOWN, now=early
            )
        assert err.value.code is TargetDenialCode.TARGET_DEADLINE_NOT_ELAPSED

    run_async(body)


def test_a_leg_that_never_answered_cannot_be_written_as_reported_failure(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _published(ctx)
        with pytest.raises(DaemonValidationError, match="requires effect_receipt_ref"):
            await reconcile(
                ctx,
                {
                    "release": state["release"],
                    "expected_revision": state["release"]["revision"],
                    "idempotency_key": "reconcile-failure-33",
                    "target_id": TARGET,
                    "status": ReleaseTargetStatus.REPORTED_FAILURE.value,
                },
            )

    run_async(body)


def test_an_unknown_row_cannot_be_hand_written_with_a_receipt(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        payload = require_attempt(state["operation"], TARGET).model_dump(mode="json")
        with pytest.raises(ValidationError, match="must not carry observation_receipt_ref"):
            OperationAttempt.model_validate(
                payload | {"observation_receipt_ref": "receipt://read-back"}
            )

    run_async(body)


# --- resolution by observation -------------------------------------------


def test_an_observation_after_the_timeout_resolves_unknown_to_observed_success(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        result = await observe(
            ctx,
            {
                "release": state["release"],
                "expected_revision": state["release"]["revision"],
                "idempotency_key": "observe-unknown-match-33",
                "target_id": TARGET,
                "manifest": manifest_payload(),
                "response": response_payload(TARGET, "match"),
                "effect_receipt_ref": EFFECT,
            },
        )
        operation = PublicationOperation.model_validate(result["operation"])
        row = require_attempt(operation, TARGET)
        assert row.status is ReleaseTargetStatus.OBSERVED_SUCCESS
        assert row.observation_receipt_ref == result["observation"]["evidence_ref"]
        assert Release.model_validate(result["release"]).status is ReleaseStatus.PUBLISHING

    run_async(body)


@pytest.mark.parametrize("case", ("mismatch", "missing"))
def test_an_observation_after_the_timeout_resolves_unknown_to_observed_mismatch(
    ctx: MethodContext, green_probes: None, case: str
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        result = await observe(
            ctx,
            {
                "release": state["release"],
                "expected_revision": state["release"]["revision"],
                "idempotency_key": f"observe-unknown-{case}-33",
                "target_id": TARGET,
                "manifest": manifest_payload(),
                "response": response_payload(TARGET, case),
                "effect_receipt_ref": EFFECT,
            },
        )
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, TARGET).status is ReleaseTargetStatus.OBSERVED_MISMATCH
        assert Release.model_validate(result["release"]).status is ReleaseStatus.RECOVERING

    run_async(body)


def test_an_inconclusive_read_back_leaves_the_leg_unknown(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        with pytest.raises(DaemonValidationError, match="observation_inconclusive"):
            await observe(
                ctx,
                {
                    "release": state["release"],
                    "expected_revision": state["release"]["revision"],
                    "idempotency_key": "observe-unknown-inconclusive-33",
                    "target_id": TARGET,
                    "manifest": manifest_payload(),
                    "response": response_payload(TARGET, "unknown"),
                    "effect_receipt_ref": EFFECT,
                },
            )
        assert current_target_status(state["operation"], TARGET) is ReleaseTargetStatus.UNKNOWN

    run_async(body)


# --- the retry budget ----------------------------------------------------


def test_a_retry_from_unknown_is_bounded_by_the_targets_retry_limit(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        recovering = moved(state["release"], ReleaseStatus.RECOVERING)
        operation = state["operation"]
        config = dev1_config()
        target = configured_target(config, TARGET)
        for attempt in range(2, 2 + target.retry_limit):
            result = await retry_target(
                ctx,
                {
                    "release": recovering,
                    "expected_revision": recovering["revision"],
                    "idempotency_key": f"retry-unknown-{attempt}-33",
                    "target_id": TARGET,
                    "proof_digest": operation.proof_digest,
                },
            )
            operation = PublicationOperation.model_validate(result["operation"])
            assert attempt_count(operation, TARGET) == attempt
            operation = _timed_out(_in_flight_leg(operation, TARGET), target_id=TARGET)
            record_snapshot(ctx, operation, key=f"seed-unknown-{attempt}-33")
            recovering = moved(result["release"], ReleaseStatus.RECOVERING)

        assert attempt_count(operation, TARGET) == 1 + target.retry_limit
        with pytest.raises(TargetTransitionError) as err:
            open_target_attempt(
                operation,
                target=target,
                now=datetime.now(UTC),
                request_digest=require_attempt(operation, TARGET).request_digest,
            )
        assert err.value.code is TargetDenialCode.RETRY_LIMIT_EXHAUSTED
        with pytest.raises(DaemonValidationError, match="unsafe_release_retry"):
            await retry_target(
                ctx,
                {
                    "release": recovering,
                    "expected_revision": recovering["revision"],
                    "idempotency_key": "retry-unknown-exhausted-33",
                    "target_id": TARGET,
                    "proof_digest": operation.proof_digest,
                },
            )

    run_async(body)


def _in_flight_leg(operation: PublicationOperation, target_id: str) -> PublicationOperation:
    """Dispatch just *target_id*'s newest attempt."""
    target = configured_target(dev1_config(), target_id)
    return advance_target_attempt(
        operation,
        target=target,
        to=ReleaseTargetStatus.IN_FLIGHT,
        now=require_attempt(operation, target_id).started_at,
    )


def test_a_retry_replays_the_operations_own_idempotency_key(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        recovering = moved(state["release"], ReleaseStatus.RECOVERING)
        with pytest.raises(DaemonValidationError, match="unsafe_release_retry"):
            await retry_target(
                ctx,
                {
                    "release": recovering,
                    "expected_revision": recovering["revision"],
                    "idempotency_key": "retry-unknown-wrong-proof-33",
                    "target_id": TARGET,
                    "proof_digest": f"sha256:{'9' * 64}",
                },
            )
        assert current_target_status(state["operation"], TARGET) is ReleaseTargetStatus.UNKNOWN

    run_async(body)


def test_retrying_a_leg_that_never_timed_out_is_refused(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _published(ctx)
        recovering = moved(state["release"], ReleaseStatus.RECOVERING)
        with pytest.raises(DaemonValidationError, match="unsafe_release_retry"):
            await retry_target(
                ctx,
                {
                    "release": recovering,
                    "expected_revision": recovering["revision"],
                    "idempotency_key": "retry-in-flight-33",
                    "target_id": TARGET,
                    "proof_digest": PublicationOperation.model_validate(
                        state["operation"].model_dump(mode="json")
                    ).proof_digest,
                },
            )
        assert current_target_status(state["operation"], TARGET) is ReleaseTargetStatus.IN_FLIGHT

    run_async(body)


def test_reconcile_refuses_to_write_an_observed_status_over_an_unknown_leg(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        with pytest.raises(DaemonValidationError, match="observer_only_status"):
            await reconcile(
                ctx,
                {
                    "release": state["release"],
                    "expected_revision": state["release"]["revision"],
                    "idempotency_key": "reconcile-observed-33",
                    "target_id": TARGET,
                    "status": ReleaseTargetStatus.OBSERVED_SUCCESS.value,
                    "effect_receipt_ref": EFFECT,
                },
            )

    run_async(body)


def test_reconcile_refuses_an_unconfigured_target(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        with pytest.raises(DaemonValidationError, match="configures no target"):
            await reconcile(
                ctx,
                {
                    "release": state["release"],
                    "expected_revision": state["release"]["revision"],
                    "idempotency_key": "reconcile-unconfigured-33",
                    "target_id": "not-a-target",
                    "status": ReleaseTargetStatus.UNKNOWN.value,
                },
            )

    run_async(body)


def test_the_release_key_is_the_ledger_scope(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        state = await _unknown(ctx)
        assert Release.model_validate(state["release"]).key == RELEASE_KEY

    run_async(body)
