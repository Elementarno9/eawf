"""The three external-effect ``release.*`` RPCs and their keying.

Under test: ``release.publish``, ``release.retry_target`` and
``release.reconcile`` requiring both ``expected_revision`` and
``idempotency_key``; a stale revision answering
``stale_release_revision``; a repeated identical call returning the
*original* receipt rather than publishing again; the same key with a
different payload answering ``idempotency_conflict``; and publish
handing back an operation reference at once, with every leg queued
rather than awaited, once the chokepoint preflight recomputes green.

Handlers are driven directly through the module-level coroutines, so the
tests need no live transport. The default signal producers do not exist
yet (every row reports ``unavailable``), so the green path patches the
probe registry -- which is also the honest statement that publishing is
gated on producers that land with later waves.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.publication import PublicationOperation, attempt_count, require_attempt
from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import load_release_config
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import publish, reconcile, retry_target
from eawf.workflow.release.ledger import ledger_path, record_operation, request_fingerprint
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN

pytestmark = pytest.mark.integration

SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"
PROOF_DIGEST = f"sha256:{'1' * 64}"
EFFECT = "receipt://target/effect"
OBSERVATION = "receipt://target/read-back"


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Drive one coroutine test body to completion."""
    asyncio.run(body())


@pytest.fixture
def green_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every readiness signal pass so the chokepoint recomputes green."""

    def passing(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, remediation="")

    monkeypatch.setattr(
        "eawf.workflow.verify.release_readiness.DEFAULT_RELEASE_PROBES",
        dict.fromkeys(ReleaseSignalName, passing),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return a method context bound to a tmp state root."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=state_path,
    )


def approved_payload(**overrides: Any) -> dict[str, Any]:
    """Return a serialized APPROVED ``0.7.0.dev1`` record."""
    record = Release(
        uid=UUID(int=27),
        key="REL-0.7.0.dev1",
        version="0.7.0.dev1",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.APPROVED,
        approval_ref="receipt://approval/dev1",
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=MANIFEST_DIGEST,
        revision=4,
    )
    return {**record.model_dump(mode="json"), **overrides}


def publish_params(**overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.publish`` params."""
    params: dict[str, Any] = {
        "release": approved_payload(),
        "expected_revision": 4,
        "idempotency_key": "publish-0.7.0.dev1-01",
        "approved_manifest_digest": MANIFEST_DIGEST,
        "proof_digest": PROOF_DIGEST,
    }
    params.update(overrides)
    return params


def dev1_config() -> Any:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def seed_legs(
    ctx: MethodContext,
    result: dict[str, Any],
    status: ReleaseTargetStatus,
) -> PublicationOperation:
    """Drive every leg of the published operation to *status* in the ledger.

    Publication adapters land with a later wave, so the leg outcomes an
    adapter would report are written straight into the ledger here.
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
        Path(ctx.state_path),
        operation,
        idempotency_key="seed-adapter-results-01",
        fingerprint=request_fingerprint("test.seed", {"status": status.value}),
        recorded_at=datetime.now(UTC) + timedelta(seconds=1),
        summary="seed adapter results",
    )


def recovering_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return the published record moved to RECOVERING."""
    published = Release.model_validate(result["release"])
    return {
        **published.model_dump(mode="json"),
        "status": ReleaseStatus.RECOVERING.value,
        "revision": published.revision + 1,
    }


# --- required keying -----------------------------------------------------


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_publish_requires_the_keying_fields(ctx: MethodContext, missing: str) -> None:
    params = publish_params()
    params.pop(missing)
    with pytest.raises(ValidationError):
        _run(lambda: publish(ctx, params))


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_retry_target_requires_the_keying_fields(ctx: MethodContext, missing: str) -> None:
    params = {
        "release": approved_payload(),
        "expected_revision": 4,
        "idempotency_key": "retry-0.7.0.dev1-01",
        "target_id": "pypi",
        "proof_digest": PROOF_DIGEST,
    }
    params.pop(missing)
    with pytest.raises(ValidationError):
        _run(lambda: retry_target(ctx, params))


@pytest.mark.parametrize("missing", ["expected_revision", "idempotency_key"])
def test_reconcile_requires_the_keying_fields(ctx: MethodContext, missing: str) -> None:
    params = {
        "release": approved_payload(),
        "expected_revision": 4,
        "idempotency_key": "reconcile-0.7.0.dev1-01",
        "target_id": "pypi",
        "observation_receipt_ref": OBSERVATION,
    }
    params.pop(missing)
    with pytest.raises(ValidationError):
        _run(lambda: reconcile(ctx, params))


# --- release.publish -----------------------------------------------------


def test_publish_returns_an_operation_reference_with_every_leg_queued(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        result = await publish(ctx, publish_params())
        assert result["replayed"] is False
        assert result["operation_ref"].startswith("operation://REL-0.7.0.dev1/")
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
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        await publish(ctx, publish_params())
        lines = ledger_path(Path(ctx.state_path)).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        envelope = json.loads(lines[0])
        assert envelope["id"] == "publish-0.7.0.dev1-01"
        assert envelope["kind"] == "release"
        assert envelope["scope_id"] == "REL-0.7.0.dev1"

    _run(body)


def test_publish_denies_approval_stale_when_the_chokepoint_reds(ctx: MethodContext) -> None:
    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="approval_stale"):
            await publish(ctx, publish_params())

    _run(body)


def test_publish_denies_approval_stale_when_the_manifest_was_repinned(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="approval_stale"):
            await publish(ctx, publish_params(approved_manifest_digest=f"sha256:{'d' * 64}"))

    _run(body)


