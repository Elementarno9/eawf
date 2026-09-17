"""The three external-effect ``release.*`` RPCs and their keying.

Under test: ``release.publish``, ``release.retry_target`` and
``release.reconcile`` requiring both ``expected_revision`` and
``idempotency_key``; a stale revision answering
``stale_release_revision``; a repeated identical call returning the
*original* receipt rather than publishing again; the same key with a
different payload answering ``idempotency_conflict``; and publish
handing back an operation reference at once, with every leg queued
rather than awaited, once the chokepoint preflight recomputes green.

``release.reconcile`` is under test at its narrowed contract: it records
what the adapter finally reported and refuses both ``observed_*``
statuses with ``observer_only_status``, because an independent read-back
is ``release.observe_target``'s job (see
:mod:`tests.integration.runtime.daemon.methods.test_observe_cli`).

Handlers are driven directly through the module-level coroutines, so the
tests need no live transport. No probe is patched: every publish sweeps
the dev1 checkout of the shared conftest at the commit its record pins,
so the green path here is the green path an operator gets. Two setups
still write the ledger directly -- a leg reported failed, and legs
dispatched without a receipt -- because no verb here produces either
from a queued leg.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.publication import PublicationOperation, attempt_count, require_attempt
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import publish, reconcile, retry_target, show
from eawf.workflow.release.ledger import ledger_path, record_operation, request_fingerprint
from eawf.workflow.release.records import read_release_records
from eawf.workflow.release.target_machine import advance_target_attempt
from tests.integration.runtime.daemon.methods.conftest import (
    DEV1_VERSION,
    EFFECT,
    PROOF_DIGEST,
    RELEASE_KEY,
    dev1_config,
    pinned_payload,
    pinned_publish_params,
)

pytestmark = pytest.mark.integration

OBSERVATION = "receipt://target/read-back"


def _run(body: Callable[[], Coroutine[Any, Any, object]]) -> None:
    """Drive one coroutine test body to completion."""
    asyncio.run(body())


def seed_legs(
    ctx: MethodContext,
    result: dict[str, Any],
    status: ReleaseTargetStatus,
) -> PublicationOperation:
    """Drive every leg of the published operation to *status* in the ledger.

    A receipt-bearing reconcile writes only what a publish job reports,
    so a leg that reported failure on every target at once is written
    straight into the ledger here.
    """
    config = dev1_config()
    operation = PublicationOperation.model_validate(result["operation"])
    for target in config.targets:
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
        operation = advance_target_attempt(
            operation,
            target=target,
            to=status,
            now=row.deadline_at,
            effect_receipt_ref=None if status is ReleaseTargetStatus.UNKNOWN else EFFECT,
        )
    return record_operation(
        Path(str(ctx.state_path)),
        operation,
        idempotency_key="seed-adapter-results-01",
        fingerprint=request_fingerprint("test.seed", {"status": status.value}),
        recorded_at=datetime.now(UTC) + timedelta(seconds=1),
        summary="seed adapter results",
    )


def seed_dispatch(ctx: MethodContext, result: dict[str, Any]) -> PublicationOperation:
    """Move every leg of the published operation to in_flight.

    An asserted status proves no dispatch, so the tests that reconcile
    by ``status`` seed only the dispatch and let the verb write the
    reported result.
    """
    config = dev1_config()
    operation = PublicationOperation.model_validate(result["operation"])
    for target in config.targets:
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
    return record_operation(
        Path(str(ctx.state_path)),
        operation,
        idempotency_key="seed-dispatch-01",
        fingerprint=request_fingerprint("test.seed", {"status": "in_flight"}),
        recorded_at=datetime.now(UTC) + timedelta(seconds=1),
        summary="seed dispatch",
    )


def recovering_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return the published record moved to RECOVERING."""
    published = Release.model_validate(result["release"])
    return {
        **published.model_dump(mode="json"),
        "status": ReleaseStatus.RECOVERING.value,
        "revision": published.revision + 1,
    }


