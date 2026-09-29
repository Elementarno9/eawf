"""DEL-017 and REL-014 through the daemon verbs that approve and publish.

``release.approve`` records the frozen inputs on the stored record, and
``release.publish`` voids an approval whose inputs moved before it opens
any publication: the record goes back to DRAFT with its typed cause, is
persisted, and the call is refused naming that cause.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseInvalidationCause,
    ReleaseStatus,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import approve, compute_readiness_method, publish
from eawf.workflow.release.ledger import read_ledger
from eawf.workflow.release.records import read_release_record
from tests.integration.runtime.daemon.methods.conftest import (
    PROOF_DIGEST,
    RELEASE_KEY,
    SOURCE_SHA,
    TREE_SHA,
    manifest_digest,
    run_async,
)

pytestmark = pytest.mark.integration

OTHER_PROOF = f"sha256:{'2' * 64}"
APPROVAL_REF = "receipt://approval/dev1"


def _candidate() -> dict[str, Any]:
    record = Release(
        uid=UUID(int=135),
        key=RELEASE_KEY,
        version="0.7.0.dev1",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.CANDIDATE,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=manifest_digest(),
        revision=3,
    )
    return record.model_dump(mode="json")


def _approved(ctx: MethodContext) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def body() -> None:
        swept = await compute_readiness_method(ctx, {"version": "0.7.0.dev1"})
        captured.update(
            await approve(
                ctx,
                {
                    "release": _candidate(),
                    "readiness": swept["readiness"],
                    "approval_ref": APPROVAL_REF,
                    "proof_digest": PROOF_DIGEST,
                },
            )
        )

    run_async(body)
    release: dict[str, Any] = captured["release"]
    return release


def _publish(ctx: MethodContext, release: dict[str, Any], *, proof: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def body() -> None:
        captured.update(
            await publish(
                ctx,
                {
                    "release": release,
                    "expected_revision": release["revision"],
                    "idempotency_key": f"publish-{proof[-4:]}",
                    "approved_manifest_digest": manifest_digest(),
                    "proof_digest": proof,
                },
            )
        )

    run_async(body)
    return captured


@pytest.mark.usefixtures("green_probes")
def test_del_017_approve_records_the_frozen_inputs(ctx: MethodContext) -> None:
    approved = _approved(ctx)
    frozen = approved["approved_inputs"]
    assert frozen["proof_digest"] == PROOF_DIGEST
    assert frozen["source_sha"] == SOURCE_SHA
    assert frozen["tag"] == "v0.7.0.dev1"
    assert frozen["target_ids"] == ["github", "npm", "pypi"]
    stored = read_release_record(Path(str(ctx.state_path)), RELEASE_KEY)
    assert stored is not None
    assert stored.approved_inputs is not None
    assert stored.approved_inputs.proof_digest == PROOF_DIGEST


def test_del_017_approve_refuses_a_request_naming_no_proof(ctx: MethodContext) -> None:
    async def body() -> None:
        with pytest.raises(Exception, match="proof_digest"):
            await approve(
                ctx, {"release": _candidate(), "readiness": {}, "approval_ref": APPROVAL_REF}
            )

    run_async(body)


@pytest.mark.usefixtures("green_probes")
def test_rel_014_publish_voids_an_approval_whose_proof_changed(ctx: MethodContext) -> None:
    approved = _approved(ctx)
    with pytest.raises(DaemonValidationError, match="approval_invalidated: artifact_changed"):
        _publish(ctx, approved, proof=OTHER_PROOF)
    state_path = Path(str(ctx.state_path))
    stored = read_release_record(state_path, RELEASE_KEY)
    assert stored is not None
    assert stored.status is ReleaseStatus.DRAFT
    assert stored.approval_ref is None
    assert stored.last_invalidation is not None
    assert stored.last_invalidation.cause is ReleaseInvalidationCause.ARTIFACT_CHANGED
    assert stored.last_invalidation.invalidated_approval_ref == APPROVAL_REF
    assert read_ledger(state_path) == {}


@pytest.mark.usefixtures("green_probes")
def test_rel_014_publish_voids_an_approval_whose_head_moved(ctx: MethodContext) -> None:
    approved = {**_approved(ctx), "source_sha": "e" * 40}
    with pytest.raises(DaemonValidationError, match="approval_invalidated: head_moved"):
        _publish(ctx, approved, proof=PROOF_DIGEST)


@pytest.mark.usefixtures("green_probes")
def test_rel_014_an_unchanged_approval_publishes(ctx: MethodContext) -> None:
    result = _publish(ctx, _approved(ctx), proof=PROOF_DIGEST)
    assert result["release"]["status"] == ReleaseStatus.PUBLISHING.value
