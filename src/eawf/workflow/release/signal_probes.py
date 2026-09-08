"""Readiness signal probes this project ships, keyed by their signal.

A readiness signal with no probe reports ``unavailable``, which is
honest but useless: it names a gap rather than a fact. This module holds
the probes that exist, so a signal graduates from "nothing computes it
yet" to a real verdict the moment its check lands.

"Probe" is the whole vocabulary here. The one thing this package calls a
*producer* is the CI job in :mod:`eawf.workflow.release.produce` that
writes the pipeline receipts these probes read back, so the two words
name two jobs rather than one job twice.

The ``platform`` signal was deliberately first: the platform claim is
the one signal whose evidence can be *forged by accident*, because a
journey that runs against a stub or a masked container looks exactly
like a journey that ran, and the difference only shows up in a user's
install.

``dependencies`` and ``artifacts`` join it here. Neither can be computed
by a sweep: one needs a resolved environment to read licenses and an
advisory database to query, the other needs two clean builds. Both are
therefore produced in CI and *read back* from the receipts named in
:mod:`eawf.workflow.release.pipeline_receipts`. A receipt the CI job did not
write leaves its row ``unavailable`` naming the job, which is the
honest reading -- an absent producer is not a passing check.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Final

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release_config import ReleasePlatformClaim
from eawf.workflow.release.dependencies import (
    ComponentOutcome,
    DependencyInventoryInputs,
    ReleaseDependencyManifest,
    compute_lock_digest,
    inventory_component,
)
from eawf.workflow.release.pipeline_receipts import (
    RECEIPT_DIRNAME,
    read_build_receipt,
    read_dependency_manifest,
    read_vulnerability_report,
)
from eawf.workflow.release.reproducibility import artifacts_component
from eawf.workflow.release.vulnerability import vulnerability_component

logger = logging.getLogger(__name__)

#: Lock the swept checkout carries. Its digest is what a manifest's own
#: ``lock_digest`` is checked against: the artifacts are built from this
#: tree, so a manifest naming another lock describes another build.
LOCK_FILENAME: Final[str] = "uv.lock"

#: The CI job that writes the three receipts, named in every
#: ``unavailable`` remediation so the operator is told what to run
#: rather than that something is missing.
RECEIPT_PRODUCER_JOB: Final[str] = "inventory-and-reproducibility"


def _unproven(claims: tuple[ReleasePlatformClaim, ...]) -> tuple[str, ...]:
    """Return the ids of the claims whose receipt did not come from a real host."""
    return tuple(claim.platform_id for claim in claims if not claim.real_host)


def platform_probe(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
    """Return the ``platform`` verdict for the checkpoint in *context*.

    An empty claim set is ``unavailable``, not ``pass``. A checkpoint
    that advertises no platform has proven nothing about any platform,
    and reporting that as green would let the row satisfy a gate by
    being vacuous -- exactly the failure the twelve-row sweep exists to
    make visible.

    A non-empty claim set passes only when every claim carries a
    real-host receipt. At ``dev1`` the single claim is the Linux
    real-bwrap CI job, whose receipt is what the row resolves to.

    Args:
        context: The signal request, carrying the loaded configuration.

    Returns:
        ``unavailable`` with remediation when no platform is claimed,
        ``fail`` naming the unproven platforms when a claim has no
        real-host receipt, and ``pass`` carrying every receipt reference
        otherwise.
    """
    claims = context.config.platform_claims
    if not claims:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            remediation=(
                "the checkpoint advertises no platform, so no platform journey can be "
                "read back; declare a platform_claims entry with the receipt of a "
                "real-host run, or drop the platform gate from the profile"
            ),
        )
    unproven = _unproven(claims)
    if unproven:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL,
            remediation=(
                f"platform claim(s) {list(unproven)} carry a receipt that did not come "
                f"from a real host; rerun the journey on a real host of each platform "
                f"and repoint receipt_ref, or withdraw the claim"
            ),
            evidence_refs=tuple(claim.receipt_ref for claim in claims),
        )
    logger.info(
        f"platform_probe release_key={context.config.release_key!r} claims={len(claims)} unproven=0"
    )
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.PASS,
        evidence_refs=tuple(claim.receipt_ref for claim in claims),
    )


def _missing_receipt(artifact_name: str) -> ReleaseSignalOutcome:
    """Return the ``unavailable`` outcome for a receipt CI did not write.

    Args:
        artifact_name: The receipt the producer should have written.

    Returns:
        An ``unavailable`` outcome naming the job and the path.
    """
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.UNAVAILABLE,
        remediation=(
            f"no {artifact_name!r} receipt was found under {RECEIPT_DIRNAME}; run the "
            f"{RECEIPT_PRODUCER_JOB!r} job for this commit and download its artifacts "
            f"before sweeping, or drop the requirement"
        ),
    )


def _merge_components(*outcomes: ComponentOutcome) -> ReleaseSignalOutcome:
    """Merge the component verdicts sharing one row into that row's outcome.

    A row shared by two checks passes only when both pass, and its
    remediation is every failing component's, joined -- reporting only
    the first would hide the second behind a repair round-trip.

    Args:
        outcomes: The component verdicts, in reporting order.

    Returns:
        The row's outcome.
    """
    failing = [outcome for outcome in outcomes if not outcome.passing]
    evidence = tuple(ref for outcome in outcomes for ref in outcome.evidence_refs)
    if not failing:
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=evidence)
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.FAIL,
        remediation=" | ".join(
            f"{outcome.component}: {outcome.remediation}" for outcome in failing
        ),
        evidence_refs=evidence,
    )


def _checkout_lock_digest(repo_root: Path) -> str | None:
    """Return the digest of the checkout's lock, or ``None`` when absent."""
    lock = repo_root / LOCK_FILENAME
    if not lock.is_file():
        return None
    return compute_lock_digest(lock.read_text(encoding="utf-8"))


