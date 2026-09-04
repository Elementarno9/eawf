"""``release.reconcile`` settles a leg from the publish job's own receipt.

The publisher is the tag-push pipeline, so the ledger cannot ask a leg
what it did -- the runner is gone by the time anyone asks. Each publish
job therefore uploads ``publication-receipt-<target>.json`` and
reconciliation reads it back.

Two properties are load-bearing here and both are asserted over recorded
receipts rather than a live pipeline. First, the mapping is *total over
the reported statuses and reaches nothing else*: a green job is
``reported_success``, a failed job is ``reported_failure``, and every
other conclusion -- cancelled, timed out, or a word GitHub has not
invented yet -- is ``unknown``, which is retryable rather than burnt.
Second, no receipt can produce either ``observed_*`` status, because a
receipt is the publisher's word about its own call and an independent
read-back is :func:`~eawf.runtime.daemon.methods.release.observe`'s job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import Release, ReleaseTargetStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import publish, reconcile
from eawf.workflow.release.publication import RECONCILABLE_TARGET_STATUSES
from eawf.workflow.release.publication_receipt import (
    PUBLICATION_RECEIPT_TEMPLATE,
    PublicationReceipt,
    load_receipt,
    read_receipts,
    receipt_filename,
    reported_status,
)
from tests.integration.runtime.daemon.methods.conftest import (
    CountingPublisher,
    dispatch_queued,
    publish_params,
    record_snapshot,
    run_async,
)

pytestmark = pytest.mark.integration

RUN_ID = "17482930165"


def receipt_payload(**overrides: Any) -> dict[str, Any]:
    """Return a well-formed ``pypi`` publication receipt as JSON."""
    payload: dict[str, Any] = {
        "target_id": "pypi",
        "version": "0.7.0.dev1",
        "artifact_digests": {"eawf-0.7.0.dev1-py3-none-any.whl": f"sha256:{'1' * 64}"},
        "job_conclusion": "success",
        "run_id": RUN_ID,
    }
    payload.update(overrides)
    return payload


def seed_dispatch(ctx: MethodContext, result: dict[str, Any]) -> None:
    """Move every leg of the published operation to in_flight."""
    record_snapshot(
        ctx,
        dispatch_queued(operation_of(result), CountingPublisher()),
        key="seed-dispatch-receipts",
    )


def operation_of(result: dict[str, Any]) -> PublicationOperation:
    """Return the operation snapshot a publication verb answered with."""
    return PublicationOperation.model_validate(result["operation"])


def reconcile_params(published: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return ``release.reconcile`` params keyed to the published record."""
    params: dict[str, Any] = {
        "release": published["release"],
        "expected_revision": Release.model_validate(published["release"]).revision,
        "idempotency_key": "reconcile-receipt-01",
        "target_id": "pypi",
        "receipt": receipt_payload(),
    }
    params.update(overrides)
    return params


# --- the receipt -> status mapping ------------------------------------------


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("success", ReleaseTargetStatus.REPORTED_SUCCESS),
        ("failure", ReleaseTargetStatus.REPORTED_FAILURE),
        ("cancelled", ReleaseTargetStatus.UNKNOWN),
        ("timed_out", ReleaseTargetStatus.UNKNOWN),
        ("skipped", ReleaseTargetStatus.UNKNOWN),
        ("neutral", ReleaseTargetStatus.UNKNOWN),
        ("SUCCESS", ReleaseTargetStatus.REPORTED_SUCCESS),
        (" failure ", ReleaseTargetStatus.REPORTED_FAILURE),
    ],
)
def test_reported_status_maps_every_conclusion(
    conclusion: str,
    expected: ReleaseTargetStatus,
) -> None:
    """Green is success, failed is failure, and everything else is unknown."""
    receipt = PublicationReceipt.model_validate(receipt_payload(job_conclusion=conclusion))
    assert reported_status(receipt) == expected


def test_reported_status_reads_an_absent_receipt_as_unknown() -> None:
    """A job that left no receipt never told us, which is not a failure."""
    assert reported_status(None) is ReleaseTargetStatus.UNKNOWN


@pytest.mark.parametrize(
    "conclusion",
    ["success", "failure", "cancelled", "timed_out", "action_required", ""],
)
def test_reported_status_never_reaches_an_observed_state(conclusion: str) -> None:
    """No conclusion, however spelled, promotes a self-report to a read-back."""
    payload = receipt_payload(job_conclusion=conclusion or "x")
    status = reported_status(PublicationReceipt.model_validate(payload))
    assert status in RECONCILABLE_TARGET_STATUSES


# --- receipt validation -----------------------------------------------------


