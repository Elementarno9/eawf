"""The waiver acknowledgement tier, reached through the release RPC surface.

The tier is only worth anything if an operator can get to it. Before the
waiver rows were threaded through the params, ``release.compute_readiness``
passed a bare ``waiver_count``: every counted waiver arrived with no
explanation attached, classified ``unexplained``, and stayed red with nothing
an acknowledgement could clear. These tests walk the whole ladder --
``unexplained`` -> ``awaiting_acknowledgement`` -> ``ready`` -> approved -- so
a regression that drops either param off the RPC surface reds here.

Every sweep runs under ``green_probes`` because the default producers report
``unavailable``; with the twelve signals green, ``ready`` is a function of the
waiver block alone, which is exactly what these tests are about.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.release.waiver import WaiverDisposition
from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import (
    approve,
    compute_readiness_method,
    publish,
)
from tests.integration.runtime.daemon.methods.conftest import (
    APPROVED_REVISION,
    PROOF_DIGEST,
    RELEASE_KEY,
    SOURCE_SHA,
    TREE_SHA,
    manifest_digest,
    publish_params,
    run_async,
)

VERSION = "0.7.0.dev1"

#: A fully explained waiver: scope, reason and protected principal all present.
EXPLAINED: dict[str, str] = {
    "scope": "P31-I01-W40",
    "reason": "the reproducible-build prover has no runner on this host",
    "protected_principal": "byte-identical rebuild from the pinned tree",
}

#: A second explained waiver, so reordering the block is observable.
OTHER: dict[str, str] = {
    "scope": "P31-I01-W41",
    "reason": "the external registry has no sandbox to read back from",
    "protected_principal": "independent observation of the published artifact",
}


def acknowledgement_of(waiver: dict[str, str], *, by: str = "operator") -> dict[str, str]:
    """Return the acknowledgement covering *waiver*.

    The pair is repeated verbatim rather than pointed at by index, which is
    the property :func:`test_acknowledgement_survives_a_reordered_waiver_block`
    exercises.
    """
    return {
        "scope": waiver["scope"],
        "protected_principal": waiver["protected_principal"],
        "acknowledged_by": by,
    }


def candidate_payload(**overrides: Any) -> dict[str, Any]:
    """Return a serialized CANDIDATE ``0.7.0.dev1`` record awaiting approval."""
    record = Release(
        uid=UUID(int=49),
        key=RELEASE_KEY,
        version=VERSION,
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.CANDIDATE,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=manifest_digest(),
        revision=APPROVED_REVISION - 1,
    )
    return {**record.model_dump(mode="json"), **overrides}


def sweep(ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Run ``release.compute_readiness`` and return its readiness block."""
    captured: dict[str, Any] = {}

    async def body() -> None:
        result = await compute_readiness_method(ctx, {"version": VERSION, **params})
        captured.update(result)

    run_async(body)
    readiness: dict[str, Any] = captured["readiness"]
    return readiness


@pytest.mark.usefixtures("green_probes")
def test_counted_waiver_without_rows_is_unexplained(ctx: MethodContext) -> None:
    """The pre-fix behaviour, pinned: a bare count is red and unclearable."""
    readiness = sweep(ctx, waiver_count=1)
    assert readiness["waiver_disposition"] == WaiverDisposition.UNEXPLAINED.value
    assert readiness["ready"] is False
    assert readiness["waivers"] == []


@pytest.mark.usefixtures("green_probes")
def test_supplied_waiver_rows_reach_awaiting_acknowledgement(ctx: MethodContext) -> None:
    """Supplying the rows moves the tier off ``unexplained``.

    This is the whole point of the fix: with no ``waivers`` param there was no
    way to reach this disposition from the RPC surface at all.
    """
    readiness = sweep(ctx, waivers=[EXPLAINED])
    assert readiness["waiver_disposition"] == WaiverDisposition.AWAITING_ACKNOWLEDGEMENT.value
    assert readiness["waiver_count"] == 1
    assert readiness["ready"] is False


