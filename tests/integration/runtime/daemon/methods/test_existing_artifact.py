"""REL-024: the artifact that is already on the target.

Under test: a re-run finding the identical artifact already published
settling ``observed_success`` with no second upload; a re-run finding a
*different* artifact under the same name settling ``observed_mismatch``
and routing the release to ``RECOVERING``; and the retry budget's two
ends -- the first retry admitted and the last one admitted, with the
next attempt denied ``retry_limit_exhausted``.

This is the case a retry is for and the case a retry is dangerous in,
and the two look identical from the publisher's side: the registry says
"that version exists". Only the read-back can tell them apart, so the
counting publisher here proves the discrimination costs no second
upload -- the leg is settled from the ledger plus one observation, never
by publishing again to see what happens.

The counting publisher stands in for an upload no adapter performs yet
(:data:`~eawf.workflow.release.adapters.DEFAULT_REGISTRY_READERS` reports
every registry unreachable), so what it counts is the dispatch the
ledger owes: one external call per queued attempt row, and none for a
row the ledger already shows in flight.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.spec.publication import PublicationOperation, attempt_count, require_attempt
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import observe, publish
from eawf.workflow.release.observation import configured_target
from eawf.workflow.release.target_machine import (
    TargetDenialCode,
    TargetTransitionError,
    advance_target_attempt,
    current_target_status,
    open_target_attempt,
    retry_budget_remaining,
)
from tests.integration.runtime.daemon.methods.conftest import (
    EFFECT,
    CountingPublisher,
    dev1_config,
    dispatch_queued,
    manifest_payload,
    publish_params,
    record_snapshot,
    response_payload,
    run_async,
)

pytestmark = pytest.mark.integration

TARGET = "pypi"


def _reported(operation: PublicationOperation, *, target_id: str) -> PublicationOperation:
    """Settle *target_id* on the adapter's own word of success."""
    return advance_target_attempt(
        operation,
        target=configured_target(dev1_config(), target_id),
        to=ReleaseTargetStatus.REPORTED_SUCCESS,
        now=require_attempt(operation, target_id).deadline_at,
        effect_receipt_ref=EFFECT,
    )


async def _uploaded(ctx: MethodContext, publisher: CountingPublisher) -> dict[str, Any]:
    """Publish once, dispatch every leg, and take the adapter's word."""
    result = await publish(ctx, publish_params())
    operation = dispatch_queued(PublicationOperation.model_validate(result["operation"]), publisher)
    operation = _reported(operation, target_id=TARGET)
    record_snapshot(ctx, operation, key="seed-uploaded-33")
    return {"release": result["release"], "operation": operation}


def _observe_params(state: dict[str, Any], case: str, **overrides: Any) -> dict[str, Any]:
    """Return ``release.observe_target`` params for the *case* fixture."""
    params: dict[str, Any] = {
        "release": state["release"],
        "expected_revision": state["release"]["revision"],
        "idempotency_key": f"observe-existing-{case}-33",
        "target_id": TARGET,
        "manifest": manifest_payload(),
        "response": response_payload(TARGET, case),
    }
    params.update(overrides)
    return params


# --- the artifact is already there ---------------------------------------


def test_an_identical_artifact_already_present_is_observed_without_a_second_upload(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        state = await _uploaded(ctx, publisher)
        assert publisher.calls_for(TARGET) == 1

        result = await observe(ctx, _observe_params(state, "match"))
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, TARGET).status is ReleaseTargetStatus.OBSERVED_SUCCESS
        assert result["observation"]["result"] == "match"
        assert publisher.calls_for(TARGET) == 1
        assert attempt_count(operation, TARGET) == 1

    run_async(body)


@pytest.mark.parametrize("case", ("mismatch", "missing"))
def test_a_different_artifact_under_the_same_name_recovers_without_a_second_upload(
    ctx: MethodContext, green_probes: None, case: str
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        state = await _uploaded(ctx, publisher)

        result = await observe(ctx, _observe_params(state, case))
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, TARGET).status is ReleaseTargetStatus.OBSERVED_MISMATCH
        assert Release.model_validate(result["release"]).status is ReleaseStatus.RECOVERING
        assert publisher.calls_for(TARGET) == 1

    run_async(body)