def test_publish_refuses_a_stale_revision(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="stale_release_revision"):
            await publish(ctx, publish_params(expected_revision=3))

    _run(body)


def test_publish_refuses_without_an_on_disk_state_root(green_probes: None) -> None:
    daemonless = MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=1,
        protocol_version=PROTOCOL_VERSION,
        version="test",
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="on-disk state root"):
            await publish(daemonless, publish_params())

    _run(body)


# --- idempotency ---------------------------------------------------------


def test_repeated_identical_publish_returns_the_original_receipt(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        first = await publish(ctx, publish_params())
        second = await publish(ctx, publish_params())
        assert second["replayed"] is True
        assert second["operation"]["operation_id"] == first["operation"]["operation_id"]
        assert second["operation_ref"] == first["operation_ref"]
        lines = ledger_path(Path(ctx.state_path)).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

    _run(body)


def test_same_key_with_a_different_payload_conflicts(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        await publish(ctx, publish_params())
        with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
            await publish(ctx, publish_params(proof_digest=f"sha256:{'7' * 64}"))

    _run(body)


def test_a_replay_is_returned_even_when_the_revision_has_since_moved(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        first = await publish(ctx, publish_params())
        replay = await publish(ctx, publish_params())
        assert replay["replayed"] is True
        assert replay["operation"]["operation_id"] == first["operation"]["operation_id"]

    _run(body)


# --- release.retry_target ------------------------------------------------


def test_retry_target_requeues_one_leg_under_the_idempotency_proof(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_legs(ctx, published, ReleaseTargetStatus.REPORTED_FAILURE)
        result = await retry_target(
            ctx,
            {
                "release": recovering_payload(published),
                "expected_revision": Release.model_validate(published["release"]).revision + 1,
                "idempotency_key": "retry-0.7.0.dev1-01",
                "target_id": "pypi",
                "proof_digest": PROOF_DIGEST,
            },
        )
        operation = PublicationOperation.model_validate(result["operation"])
        assert attempt_count(operation, "pypi") == 2
        assert attempt_count(operation, "npm") == 1
        assert Release.model_validate(result["release"]).status is ReleaseStatus.PUBLISHING

    _run(body)


def test_retry_target_denies_unsafe_release_retry_on_a_changed_proof(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_legs(ctx, published, ReleaseTargetStatus.REPORTED_FAILURE)
        with pytest.raises(DaemonValidationError, match="unsafe_release_retry"):
            await retry_target(
                ctx,
                {
                    "release": recovering_payload(published),
                    "expected_revision": Release.model_validate(published["release"]).revision + 1,
                    "idempotency_key": "retry-0.7.0.dev1-02",
                    "target_id": "pypi",
                    "proof_digest": f"sha256:{'8' * 64}",
                },
            )

    _run(body)


def test_retry_target_refuses_when_no_operation_is_open(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="no publication operation is open"):
            await retry_target(
                ctx,
                {
                    "release": approved_payload(),
                    "expected_revision": 4,
                    "idempotency_key": "retry-0.7.0.dev1-03",
                    "target_id": "pypi",
                    "proof_digest": PROOF_DIGEST,
                },
            )

    _run(body)


# --- release.reconcile ---------------------------------------------------


def test_reconcile_settles_one_leg_as_observed(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_legs(ctx, published, ReleaseTargetStatus.REPORTED_SUCCESS)
        result = await reconcile(
            ctx,
            {
                "release": published["release"],
                "expected_revision": Release.model_validate(published["release"]).revision,
                "idempotency_key": "reconcile-0.7.0.dev1-01",
                "target_id": "pypi",
                "observation_receipt_ref": OBSERVATION,
            },
        )
        operation = PublicationOperation.model_validate(result["operation"])
        row = require_attempt(operation, "pypi")
        assert row.status is ReleaseTargetStatus.OBSERVED_SUCCESS
        assert row.observation_receipt_ref == OBSERVATION
        reconciled = Release.model_validate(result["release"])
        assert reconciled.target_statuses["pypi"] is ReleaseTargetStatus.OBSERVED_SUCCESS

    _run(body)


def test_reconcile_records_a_mismatch_without_claiming_success(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_legs(ctx, published, ReleaseTargetStatus.REPORTED_SUCCESS)
        result = await reconcile(
            ctx,
            {
                "release": published["release"],
                "expected_revision": Release.model_validate(published["release"]).revision,
                "idempotency_key": "reconcile-0.7.0.dev1-02",
                "target_id": "pypi",
                "observation_receipt_ref": OBSERVATION,
                "observation_matched": False,
            },
        )
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, "pypi").status is ReleaseTargetStatus.OBSERVED_MISMATCH

    _run(body)


def test_reconcile_refuses_an_unconfigured_target(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_legs(ctx, published, ReleaseTargetStatus.REPORTED_SUCCESS)
        with pytest.raises(DaemonValidationError, match="configures no target"):
            await reconcile(
                ctx,
                {
                    "release": published["release"],
                    "expected_revision": Release.model_validate(published["release"]).revision,
                    "idempotency_key": "reconcile-0.7.0.dev1-03",
                    "target_id": "crates",
                    "observation_receipt_ref": OBSERVATION,
                },
            )

    _run(body)


def test_reconcile_refuses_a_stale_revision(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_legs(ctx, published, ReleaseTargetStatus.REPORTED_SUCCESS)
        with pytest.raises(DaemonValidationError, match="stale_release_revision"):
            await reconcile(
                ctx,
                {
                    "release": published["release"],
                    "expected_revision": 99,
                    "idempotency_key": "reconcile-0.7.0.dev1-04",
                    "target_id": "pypi",
                    "observation_receipt_ref": OBSERVATION,
                },
            )

    _run(body)