def test_load_receipt_accepts_a_well_formed_body() -> None:
    """The happy path returns a typed record carrying every declared field."""
    receipt = load_receipt(receipt_payload())
    assert receipt.target_id == "pypi"
    assert receipt.run_id == RUN_ID
    assert set(receipt.artifact_digests) == {"eawf-0.7.0.dev1-py3-none-any.whl"}


@pytest.mark.parametrize("missing", ["target_id", "version", "job_conclusion", "run_id"])
def test_load_receipt_refuses_a_body_missing_a_required_field(missing: str) -> None:
    """A receipt without one of its four facts is a broken publisher."""
    payload = receipt_payload()
    payload.pop(missing)
    with pytest.raises(ValueError, match="does not validate"):
        load_receipt(payload)


@pytest.mark.parametrize("blank", ["target_id", "version", "job_conclusion", "run_id"])
def test_load_receipt_refuses_an_empty_required_field(blank: str) -> None:
    """An empty string is the boundary a min_length constraint exists for."""
    with pytest.raises(ValueError, match="does not validate"):
        load_receipt(receipt_payload(**{blank: ""}))


def test_load_receipt_refuses_an_unknown_field() -> None:
    """``extra='forbid'``: a field nobody declared is a drifted producer."""
    with pytest.raises(ValueError, match="does not validate"):
        load_receipt(receipt_payload(published_at="2026-09-04T00:00:00Z"))


def test_load_receipt_refuses_a_non_mapping_body() -> None:
    """A JSON list where an object belongs never reaches the status map."""
    with pytest.raises(ValueError, match="does not validate"):
        load_receipt([receipt_payload()])


def test_load_receipt_accepts_an_empty_digest_map() -> None:
    """A job that failed before uploading anything still reports itself."""
    receipt = load_receipt(receipt_payload(artifact_digests={}, job_conclusion="failure"))
    assert receipt.artifact_digests == {}
    assert reported_status(receipt) is ReleaseTargetStatus.REPORTED_FAILURE


# --- receipt naming + directory reads ---------------------------------------


@pytest.mark.parametrize("target_id", ["pypi", "npm", "github"])
def test_receipt_filename_is_one_file_per_leg(target_id: str) -> None:
    """Three jobs uploading in parallel cannot clobber one file."""
    assert receipt_filename(target_id) == f"publication-receipt-{target_id}.json"
    assert receipt_filename(target_id) == PUBLICATION_RECEIPT_TEMPLATE.format(target_id=target_id)


def test_receipt_filename_refuses_an_empty_target() -> None:
    """An empty leg id would collapse every receipt onto one name."""
    with pytest.raises(ValueError, match="must be non-empty"):
        receipt_filename("")


def test_read_receipts_returns_nothing_from_an_empty_download(tmp_path: Path) -> None:
    """No job uploaded, so every leg is silent rather than failed."""
    assert read_receipts(tmp_path, ("pypi", "npm", "github")) == {}


def test_read_receipts_returns_only_the_legs_that_left_one(tmp_path: Path) -> None:
    """A partial download is the normal shape when one job is still running."""
    (tmp_path / receipt_filename("pypi")).write_text(
        json.dumps(receipt_payload()), encoding="utf-8"
    )
    found = read_receipts(tmp_path, ("pypi", "npm", "github"))
    assert set(found) == {"pypi"}
    assert reported_status(found.get("npm")) is ReleaseTargetStatus.UNKNOWN


def test_read_receipts_refuses_a_file_naming_another_leg(tmp_path: Path) -> None:
    """A receipt filed under the wrong name would settle the wrong leg."""
    (tmp_path / receipt_filename("npm")).write_text(json.dumps(receipt_payload()), encoding="utf-8")
    with pytest.raises(ValueError, match=r"reports target 'pypi', not 'npm'"):
        read_receipts(tmp_path, ("npm",))


def test_read_receipts_refuses_a_truncated_file(tmp_path: Path) -> None:
    """A half-written upload is a repair task, not an absent producer."""
    (tmp_path / receipt_filename("pypi")).write_text('{"target_id": "py', encoding="utf-8")
    with pytest.raises(ValueError, match="is not valid JSON"):
        read_receipts(tmp_path, ("pypi",))


def test_read_receipts_over_no_targets_reads_nothing(tmp_path: Path) -> None:
    """The empty target tuple is the boundary the loop must survive."""
    (tmp_path / receipt_filename("pypi")).write_text(
        json.dumps(receipt_payload()), encoding="utf-8"
    )
    assert read_receipts(tmp_path, ()) == {}