def test_re_dispatching_an_already_uploaded_leg_makes_no_second_call(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        state = await _uploaded(ctx, publisher)
        before = publisher.per_target()
        dispatch_queued(state["operation"], publisher)
        assert publisher.per_target() == before

    run_async(body)


def test_observing_the_same_leg_twice_replays_the_original_receipt(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        state = await _uploaded(ctx, publisher)
        first = await observe(ctx, _observe_params(state, "match"))
        second = await observe(ctx, _observe_params(state, "match"))
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["observation"] is None
        assert second["operation"] == first["operation"]
        assert publisher.calls_for(TARGET) == 1

    run_async(body)


def test_an_observed_leg_is_terminal_and_cannot_be_published_again(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        state = await _uploaded(ctx, publisher)
        result = await observe(ctx, _observe_params(state, "match"))
        operation = PublicationOperation.model_validate(result["operation"])
        target = configured_target(dev1_config(), TARGET)
        with pytest.raises(TargetTransitionError) as err:
            open_target_attempt(
                operation,
                target=target,
                now=datetime.now(UTC),
                request_digest=require_attempt(operation, TARGET).request_digest,
            )
        assert err.value.code is TargetDenialCode.ILLEGAL_TARGET_TRANSITION
        assert publisher.calls_for(TARGET) == 1

    run_async(body)


def test_observing_an_unconfigured_target_is_refused(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await _uploaded(ctx, CountingPublisher())
        with pytest.raises(DaemonValidationError, match="configures no target"):
            await observe(ctx, _observe_params(state, "match", target_id="not-a-target"))

    run_async(body)


# --- the retry budget's two ends -----------------------------------------


def test_the_first_retry_and_the_last_retry_hit_the_limit_exactly(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        result = await publish(ctx, publish_params())
        operation = PublicationOperation.model_validate(result["operation"])
        target = configured_target(dev1_config(), TARGET)
        digest = require_attempt(operation, TARGET).request_digest

        for attempt in range(1, 2 + target.retry_limit):
            operation = dispatch_queued(operation, publisher)
            assert attempt_count(operation, TARGET) == attempt
            assert publisher.calls_for(TARGET) == attempt
            operation = advance_target_attempt(
                operation,
                target=target,
                to=ReleaseTargetStatus.REPORTED_FAILURE,
                now=require_attempt(operation, TARGET).deadline_at,
                effect_receipt_ref=EFFECT,
            )
            if attempt > target.retry_limit:
                break
            assert retry_budget_remaining(operation, target)
            operation = open_target_attempt(
                operation,
                target=target,
                now=require_attempt(operation, TARGET).deadline_at,
                request_digest=digest,
            )

        assert attempt_count(operation, TARGET) == 1 + target.retry_limit
        assert publisher.calls_for(TARGET) == 1 + target.retry_limit
        assert not retry_budget_remaining(operation, target)
        with pytest.raises(TargetTransitionError) as err:
            open_target_attempt(
                operation, target=target, now=datetime.now(UTC), request_digest=digest
            )
        assert err.value.code is TargetDenialCode.RETRY_LIMIT_EXHAUSTED
        assert current_target_status(operation, TARGET) is ReleaseTargetStatus.REPORTED_FAILURE

    run_async(body)


def test_a_target_with_no_retry_budget_gets_exactly_one_attempt(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        result = await publish(ctx, publish_params())
        operation = dispatch_queued(
            PublicationOperation.model_validate(result["operation"]), publisher
        )
        target = configured_target(dev1_config(), TARGET).model_copy(update={"retry_limit": 0})
        operation = advance_target_attempt(
            operation,
            target=target,
            to=ReleaseTargetStatus.REPORTED_FAILURE,
            now=require_attempt(operation, TARGET).deadline_at,
            effect_receipt_ref=EFFECT,
        )
        assert not retry_budget_remaining(operation, target)
        with pytest.raises(TargetTransitionError) as err:
            open_target_attempt(
                operation,
                target=target,
                now=datetime.now(UTC),
                request_digest=require_attempt(operation, TARGET).request_digest,
            )
        assert err.value.code is TargetDenialCode.RETRY_LIMIT_EXHAUSTED
        assert publisher.calls_for(TARGET) == 1

    run_async(body)