@pytest.mark.usefixtures("green_probes")
def test_acknowledgement_transitions_the_checkpoint_to_ready(ctx: MethodContext) -> None:
    """The acknowledgement clears the explained waiver and the sweep goes green."""
    readiness = sweep(ctx, waivers=[EXPLAINED], acknowledgements=[acknowledgement_of(EXPLAINED)])
    assert readiness["waiver_disposition"] == WaiverDisposition.AWAITING_ACKNOWLEDGEMENT.value
    assert readiness["waiver_acknowledgements"][0]["acknowledged_by"] == "operator"
    assert readiness["ready"] is True


@pytest.mark.usefixtures("green_probes")
def test_one_acknowledgement_does_not_clear_two_waivers(ctx: MethodContext) -> None:
    """Off-by-one: the uncovered waiver still holds the sweep."""
    readiness = sweep(
        ctx, waivers=[EXPLAINED, OTHER], acknowledgements=[acknowledgement_of(EXPLAINED)]
    )
    assert readiness["ready"] is False
    readiness = sweep(
        ctx,
        waivers=[EXPLAINED, OTHER],
        acknowledgements=[acknowledgement_of(EXPLAINED), acknowledgement_of(OTHER)],
    )
    assert readiness["ready"] is True


@pytest.mark.usefixtures("green_probes")
def test_acknowledgement_survives_a_reordered_waiver_block(ctx: MethodContext) -> None:
    """The pair is matched by value, so row order cannot forge coverage.

    An index-keyed acknowledgement would go on reading as acknowledged while
    naming a different protection once the block is reordered.
    """
    acks = [acknowledgement_of(EXPLAINED), acknowledgement_of(OTHER)]
    forward = sweep(ctx, waivers=[EXPLAINED, OTHER], acknowledgements=acks)
    reversed_block = sweep(ctx, waivers=[OTHER, EXPLAINED], acknowledgements=acks)
    assert forward["ready"] is True
    assert reversed_block["ready"] is True


@pytest.mark.usefixtures("green_probes")
def test_count_larger_than_the_rows_stays_unexplained(ctx: MethodContext) -> None:
    """A waiver counted with nothing attached is red however many rows came."""
    readiness = sweep(ctx, waiver_count=2, waivers=[EXPLAINED])
    assert readiness["waiver_disposition"] == WaiverDisposition.UNEXPLAINED.value
    assert readiness["ready"] is False


@pytest.mark.usefixtures("green_probes")
def test_blank_field_makes_the_waiver_unexplained(ctx: MethodContext) -> None:
    """A row missing its protected principal explains nothing.

    The row is representable on purpose: refusing to construct it would only
    move the blindness out to a caller that drops the row and reports a count.
    """
    readiness = sweep(ctx, waivers=[{**EXPLAINED, "protected_principal": "  "}])
    assert readiness["waiver_disposition"] == WaiverDisposition.UNEXPLAINED.value
    assert readiness["ready"] is False


@pytest.mark.usefixtures("green_probes")
def test_no_waiver_reports_none_and_stays_ready(ctx: MethodContext) -> None:
    """Empty boundary: the default params carry no waiver block at all."""
    readiness = sweep(ctx)
    assert readiness["waiver_disposition"] == WaiverDisposition.NONE.value
    assert readiness["waivers"] == []
    assert readiness["ready"] is True


@pytest.mark.usefixtures("green_probes")
def test_acknowledgement_naming_no_counted_waiver_is_refused(ctx: MethodContext) -> None:
    """An acknowledgement of nothing accepts no loss, so the sweep is refused."""
    with pytest.raises(DaemonValidationError, match="no counted waiver"):
        sweep(ctx, waivers=[EXPLAINED], acknowledgements=[acknowledgement_of(OTHER)])


@pytest.mark.usefixtures("green_probes")
def test_negative_waiver_count_is_refused(ctx: MethodContext) -> None:
    """Out-of-range: a negative count is rejected before the sweep runs."""
    with pytest.raises(DaemonValidationError, match="must not be negative"):
        sweep(ctx, waiver_count=-1)


def test_unknown_waiver_field_is_refused(ctx: MethodContext) -> None:
    """Schema mismatch: the params model forbids extras on a nested row."""
    with pytest.raises(ValidationError):
        sweep(ctx, waivers=[{**EXPLAINED, "severity": "high"}])