def reconcile_params(published: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return asserted-status ``release.reconcile`` params for *published*."""
    params: dict[str, Any] = {
        "release": published["release"],
        "expected_revision": published["release"]["revision"],
        "idempotency_key": "reconcile-0.7.0.dev1-01",
        "target_id": "pypi",
        "status": ReleaseTargetStatus.REPORTED_SUCCESS.value,
        "effect_receipt_ref": EFFECT,
    }
    params.update(overrides)
    return params


def retry_params(published: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return ``release.retry_target`` params for *published*, moved to RECOVERING."""
    recovering = recovering_payload(published)
    params: dict[str, Any] = {
        "release": recovering,
        "expected_revision": recovering["revision"],
        "idempotency_key": "retry-0.7.0.dev1-01",
        "target_id": "pypi",
        "proof_digest": PROOF_DIGEST,
    }
    params.update(overrides)
    return params


def shown_status(ctx: MethodContext) -> str:
    """Return the status ``release.show`` reports for the dev1 rung."""

    async def ask() -> str:
        status: str = (await show(ctx, {"version": DEV1_VERSION}))["record"]["status"]
        return status

    return asyncio.run(ask())


# --- required keying -----------------------------------------------------


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_publish_requires_the_keying_fields(
    walk_ctx: MethodContext, dev1_checkout: Path, missing: str
) -> None:
    params = pinned_publish_params(dev1_checkout)
    params.pop(missing)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await publish(walk_ctx, params)

    _run(body)


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_retry_target_requires_the_keying_fields(
    walk_ctx: MethodContext, dev1_checkout: Path, missing: str
) -> None:
    params = {
        "release": pinned_payload(dev1_checkout),
        "expected_revision": 4,
        "idempotency_key": "retry-0.7.0.dev1-01",
        "target_id": "pypi",
        "proof_digest": PROOF_DIGEST,
    }
    params.pop(missing)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await retry_target(walk_ctx, params)

    _run(body)


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_reconcile_requires_the_keying_fields(
    walk_ctx: MethodContext, dev1_checkout: Path, missing: str
) -> None:
    params = {
        "release": pinned_payload(dev1_checkout),
        "expected_revision": 4,
        "idempotency_key": "reconcile-0.7.0.dev1-01",
        "target_id": "pypi",
        "status": ReleaseTargetStatus.REPORTED_SUCCESS.value,
    }
    params.pop(missing)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await reconcile(walk_ctx, params)

    _run(body)


# --- release.publish -----------------------------------------------------


def test_publish_returns_an_operation_reference_with_every_leg_queued(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        result = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        assert result["replayed"] is False
        assert result["operation_ref"].startswith(f"operation://{RELEASE_KEY}/")
        published = Release.model_validate(result["release"])
        assert published.status is ReleaseStatus.PUBLISHING
        assert published.publication_operation_ref == result["operation_ref"]
        operation = PublicationOperation.model_validate(result["operation"])
        assert set(operation.target_ids) == {"pypi", "npm", "github"}
        assert all(
            require_attempt(operation, target).status is ReleaseTargetStatus.QUEUED
            for target in operation.target_ids
        )

    _run(body)


def test_publish_appends_one_ledger_envelope_keyed_by_the_idempotency_key(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        params = pinned_publish_params(dev1_checkout)
        await publish(walk_ctx, params)
        lines = ledger_path(Path(str(walk_ctx.state_path))).read_text(encoding="utf-8")
        envelopes = [json.loads(line) for line in lines.splitlines()]
        assert len(envelopes) == 1
        assert envelopes[0]["id"] == params["idempotency_key"]
        assert envelopes[0]["kind"] == "release"
        assert envelopes[0]["scope_id"] == RELEASE_KEY

    _run(body)


def test_publish_records_the_publishing_record(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        result = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        stored = read_release_records(Path(str(walk_ctx.state_path)))
        assert stored[RELEASE_KEY].model_dump(mode="json") == result["release"]

    _run(body)
    assert shown_status(walk_ctx) == ReleaseStatus.PUBLISHING.value


def test_publish_denies_approval_stale_when_the_chokepoint_reds(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    (dev1_checkout / "stray.txt").write_text("uncommitted\n", encoding="utf-8")

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="approval_stale"):
            await publish(walk_ctx, pinned_publish_params(dev1_checkout))

    _run(body)
    assert not ledger_path(Path(str(walk_ctx.state_path))).exists()


def test_publish_denies_approval_stale_when_the_manifest_was_repinned(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        params = pinned_publish_params(dev1_checkout, approved_manifest_digest=f"sha256:{'d' * 64}")
        with pytest.raises(DaemonValidationError, match="approval_stale"):
            await publish(walk_ctx, params)

    _run(body)


def test_publish_refuses_a_stale_revision(walk_ctx: MethodContext, dev1_checkout: Path) -> None:
    async def body() -> None:
        params = pinned_publish_params(dev1_checkout, expected_revision=3)
        with pytest.raises(DaemonValidationError, match="stale_release_revision"):
            await publish(walk_ctx, params)

    _run(body)


def test_publish_refuses_without_an_on_disk_state_root(dev1_checkout: Path) -> None:
    daemonless = MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=1,
        protocol_version=PROTOCOL_VERSION,
        version="test",
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="on-disk state root"):
            await publish(daemonless, pinned_publish_params(dev1_checkout))

    _run(body)


# --- idempotency ---------------------------------------------------------


def test_repeated_identical_publish_returns_the_original_receipt(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        params = pinned_publish_params(dev1_checkout)
        first = await publish(walk_ctx, params)
        second = await publish(walk_ctx, params)
        assert second["replayed"] is True
        assert second["operation"]["operation_id"] == first["operation"]["operation_id"]
        assert second["operation_ref"] == first["operation_ref"]
        assert second["release"] == first["release"]
        state_path = Path(str(walk_ctx.state_path))
        assert len(ledger_path(state_path).read_text(encoding="utf-8").splitlines()) == 1
        assert len(read_release_records(state_path)) == 1

    _run(body)


def test_same_key_with_a_different_payload_conflicts(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
            await publish(
                walk_ctx, pinned_publish_params(dev1_checkout, proof_digest=f"sha256:{'7' * 64}")
            )

    _run(body)


def test_a_replay_is_returned_even_when_the_revision_has_since_moved(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        first = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        moved_on = first["release"]
        replay = await publish(
            walk_ctx,
            pinned_publish_params(
                dev1_checkout, release=moved_on, expected_revision=moved_on["revision"]
            ),
        )
        assert replay["replayed"] is True
        assert replay["operation"]["operation_id"] == first["operation"]["operation_id"]
        assert replay["release"] == moved_on

    _run(body)


# --- release.retry_target ------------------------------------------------


def test_retry_target_requeues_one_leg_under_the_idempotency_proof(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_legs(walk_ctx, published, ReleaseTargetStatus.REPORTED_FAILURE)
        result = await retry_target(walk_ctx, retry_params(published))
        operation = PublicationOperation.model_validate(result["operation"])
        assert attempt_count(operation, "pypi") == 2
        assert attempt_count(operation, "npm") == 1
        assert Release.model_validate(result["release"]).status is ReleaseStatus.PUBLISHING
        stored = read_release_records(Path(str(walk_ctx.state_path)))[RELEASE_KEY]
        assert stored.model_dump(mode="json") == result["release"]

    _run(body)


def test_retry_target_denies_unsafe_release_retry_on_a_changed_proof(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_legs(walk_ctx, published, ReleaseTargetStatus.REPORTED_FAILURE)
        params = retry_params(
            published,
            idempotency_key="retry-0.7.0.dev1-02",
            proof_digest=f"sha256:{'8' * 64}",
        )
        with pytest.raises(DaemonValidationError, match="unsafe_release_retry"):
            await retry_target(walk_ctx, params)

    _run(body)
    assert shown_status(walk_ctx) == ReleaseStatus.PUBLISHING.value


def test_retry_target_refuses_when_no_operation_is_open(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no publication operation is open"):
            await retry_target(
                walk_ctx,
                {
                    "release": pinned_payload(dev1_checkout),
                    "expected_revision": 4,
                    "idempotency_key": "retry-0.7.0.dev1-03",
                    "target_id": "pypi",
                    "proof_digest": PROOF_DIGEST,
                },
            )

    _run(body)


# --- release.reconcile ---------------------------------------------------


def test_reconcile_settles_one_leg_as_reported(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_dispatch(walk_ctx, published)
        result = await reconcile(walk_ctx, reconcile_params(published))
        operation = PublicationOperation.model_validate(result["operation"])
        row = require_attempt(operation, "pypi")
        assert row.status is ReleaseTargetStatus.REPORTED_SUCCESS
        assert row.effect_receipt_ref == EFFECT
        assert row.observation_receipt_ref is None
        reconciled = Release.model_validate(result["release"])
        assert reconciled.target_statuses["pypi"] is ReleaseTargetStatus.REPORTED_SUCCESS
        assert reconciled.status is ReleaseStatus.PUBLISHING

    _run(body)


def test_reconcile_records_a_failure_without_claiming_success(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_dispatch(walk_ctx, published)
        result = await reconcile(
            walk_ctx,
            reconcile_params(
                published,
                idempotency_key="reconcile-0.7.0.dev1-02",
                status=ReleaseTargetStatus.REPORTED_FAILURE.value,
            ),
        )
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, "pypi").status is ReleaseTargetStatus.REPORTED_FAILURE

    _run(body)


def test_reconcile_opens_verification_once_every_asserted_leg_reported(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_dispatch(walk_ctx, published)
        result = published
        for target_id in ("pypi", "npm", "github"):
            result = await reconcile(
                walk_ctx,
                reconcile_params(
                    result, idempotency_key=f"reconcile-all-{target_id}", target_id=target_id
                ),
            )
        assert Release.model_validate(result["release"]).status is ReleaseStatus.VERIFYING

    _run(body)
    assert shown_status(walk_ctx) == ReleaseStatus.VERIFYING.value


@pytest.mark.parametrize(
    "status",
    (
        ReleaseTargetStatus.OBSERVED_SUCCESS.value,
        ReleaseTargetStatus.OBSERVED_MISMATCH.value,
    ),
)
def test_reconcile_refuses_to_write_an_observed_status(
    walk_ctx: MethodContext, dev1_checkout: Path, status: str
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_legs(walk_ctx, published, ReleaseTargetStatus.REPORTED_SUCCESS)
        params = reconcile_params(
            published, idempotency_key=f"reconcile-observed-{status}", status=status
        )
        params.pop("effect_receipt_ref")
        with pytest.raises(DaemonValidationError, match="observer_only_status"):
            await reconcile(walk_ctx, params)

    _run(body)


def test_reconcile_no_longer_accepts_an_observation_receipt(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_dispatch(walk_ctx, published)
        params = reconcile_params(
            published,
            idempotency_key="reconcile-0.7.0.dev1-05",
            observation_receipt_ref=OBSERVATION,
        )
        with pytest.raises(ValidationError, match="observation_receipt_ref"):
            await reconcile(walk_ctx, params)

    _run(body)


def test_reconcile_refuses_an_unconfigured_target(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_dispatch(walk_ctx, published)
        params = reconcile_params(
            published, idempotency_key="reconcile-0.7.0.dev1-03", target_id="crates"
        )
        with pytest.raises(DaemonValidationError, match="configures no target"):
            await reconcile(walk_ctx, params)

    _run(body)


def test_reconcile_refuses_a_stale_revision(walk_ctx: MethodContext, dev1_checkout: Path) -> None:
    async def body() -> None:
        published = await publish(walk_ctx, pinned_publish_params(dev1_checkout))
        seed_dispatch(walk_ctx, published)
        params = reconcile_params(
            published, idempotency_key="reconcile-0.7.0.dev1-04", expected_revision=99
        )
        with pytest.raises(DaemonValidationError, match="stale_release_revision"):
            await reconcile(walk_ctx, params)

    _run(body)