def dependencies_probe(
    context: ReleaseSignalContext,
    *,
    repo_root: Path | None = None,
) -> ReleaseSignalOutcome:
    """Return the ``dependencies`` verdict from the two written receipts.

    Both components of the row are read: the inventory receipt is judged
    against the swept checkout's own lock, and the vulnerability report
    is judged against its blocking set. Either receipt being absent
    leaves the whole row ``unavailable`` rather than half-computed --
    half a dependency verdict is not a weaker verdict, it is a different
    claim.

    Args:
        context: The signal request.
        repo_root: Checkout the receipts and the lock are read from;
            defaults to the working directory, which is the repo root
            in every path that sweeps a release.

    Returns:
        The row's outcome.
    """
    root = repo_root or Path.cwd()
    manifest: ReleaseDependencyManifest | None = read_dependency_manifest(root)
    if manifest is None:
        return _missing_receipt("dependency-manifest")
    report = read_vulnerability_report(root)
    if report is None:
        return _missing_receipt("vulnerability-report")
    built_digest = _checkout_lock_digest(root) or manifest.lock_digest
    logger.info(
        f"dependencies_probe release_key={context.config.release_key!r} "
        f"packages={len(manifest.packages)} advisories={len(report.advisories)}"
    )
    return _merge_components(
        inventory_component(
            DependencyInventoryInputs(manifest=manifest, wheel_lock_digest=built_digest)
        ),
        vulnerability_component(report),
    )


def artifacts_probe(
    context: ReleaseSignalContext,
    *,
    repo_root: Path | None = None,
) -> ReleaseSignalOutcome:
    """Return the ``artifacts`` verdict from the double-build receipt.

    Args:
        context: The signal request.
        repo_root: Checkout the receipt is read from; defaults to the
            working directory.

    Returns:
        The row's outcome: ``unavailable`` with no receipt, ``fail``
        naming the divergent artifact, ``pass`` otherwise.
    """
    root = repo_root or Path.cwd()
    receipt = read_build_receipt(root)
    if receipt is None:
        return _missing_receipt("reproducible-build-receipt")
    outcome = artifacts_component(receipt)
    logger.info(
        f"artifacts_probe release_key={context.config.release_key!r} "
        f"source_sha={receipt.source_sha[:12]!r} status={outcome.status.value!r}"
    )
    return ReleaseSignalOutcome(
        status=outcome.status,
        remediation=outcome.remediation,
        evidence_refs=outcome.evidence_refs,
    )


def build_receipt_probes(repo_root: Path) -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return the receipt-reading probes bound to *repo_root*.

    Args:
        repo_root: Checkout whose receipts the probes read.

    Returns:
        A registry over the two signals the CI receipts settle.
    """
    return {
        ReleaseSignalName.DEPENDENCIES: partial(dependencies_probe, repo_root=repo_root),
        ReleaseSignalName.ARTIFACTS: partial(artifacts_probe, repo_root=repo_root),
    }


#: Probes this project ships, by signal. A caller's own probe for the
#: same signal wins: injection is how a test pins a verdict and how a
#: later checkpoint swaps in a stronger check without editing this map.
DEFAULT_RELEASE_PROBES: Final[Mapping[ReleaseSignalName, ReleaseSignalProbe]] = {
    ReleaseSignalName.PLATFORM: platform_probe,
    ReleaseSignalName.DEPENDENCIES: dependencies_probe,
    ReleaseSignalName.ARTIFACTS: artifacts_probe,
}


__all__ = [
    "DEFAULT_RELEASE_PROBES",
    "LOCK_FILENAME",
    "RECEIPT_PRODUCER_JOB",
    "artifacts_probe",
    "build_receipt_probes",
    "dependencies_probe",
    "platform_probe",
]