def test_acknowledgement_missing_its_signer_is_refused(ctx: MethodContext) -> None:
    """Missing key: every acknowledgement names who accepted the loss."""
    with pytest.raises(ValidationError):
        sweep(
            ctx,
            waivers=[EXPLAINED],
            acknowledgements=[
                {k: v for k, v in acknowledgement_of(EXPLAINED).items() if k != "acknowledged_by"}
            ],
        )


def test_blank_acknowledgement_scope_is_refused(ctx: MethodContext) -> None:
    """An empty scope names no waiver; ``min_length=1`` rejects it."""
    with pytest.raises(ValidationError):
        sweep(
            ctx,
            waivers=[EXPLAINED],
            acknowledgements=[{**acknowledgement_of(EXPLAINED), "scope": ""}],
        )


@pytest.mark.usefixtures("green_probes")
def test_approve_denies_an_unacknowledged_waiver(ctx: MethodContext) -> None:
    """Approval reads the waiver block of the sweep it binds and denies on it."""
    readiness = sweep(ctx, waivers=[EXPLAINED])

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="release_not_ready") as caught:
            await approve(
                ctx,
                {
                    "release": candidate_payload(),
                    "readiness": readiness,
                    "approval_ref": "receipt://approval/dev1",
                },
            )
        assert "awaiting_acknowledgement" in str(caught.value)

    run_async(body)


@pytest.mark.usefixtures("green_probes")
def test_approve_accepts_an_acknowledged_waiver(ctx: MethodContext) -> None:
    """The acknowledged sweep carries the candidate through to APPROVED.

    The acknowledgement block travels inside ``readiness``, which the handler
    revalidates, so a forged ``ready`` on an unacknowledged block cannot get
    an approval through this path.
    """
    readiness = sweep(ctx, waivers=[EXPLAINED], acknowledgements=[acknowledgement_of(EXPLAINED)])
    captured: dict[str, Any] = {}

    async def body() -> None:
        result = await approve(
            ctx,
            {
                "release": candidate_payload(),
                "readiness": readiness,
                "approval_ref": "receipt://approval/dev1",
            },
        )
        captured.update(result)

    run_async(body)
    assert captured["release"]["status"] == ReleaseStatus.APPROVED.value
    assert captured["release"]["approval_ref"] == "receipt://approval/dev1"


@pytest.mark.usefixtures("green_probes")
def test_forged_ready_on_an_unacknowledged_sweep_is_refused(ctx: MethodContext) -> None:
    """``ready`` is derived, so flipping it in the payload does not buy an approval."""
    readiness = {**sweep(ctx, waivers=[EXPLAINED]), "ready": True}

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="readiness payload invalid"):
            await approve(
                ctx,
                {
                    "release": candidate_payload(),
                    "readiness": readiness,
                    "approval_ref": "receipt://approval/dev1",
                },
            )

    run_async(body)


@pytest.mark.usefixtures("green_probes")
def test_publish_replays_the_acknowledged_waiver_block(ctx: MethodContext) -> None:
    """The chokepoint recomputes the sweep, so it needs the same block.

    Without the rows the recomputed sweep classifies ``unexplained`` and denies
    ``approval_stale``, refusing the publication the approval already cleared.
    """
    params = publish_params(
        waivers=[EXPLAINED],
        acknowledgements=[acknowledgement_of(EXPLAINED)],
    )
    captured: dict[str, Any] = {}

    async def body() -> None:
        captured.update(await publish(ctx, params))

    run_async(body)
    assert captured["release"]["status"] == ReleaseStatus.PUBLISHING.value
    assert captured["replayed"] is False
    assert captured["release"]["manifest_digest"] == manifest_digest()
    assert params["proof_digest"] == PROOF_DIGEST


@pytest.mark.usefixtures("green_probes")
def test_publish_denies_when_the_waiver_block_is_dropped(ctx: MethodContext) -> None:
    """Counting the waiver without its rows reds the chokepoint, as it should."""

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="approval_stale"):
            await publish(ctx, publish_params(waiver_count=1))

    run_async(body)
