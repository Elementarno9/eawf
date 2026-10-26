"""Export a release's native-canary evidence from this machine's stored certifications.

A release readiness sweep reads the committed export of
:mod:`eawf.workflow.evidence.provider_certification`, never the daemon's
store, so the certifications a canary walk earned have to be copied into
the checkout before a rung can show its ``provider`` row proven. This
module is that copy. Every certification it writes is the record the
certify stage wrote, read back unchanged from the machine-local store
(:mod:`eawf.workflow.evidence.machine_certification`); nothing is rebuilt,
so the export can only claim what a walk on this machine recorded.

The runtimes the export advertises, and the capabilities each claim
requires, are the ones the release's previous export advertised: a rung
re-certifies the runtimes the train already claims at the versions
installed now. The declared canaries are carried forward the same way.
The Milestones a canary walk recorded, the isolation pair, the run logs
and the certifications of classes not claimed on this run are kept only
when the prior export is this release's own, since evidence recorded for
another rung proves nothing here.

Only a live certification is exported: the newest walk of a version
decides, so a later quarantine refuses the claim, and an expired
certification is refused rather than copied.

The export is a committed artifact rather than project state, so it is
written by the caller's process like the canary walk's record, not
through the daemon.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.runtime.daemon.runtime_certifier import installed_version
from eawf.runtime.runtimes.probes.sdk_baseline import probe_runtime
from eawf.workflow.evidence.machine_certification import read_machine_certifications
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIRS,
    CanaryEvidence,
    CertificationRecord,
    OsClass,
    RuntimeClaim,
    canary_evidence_path,
    load_canary_evidence,
)

logger = logging.getLogger(__name__)


def prior_canary_evidence(repo_root: Path, release_key: str) -> CanaryEvidence:
    """Return the export *release_key*'s new export carries its claims forward from.

    Args:
        repo_root: Checkout the exports are committed in.
        release_key: The checkpoint being exported.

    Returns:
        The release's own committed export when one exists, so a re-export
        keeps the Milestones a canary walk recorded into it; otherwise the
        export of the nearest earlier rung that has one.

    Raises:
        ValueError: When no export directory is mapped for *release_key*, no
            rung up to it has a committed export, or one of them does not
            load.
    """
    keys = list(CANARY_EVIDENCE_DIRS)
    for key in reversed(keys[: keys.index(release_key) + 1]):
        evidence = load_canary_evidence(repo_root, key)
        if evidence is not None:
            return evidence
    raise ValueError(
        f"canary_export_no_prior: no export is committed for {release_key!r} or a rung "
        f"before it, so there is no advertised runtime to carry forward"
    )


def _latest_walk(
    rows: Sequence[MachineCertification], runtime_id: str, version: str, os_class: str
) -> MachineCertification | None:
    """Return the newest walk of *runtime_id* at *version* that bears on *os_class*.

    The rule is the daemon's binding rule: the newest row wins whatever its
    outcome, and a tie keeps the later append. A row without a kept
    certification names no class, so it bears on every class; that is what
    lets a later quarantine shadow an earlier certification.

    Args:
        rows: This machine's stored certification rows, in append order.
        runtime_id: The runtime being claimed.
        version: Its installed version.
        os_class: The operating-system class being claimed.

    Returns:
        The newest bearing row, or ``None`` when no row bears on the claim.
    """
    latest: MachineCertification | None = None
    for row in rows:
        if row.runtime_id != runtime_id or row.harness_version != version:
            continue
        if row.certification is not None and row.certification.os_class != os_class:
            continue
        if latest is None or row.verified_at >= latest.verified_at:
            latest = row
    return latest


def _claimed_record(
    rows: Sequence[MachineCertification],
    runtime_id: str,
    version: str,
    os_class: str,
    *,
    now: datetime,
) -> CertificationRecord:
    """Return the live certification of *runtime_id* at *version* on *os_class*.

    Args:
        rows: This machine's stored certification rows, in append order.
        runtime_id: The runtime being claimed.
        version: Its installed version.
        os_class: The operating-system class being claimed.
        now: The export time an expiry is measured against.

    Returns:
        The certification the newest bearing walk stored, with its URN.

    Raises:
        ValueError: ``canary_export_quarantined`` when the newest bearing
            walk quarantined the version, ``canary_export_uncovered`` when
            no walk bears on the claim or the newest one kept no driver
            certification, and ``canary_export_expired`` when its
            certification expired at or before *now*.
    """
    row = _latest_walk(rows, runtime_id, version, os_class)
    if row is not None and row.outcome == "quarantined":
        raise ValueError(
            f"canary_export_quarantined: {row.certification_urn} quarantined {runtime_id} "
            f"{version} after any earlier certification; run `eawf runtime certify "
            f"{runtime_id} --task <key> --actor <key>` once the version passes again"
        )
    if row is None or row.certification is None:
        raise ValueError(
            f"canary_export_uncovered: no stored driver certification covers {runtime_id} "
            f"{version} on {os_class!r}; run `eawf runtime certify {runtime_id} --task <key> "
            f"--actor <key>` on a {os_class} machine, or drop {os_class!r} from --os-class"
        )
    certification = row.certification
    if certification.expires_at <= now:
        raise ValueError(
            f"canary_export_expired: {row.certification_urn} certified {runtime_id} {version} "
            f"on {os_class!r} until {certification.expires_at:%Y-%m-%d}; run `eawf runtime "
            f"certify {runtime_id} --task <key> --actor <key>` to renew it"
        )
    return CertificationRecord(
        certification_urn=row.certification_urn,
        runtime_id=row.runtime_id,
        tuple_digest=row.tuple_digest,
        certification=certification,
    )


def _check_kept_record(
    rows: Sequence[MachineCertification], record: CertificationRecord, *, now: datetime
) -> None:
    """Refuse a kept certification a claim would ship on that is no longer live.

    A kept record was taken on another machine, so its own walk history is
    not in *rows*; only a walk this machine recorded for the same runtime,
    version and class at or after the kept verification can shadow it.
    When there is none the record stays, because the absence of local
    walks says nothing about the machine that took it.

    Args:
        rows: This machine's stored certification rows, in append order.
        record: The prior export's certification of a class not claimed now.
        now: The export time an expiry is measured against.

    Raises:
        ValueError: ``canary_export_quarantined`` when this machine's newest
            bearing walk is a quarantine no older than the kept
            certification, and ``canary_export_expired`` when the kept
            certification expired at or before *now*.
    """
    certification = record.certification
    os_class = certification.os_class
    row = _latest_walk(rows, record.runtime_id, certification.distribution_version, os_class)
    if (
        row is not None
        and row.outcome == "quarantined"
        and row.verified_at >= certification.verified_at
    ):
        raise ValueError(
            f"canary_export_quarantined: {row.certification_urn} quarantined "
            f"{record.runtime_id} {certification.distribution_version} after the kept "
            f"certification {record.certification_urn} on {os_class!r}; run `eawf runtime "
            f"certify {record.runtime_id} --task <key> --actor <key>` once the version "
            f"passes again"
        )
    if certification.expires_at <= now:
        raise ValueError(
            f"canary_export_expired: the kept {record.certification_urn} certified "
            f"{record.runtime_id} {certification.distribution_version} on {os_class!r} until "
            f"{certification.expires_at:%Y-%m-%d}; run `eawf runtime certify "
            f"{record.runtime_id} --task <key> --actor <key>` on a {os_class} machine and "
            f"export {os_class!r} again"
        )


def build_canary_export(
    release_key: str,
    *,
    prior: CanaryEvidence,
    rows: Sequence[MachineCertification],
    installed: Mapping[str, str],
    os_classes: Sequence[OsClass],
    now: datetime,
) -> CanaryEvidence:
    """Return *release_key*'s export over the stored certifications of the installed runtimes.

    Args:
        release_key: The checkpoint being exported.
        prior: The export the advertised runtimes, their required
            capabilities and the declared canaries are carried from.
        rows: This machine's stored certification rows, in append order.
        installed: The installed version of each runtime *prior* advertises.
        os_classes: The operating-system classes this run exports.
        now: The export time an expiry is measured against.

    Returns:
        The export: one claim per runtime *prior* advertises, addressed by
        the manifest its stored certification names, and that certification
        copied unchanged for each exported class. When *prior* is this
        release's own export, its isolation pair, run logs, Milestones and
        the certifications of every class not exported now are kept after
        the new records, since another machine took them, and each claim
        also ships on every kept class certified for its manifest.

    Raises:
        ValueError: When an exported class has no stored driver
            certification of an advertised runtime at its installed version,
            the newest walk of that version is a quarantine, or its
            certification has expired; and when a kept certification a
            claim ships on has expired or a newer walk on this machine
            quarantined it.
    """
    claimed = tuple(dict.fromkeys(os_classes))
    own = prior.release_key == release_key
    kept = (
        tuple(
            record
            for record in prior.certifications
            if record.certification.os_class not in claimed
        )
        if own
        else ()
    )
    advertised: list[RuntimeClaim] = []
    certifications: list[CertificationRecord] = []
    for claim in prior.advertised:
        version = installed[claim.runtime_id]
        records = [
            _claimed_record(rows, claim.runtime_id, version, os_class, now=now)
            for os_class in claimed
        ]
        manifest_ref = records[0].certification.manifest_ref
        # A kept class joins the claim only when its record certifies the
        # manifest claimed now, since the provider gate matches on it.
        joined = [
            record
            for record in kept
            if record.runtime_id == claim.runtime_id
            and record.certification.manifest_ref == manifest_ref
        ]
        for record in joined:
            _check_kept_record(rows, record, now=now)
        advertised.append(
            RuntimeClaim(
                runtime_id=claim.runtime_id,
                manifest_ref=manifest_ref,
                manifest_digest=records[0].certification.manifest_digest,
                required_capabilities=claim.required_capabilities,
                os_classes=tuple(
                    dict.fromkeys((*claimed, *(record.certification.os_class for record in joined)))
                ),
            )
        )
        certifications.extend(records)
    if not own:
        return CanaryEvidence(
            schema_version="native-canary-evidence/v1",
            release_key=release_key,
            canaries=prior.canaries,
            advertised=tuple(advertised),
            certifications=tuple(certifications),
        )
    return CanaryEvidence(
        schema_version="native-canary-evidence/v1",
        release_key=release_key,
        canaries=prior.canaries,
        advertised=tuple(advertised),
        certifications=(*certifications, *kept),
        run_logs=prior.run_logs,
        milestones=prior.milestones,
        isolation=prior.isolation,
    )


def _installed_version(runtime_id: str) -> str:
    """Return the version of *runtime_id* installed on this machine.

    Raises:
        ValueError: When the binary is absent or reports no version.
    """
    observed = probe_runtime(runtime_id)
    version = installed_version(observed.version) if observed.installed else None
    if version is None:
        raise ValueError(
            f"canary_export_not_installed: {runtime_id} has no binary on PATH that reports a "
            f"version, so no installed version can be exported"
        )
    return version


def write_canary_export(
    state_path: Path, release_key: str, *, os_classes: Sequence[OsClass]
) -> CanaryEvidence:
    """Write *release_key*'s export into its evidence directory and return it.

    Args:
        state_path: The tree's ``state.json``; its machine-local store holds
            the certifications and its checkout receives the export.
        release_key: The checkpoint being exported.
        os_classes: The operating-system classes this run exports.

    Returns:
        The export as written.

    Raises:
        ValueError: When no export directory is mapped for *release_key*, no
            prior export exists, an advertised runtime is
            not installed, or a claimed class has no stored certification.
    """
    repo_root = state_path.parent.parent
    prior = prior_canary_evidence(repo_root, release_key)
    installed = {
        claim.runtime_id: _installed_version(claim.runtime_id) for claim in prior.advertised
    }
    evidence = build_canary_export(
        release_key,
        prior=prior,
        rows=read_machine_certifications(state_path),
        installed=installed,
        os_classes=os_classes,
        now=datetime.now(UTC),
    )
    path = canary_evidence_path(repo_root, release_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True)
    path.write_text(f"{document}\n", encoding="utf-8")
    logger.info(
        f"write_canary_export release_key={release_key!r} installed={installed} "
        f"os_classes={list(os_classes)} certifications={len(evidence.certifications)}"
    )
    return evidence


__all__ = ["build_canary_export", "prior_canary_evidence", "write_canary_export"]
