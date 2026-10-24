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
installed now. The declared canaries are carried forward the same way,
and the Milestones a canary walk recorded are kept only when the prior
export is this release's own, since an acceptance recorded for another
rung accepts nothing here.

The export is a committed artifact rather than project state, so it is
written by the caller's process like the canary walk's record, not
through the daemon.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
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


def _claimed_record(
    rows: Sequence[MachineCertification], runtime_id: str, version: str, os_class: str
) -> CertificationRecord:
    """Return the newest stored certification of *runtime_id* at *version* on *os_class*.

    Raises:
        ValueError: When no stored row carries a driver certification of
            that runtime, version and operating-system class.
    """
    covering = [
        (row, certification)
        for row in rows
        if (certification := row.certification) is not None
        and row.runtime_id == runtime_id
        and row.harness_version == version
        and certification.os_class == os_class
    ]
    if not covering:
        raise ValueError(
            f"canary_export_uncovered: no stored driver certification covers {runtime_id} "
            f"{version} on {os_class!r}; run `eawf runtime certify {runtime_id} --task <key> "
            f"--actor <key>` on a {os_class} machine, or drop {os_class!r} from --os-class"
        )
    row, certification = max(covering, key=lambda pair: pair[0].verified_at)
    return CertificationRecord(
        certification_urn=row.certification_urn,
        runtime_id=row.runtime_id,
        tuple_digest=row.tuple_digest,
        certification=certification,
    )


def build_canary_export(
    release_key: str,
    *,
    prior: CanaryEvidence,
    rows: Sequence[MachineCertification],
    installed: Mapping[str, str],
    os_classes: Sequence[OsClass],
) -> CanaryEvidence:
    """Return *release_key*'s export over the stored certifications of the installed runtimes.

    Args:
        release_key: The checkpoint being exported.
        prior: The export the advertised runtimes, their required
            capabilities and the declared canaries are carried from.
        rows: This machine's stored certification rows.
        installed: The installed version of each runtime *prior* advertises.
        os_classes: The operating-system classes every claim ships on.

    Returns:
        The export: one claim per runtime *prior* advertises, addressed by
        the manifest its stored certification names, and that certification
        copied unchanged for each claimed class.

    Raises:
        ValueError: When a claimed class has no stored driver certification
            of an advertised runtime at its installed version.
    """
    claimed = tuple(dict.fromkeys(os_classes))
    advertised: list[RuntimeClaim] = []
    certifications: list[CertificationRecord] = []
    for claim in prior.advertised:
        version = installed[claim.runtime_id]
        records = [
            _claimed_record(rows, claim.runtime_id, version, os_class) for os_class in claimed
        ]
        advertised.append(
            RuntimeClaim(
                runtime_id=claim.runtime_id,
                manifest_ref=records[0].certification.manifest_ref,
                manifest_digest=records[0].certification.manifest_digest,
                required_capabilities=claim.required_capabilities,
                os_classes=claimed,
            )
        )
        certifications.extend(records)
    return CanaryEvidence(
        schema_version="native-canary-evidence/v1",
        release_key=release_key,
        canaries=prior.canaries,
        advertised=tuple(advertised),
        certifications=tuple(certifications),
        milestones=prior.milestones if prior.release_key == release_key else (),
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
        os_classes: The operating-system classes every claim ships on.

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
