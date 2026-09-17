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

``provider`` and ``membership`` join them for the same reason one level
further out. Their producers are the daemon-owned conformance runner and
the disposable canary a Milestone was accepted in, and neither of those
is reachable from the checkout a sweep runs against: the runner writes
into a daemon's own store and the canary is a throwaway repository. What
the checkout carries is the *export* of both, read back through
:mod:`eawf.workflow.evidence.provider_certification`.

The two rows treat an absent export differently, and the asymmetry is
deliberate. The advertised runtime set lives in the export, so an absent
export advertises nothing and the ``provider`` row is ``unavailable``.
The acceptance bundles live in the checkpoint *configuration*, so an
absent export leaves a declaration standing with nothing behind it, and
the ``membership`` row fails.

The receipt probes are not defaults. They read one checkout's receipts,
and a default has no checkout to name but the process's working
directory, which is whatever directory the daemon happened to start in.
A sweep that wants them binds them to its repository through
:func:`build_receipt_probes`.
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
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIR,
    CANARY_EVIDENCE_FILENAME,
    CanaryEvidence,
    load_canary_evidence,
    membership_evidence_refs,
    membership_findings,
    provider_evidence_refs,
    provider_findings,
    summarise_findings,
)
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
    repo_root: Path,
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
        repo_root: Checkout the receipts and the lock are read from.

    Returns:
        The row's outcome.
    """
    manifest: ReleaseDependencyManifest | None = read_dependency_manifest(repo_root)
    if manifest is None:
        return _missing_receipt("dependency-manifest")
    report = read_vulnerability_report(repo_root)
    if report is None:
        return _missing_receipt("vulnerability-report")
    built_digest = _checkout_lock_digest(repo_root) or manifest.lock_digest
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
    repo_root: Path,
) -> ReleaseSignalOutcome:
    """Return the ``artifacts`` verdict from the double-build receipt.

    Args:
        context: The signal request.
        repo_root: Checkout the receipt is read from.

    Returns:
        The row's outcome: ``unavailable`` with no receipt, ``fail``
        naming the divergent artifact, ``pass`` otherwise.
    """
    receipt = read_build_receipt(repo_root)
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


def _export_location() -> str:
    """Return the repo-relative location the canary evidence export is read from."""
    return "/".join((*CANARY_EVIDENCE_DIR, CANARY_EVIDENCE_FILENAME))


def _unreadable_export(exc: ValueError) -> ReleaseSignalOutcome:
    """Return the outcome for an export that is present and does not parse.

    Args:
        exc: What the loader refused the document with.

    Returns:
        A ``fail`` outcome. An unreadable export is not an absent one:
        something was committed as evidence and cannot be read, which is
        a defect in the evidence rather than a missing producer.
    """
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.FAIL,
        remediation=(
            f"the canary evidence export at {_export_location()} does not read back: {exc}; "
            f"re-export the conformance records rather than editing the document by hand"
        ),
    )


def _read_export(repo_root: Path) -> tuple[CanaryEvidence | None, ReleaseSignalOutcome | None]:
    """Return the committed export of *repo_root*, or the outcome that replaces it.

    Args:
        repo_root: Checkout the export was committed in.

    Returns:
        ``(evidence, None)`` when an export loads, ``(None, outcome)``
        when it is present and unreadable, and ``(None, None)`` when no
        export is committed -- which the two rows read differently.
    """
    try:
        return load_canary_evidence(repo_root), None
    except ValueError as exc:
        return None, _unreadable_export(exc)


def provider_probe(
    context: ReleaseSignalContext,
    *,
    repo_root: Path,
) -> ReleaseSignalOutcome:
    """Return the ``provider`` verdict from the committed certification export.

    The row passes only when every runtime tuple the export advertises
    carries a runner-written certification that earned itself: a passed
    ``certify`` stage on the contiguous ``probe`` and ``canary`` before
    it, a ``verified`` record over an installation that is not
    quarantined, and a ``verified`` row for every capability the claim
    requires. An advertised tuple backed by a run log and nothing else
    fails like one backed by nothing at all, because a run log records
    that the binary ran rather than that the contract holds.

    Args:
        context: The signal request, carrying the loaded configuration.
        repo_root: Checkout the export is read from.

    Returns:
        ``unavailable`` when no export is committed or it advertises no
        tuple, ``fail`` naming every gap otherwise, and ``pass`` citing
        each certification's URN when nothing is outstanding.
    """
    evidence, refused = _read_export(repo_root)
    if refused is not None:
        return refused
    if evidence is None:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            remediation=(
                f"no conformance certification export is committed at {_export_location()}, "
                f"so no runtime tuple is advertised; run the conformance probe, canary and "
                f"certify stages and export their records, or drop the provider gate"
            ),
        )
    if not evidence.advertised:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            remediation=(
                f"the export at {_export_location()} advertises no runtime tuple, so the "
                f"provider claim is empty; advertise the tuples this checkpoint ships or "
                f"drop the provider gate"
            ),
        )
    findings = provider_findings(evidence)
    refs = provider_evidence_refs(evidence)
    if findings:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL,
            remediation=(
                f"{len(findings)} advertised runtime claim(s) are not backed by a "
                f"runner-written certification: {summarise_findings(findings)}"
            ),
            evidence_refs=refs,
        )
    logger.info(
        f"provider_probe release_key={context.config.release_key!r} "
        f"advertised={len(evidence.advertised)} certified={len(refs)}"
    )
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=refs)


def membership_probe(
    context: ReleaseSignalContext,
    *,
    repo_root: Path,
) -> ReleaseSignalOutcome:
    """Return the ``membership`` verdict for the checkpoint's acceptance bundles.

    The row passes only when every ``membership_ref`` the configuration
    declares resolves to a COMPLETED Milestone recorded in a canary the
    export declares. A reference that resolves to nothing, to a Milestone
    that has not finished, or to one recorded outside a declared canary
    leaves the row red, because each of those is a bundle the checkpoint
    claimed and cannot show accepted.

    Args:
        context: The signal request, carrying the loaded configuration.
        repo_root: Checkout the export is read from.

    Returns:
        ``unavailable`` when the checkpoint declares no bundle -- which is
        every rung before ``dev3`` -- ``fail`` naming every unresolved or
        unfinished bundle, and ``pass`` citing each accepted Milestone
        otherwise.
    """
    declared = context.config.membership_refs
    if not declared:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            remediation=(
                "the checkpoint declares no membership_refs, so there is no acceptance "
                "bundle to resolve; declare the canary Milestone bundles this checkpoint "
                "accepts, or drop the membership gate from the profile"
            ),
        )
    evidence, refused = _read_export(repo_root)
    if refused is not None:
        return refused
    if evidence is None:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL,
            remediation=(
                f"the checkpoint declares {len(declared)} acceptance bundle(s) and no canary "
                f"evidence is committed at {_export_location()}; accept the Milestones in a "
                f"declared canary and export the records, or withdraw the references"
            ),
        )
    findings = membership_findings(evidence, declared)
    refs = membership_evidence_refs(evidence, declared)
    if findings:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL,
            remediation=(
                f"{len(findings)} declared acceptance bundle(s) are not accepted: "
                f"{summarise_findings(findings)}"
            ),
            evidence_refs=refs,
        )
    logger.info(
        f"membership_probe release_key={context.config.release_key!r} "
        f"declared={len(declared)} accepted={len(refs)}"
    )
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=refs)


def build_receipt_probes(repo_root: Path) -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return the receipt-reading probes bound to *repo_root*.

    Args:
        repo_root: Checkout whose receipts and committed evidence the
            probes read.

    Returns:
        A registry over the two signals the CI receipts settle plus the
        two the committed canary evidence settles.
    """
    return {
        ReleaseSignalName.DEPENDENCIES: partial(dependencies_probe, repo_root=repo_root),
        ReleaseSignalName.ARTIFACTS: partial(artifacts_probe, repo_root=repo_root),
        ReleaseSignalName.PROVIDER: partial(provider_probe, repo_root=repo_root),
        ReleaseSignalName.MEMBERSHIP: partial(membership_probe, repo_root=repo_root),
    }


#: Probes that need nothing but the checkpoint, by signal. A caller's own
#: probe for the same signal wins: injection is how a test pins a verdict
#: and how a later checkpoint swaps in a stronger check without editing
#: this map. Probes that read a checkout are bound by the caller instead.
DEFAULT_RELEASE_PROBES: Final[Mapping[ReleaseSignalName, ReleaseSignalProbe]] = {
    ReleaseSignalName.PLATFORM: platform_probe,
}


__all__ = [
    "DEFAULT_RELEASE_PROBES",
    "LOCK_FILENAME",
    "RECEIPT_PRODUCER_JOB",
    "artifacts_probe",
    "build_receipt_probes",
    "dependencies_probe",
    "membership_probe",
    "platform_probe",
    "provider_probe",
]
