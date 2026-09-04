"""The receipt a publish job leaves behind, and what reconciliation reads.

The publisher is the tag-push pipeline, not the daemon. A publish job
runs on a runner the daemon never sees, succeeds or fails there, and
then the runner is gone -- so the only way the ledger can learn what a
leg did is for the leg to write it down before it exits. That artifact
is a :class:`PublicationReceipt`, uploaded as
``publication-receipt-<target>.json`` beside the thing it published.

Reconciliation is then a *download* rather than an interrogation: the
operator (or the recovery sweep) fetches the receipts of the tag's runs
and hands each to ``release.reconcile``, which maps it onto one of the
three reported statuses. The mapping deliberately cannot reach either
``observed_*`` status -- a receipt is the publisher's own word about its
own call, which is exactly what a reported status means. Confirming that
the artifact is really on the registry is a separate read-back, and
:func:`~eawf.workflow.release.observe.observe_target` owns it.

An absent receipt is not a failure. A job that was cancelled, timed out
or died before its final step leaves nothing behind, and the honest
reading of "the publisher never told us" is
:attr:`~eawf.kernel.spec.release.ReleaseTargetStatus.UNKNOWN`, which is
retryable. Treating silence as failure would burn a version that may
well have published.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

from pydantic import Field, ValidationError

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReleaseTargetStatus

logger = logging.getLogger(__name__)

#: Filename (and workflow-artifact name, minus the extension) each
#: publish job writes its receipt under. One name per target so three
#: jobs uploading in parallel cannot clobber each other's evidence.
PUBLICATION_RECEIPT_TEMPLATE: Final[str] = "publication-receipt-{target_id}.json"

#: Job conclusions that mean the publisher believes it published. GitHub
#: Actions spells a green job ``success``; nothing else does.
_REPORTED_SUCCESS_CONCLUSIONS: Final[frozenset[str]] = frozenset({"success"})

#: Job conclusions that mean the publisher believes it did NOT publish.
#: ``cancelled`` and ``timed_out`` are absent on purpose: both can land
#: after the upload call succeeded, so they are unknown, not failed.
_REPORTED_FAILURE_CONCLUSIONS: Final[frozenset[str]] = frozenset({"failure"})


class PublicationReceipt(_StrictModel):
    """What one publish job recorded about its own leg.

    Attributes:
        target_id: The configured leg this job published, e.g. ``pypi``.
        version: The checkpoint version published, in the spelling the
            leg's registry carries -- PEP 440 for PyPI and the source
            host, SemVer for npm.
        artifact_digests: ``filename -> sha256:<hex>`` for every
            artifact the job pushed. Empty on a job that failed before
            uploading anything, which is why the field carries no
            minimum length.
        job_conclusion: The runner's own verdict on the job, verbatim.
        run_id: The workflow run the job belonged to, so a receipt can
            be traced back to its logs.
    """

    target_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    artifact_digests: dict[str, str] = Field(default_factory=dict)
    job_conclusion: str = Field(min_length=1)
    run_id: str = Field(min_length=1)


def receipt_filename(target_id: str) -> str:
    """Return the filename the publish job for *target_id* writes.

    Args:
        target_id: The configured leg, e.g. ``npm``.

    Returns:
        The ``publication-receipt-<target>.json`` name.

    Raises:
        ValueError: When *target_id* is empty, which would collapse
            every leg's receipt onto one filename.
    """
    if not target_id:
        raise ValueError("target_id must be non-empty to name a publication receipt")
    return PUBLICATION_RECEIPT_TEMPLATE.format(target_id=target_id)


def reported_status(receipt: PublicationReceipt | None) -> ReleaseTargetStatus:
    """Return the reported status *receipt* supports.

    Total over the three reported statuses and reachable by no other:
    the two ``observed_*`` statuses assert an independent read-back,
    which no self-written receipt can supply.

    Args:
        receipt: The downloaded receipt, or ``None`` when the job left
            none -- cancelled, killed, or dead before its final step.

    Returns:
        ``reported_success`` for a green job, ``reported_failure`` for a
        job that concluded failed, and ``unknown`` for silence or any
        other conclusion.
    """
    if receipt is None:
        return ReleaseTargetStatus.UNKNOWN
    conclusion = receipt.job_conclusion.strip().lower()
    if conclusion in _REPORTED_SUCCESS_CONCLUSIONS:
        return ReleaseTargetStatus.REPORTED_SUCCESS
    if conclusion in _REPORTED_FAILURE_CONCLUSIONS:
        return ReleaseTargetStatus.REPORTED_FAILURE
    return ReleaseTargetStatus.UNKNOWN


def load_receipt(payload: object) -> PublicationReceipt:
    """Return the receipt *payload* validates as.

    Args:
        payload: A downloaded receipt body, already JSON-decoded.

    Returns:
        The validated receipt.

    Raises:
        ValueError: When *payload* does not validate. A malformed
            receipt is a broken publisher, not a silent job: reading it
            as ``unknown`` would hide a pipeline defect behind a retry.
    """
    try:
        return PublicationReceipt.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"publication receipt does not validate: {exc.error_count()} error(s); "
            f"first: {exc.errors()[0]['msg']}"
        ) from exc


def read_receipts(directory: Path, target_ids: tuple[str, ...]) -> dict[str, PublicationReceipt]:
    """Return the receipts *directory* carries for *target_ids*.

    This is the shape ``gh run download`` leaves behind: one directory
    holding whichever receipts the tag's runs actually uploaded. A leg
    with no file is simply absent from the result, which
    :func:`reported_status` reads as ``unknown``.

    Args:
        directory: Where the downloaded receipts were unpacked.
        target_ids: The configured legs to look for.

    Returns:
        ``target_id -> receipt`` for every leg that left one.

    Raises:
        ValueError: When a present receipt is not valid JSON, does not
            validate, or names a different leg than its filename does.
    """
    found: dict[str, PublicationReceipt] = {}
    for target_id in target_ids:
        path = directory / receipt_filename(target_id)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"publication receipt {path.name!r} is not valid JSON: {exc}") from exc
        receipt = load_receipt(payload)
        if receipt.target_id != target_id:
            raise ValueError(
                f"publication receipt {path.name!r} reports target "
                f"{receipt.target_id!r}, not {target_id!r}"
            )
        found[target_id] = receipt
    logger.info(f"read_receipts targets={list(target_ids)} found={sorted(found)}")
    return found


__all__ = [
    "PUBLICATION_RECEIPT_TEMPLATE",
    "PublicationReceipt",
    "load_receipt",
    "read_receipts",
    "receipt_filename",
    "reported_status",
]