# --- release.reconcile over a downloaded receipt ----------------------------


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("success", ReleaseTargetStatus.REPORTED_SUCCESS),
        ("failure", ReleaseTargetStatus.REPORTED_FAILURE),
    ],
)
def test_reconcile_settles_the_leg_the_receipt_reports(
    ctx: MethodContext,
    green_probes: None,
    conclusion: str,
    expected: ReleaseTargetStatus,
) -> None:
    """The verb writes the status the job's own conclusion supports."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        result = await reconcile(
            ctx,
            reconcile_params(published, receipt=receipt_payload(job_conclusion=conclusion)),
        )
        row = require_attempt(operation_of(result), "pypi")
        assert row.status is expected
        assert row.observation_receipt_ref is None
        reconciled = Release.model_validate(result["release"])
        assert reconciled.target_statuses["pypi"] is expected

    run_async(body)


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
def test_reconcile_of_an_unknown_receipt_still_waits_out_the_deadline(
    ctx: MethodContext,
    green_probes: None,
    conclusion: str,
) -> None:
    """A receipt cannot shortcut the timeout the ``unknown`` edge is guarded by.

    The mapping says ``unknown``, but a leg still in flight inside its
    own deadline may yet report: settling it early would open a retry
    against a publication that is still running.
    """

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        with pytest.raises(DaemonValidationError, match="target_deadline_not_elapsed"):
            await reconcile(
                ctx,
                reconcile_params(published, receipt=receipt_payload(job_conclusion=conclusion)),
            )

    run_async(body)


def test_reconcile_points_the_effect_receipt_at_the_producing_run(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """A receipt with no explicit locator supplies its own, run id and all."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        result = await reconcile(ctx, reconcile_params(published))
        assert require_attempt(operation_of(result), "pypi").effect_receipt_ref == (
            f"receipt://pypi/run/{RUN_ID}"
        )

    run_async(body)


def test_reconcile_keeps_an_explicit_effect_receipt_over_the_derived_one(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """An operator who names the receipt is not overruled by the default."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        result = await reconcile(
            ctx,
            reconcile_params(published, effect_receipt_ref="receipt://pypi/manual"),
        )
        assert (
            require_attempt(operation_of(result), "pypi").effect_receipt_ref
            == "receipt://pypi/manual"
        )

    run_async(body)


def test_reconcile_accepts_the_npm_semver_spelling_of_the_version(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """npm carries ``0.7.0-dev.1`` for the checkpoint PyPI calls ``0.7.0.dev1``."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        result = await reconcile(
            ctx,
            reconcile_params(
                published,
                target_id="npm",
                receipt=receipt_payload(target_id="npm", version="0.7.0-dev.1"),
            ),
        )
        assert require_attempt(operation_of(result), "npm").status is (
            ReleaseTargetStatus.REPORTED_SUCCESS
        )

    run_async(body)


@pytest.mark.parametrize(
    "status",
    [
        ReleaseTargetStatus.OBSERVED_SUCCESS.value,
        ReleaseTargetStatus.OBSERVED_MISMATCH.value,
    ],
)
def test_reconcile_over_a_receipt_cannot_be_pushed_to_an_observed_status(
    ctx: MethodContext,
    green_probes: None,
    status: str,
) -> None:
    """Naming an observed status beside a receipt is refused twice over."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        with pytest.raises(ValidationError, match="exactly one of"):
            await reconcile(ctx, reconcile_params(published, status=status))

    run_async(body)


def test_reconcile_refuses_neither_a_status_nor_a_receipt(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """A call carrying no result source settles nothing and must say so."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        params = reconcile_params(published)
        params.pop("receipt")
        with pytest.raises(ValidationError, match="exactly one of"):
            await reconcile(ctx, params)

    run_async(body)


def test_reconcile_refuses_a_receipt_filed_against_another_leg(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """The npm job's receipt cannot settle the pypi leg."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        with pytest.raises(DaemonValidationError, match=r"reports target 'npm', not 'pypi'"):
            await reconcile(
                ctx,
                reconcile_params(published, receipt=receipt_payload(target_id="npm")),
            )

    run_async(body)


def test_reconcile_refuses_a_receipt_for_another_version(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """A receipt from the previous checkpoint's run settles nothing here."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        with pytest.raises(DaemonValidationError, match=r"reports version '0\.6\.9'"):
            await reconcile(
                ctx,
                reconcile_params(published, receipt=receipt_payload(version="0.6.9")),
            )

    run_async(body)


def test_reconcile_refuses_a_malformed_receipt(
    ctx: MethodContext,
    green_probes: None,
) -> None:
    """A receipt missing its run id is a broken publisher, not a silent one."""

    async def body() -> None:
        published = await publish(ctx, publish_params())
        seed_dispatch(ctx, published)
        payload = receipt_payload()
        payload.pop("run_id")
        with pytest.raises(DaemonValidationError, match="does not validate"):
            await reconcile(ctx, reconcile_params(published, receipt=payload))

    run_async(body)
