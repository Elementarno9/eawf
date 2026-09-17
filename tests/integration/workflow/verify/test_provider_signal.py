"""REL-035: the ``provider`` row reads runner-written certifications.

The conformance runner is the sole writer of a ``DriverCertification``
and it writes into a daemon's own store. A readiness sweep runs against
a checkout, so what it reads is the committed export of those records.
These tests pin what that reading admits.

**An advertised tuple with no certify-stage record fails.** Not
``unavailable``, not silently absent: the checkpoint advertised a
runtime contract, the evidence for it does not exist, and the row says
``provider_claim_unproven``. The case that matters most is the near
miss -- a run log and nothing else -- because a green run looks like
conformance evidence and is evidence only of execution.

**The stage history is re-checked, never trusted.** The export carries
the record verbatim, so the reader confirms the one thing the runner
promises: a passed ``certify`` sitting on the contiguous passed
``probe`` and ``canary`` before it. A record whose history was truncated
or whose certify was refused is refused here even though it validates as
a model.

**The committed export is real evidence.** The last group runs the probe
against this repository rather than a fixture: one tuple installed on
the producing host walked the three stages, and the dev3 readiness cites
its certification by URN. That is the ``PRX-050`` bootstrap, and it is
asserted rather than described.
"""

from __future__ import annotations

import copy
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.release.signals import (
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.runtime.release.chokepoint import sweep_for_tag
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIR,
    CANARY_EVIDENCE_FILENAME,
    CanaryEvidenceGap,
    canary_evidence_path,
    load_canary_evidence,
    provider_evidence_refs,
    provider_findings,
    summarise_findings,
)
from eawf.workflow.release.signal_probes import build_receipt_probes, provider_probe
from eawf.workflow.release.train import DEV3_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalContext,
    compute_readiness,
)

pytestmark = pytest.mark.integration

#: This checkout, whose committed export is the real evidence.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: Instant every sweep in this module is computed at.
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The acceptance bundle a dev3 configuration has to name to load.
MEMBERSHIP_REF = "milestone://epoch2/native-canary"

#: The URN the committed certification is cited by.
CERTIFICATION_URN = "certification://claude-code/2026-09-18"

#: The manifest the committed export advertises and certifies.
MANIFEST_REF = "driver://claude-code/2.1.274"

#: A digest of the right shape that names no real manifest.
OTHER_DIGEST = f"sha256:{'b' * 64}"


def committed() -> dict[str, Any]:
    """Return a mutable copy of this checkout's committed export."""
    path = REPO_ROOT.joinpath(*CANARY_EVIDENCE_DIR, CANARY_EVIDENCE_FILENAME)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return copy.deepcopy(document)


def staged(tmp_path: Path, document: dict[str, Any] | str) -> Path:
    """Write *document* as the export of a checkout at *tmp_path*.

    Args:
        tmp_path: The staged checkout root.
        document: The export payload, or raw text for the unreadable
            cases.

    Returns:
        The staged checkout root.
    """
    path = canary_evidence_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else json.dumps(document, indent=2)
    path.write_text(text, encoding="utf-8")
    return tmp_path


def dev3_config(*, membership_refs: tuple[str, ...] = (MEMBERSHIP_REF,)) -> ReleaseConfig:
    """Return the rendered dev3 configuration carrying *membership_refs*."""
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV3_RELEASE_CONFIG_YAML))["release"]
    body["membership_refs"] = list(membership_refs)
    return load_release_config({"release": body}, train=V07_TRAIN)


def probe_at(repo_root: Path) -> Any:
    """Return the ``provider`` outcome for a checkout at *repo_root*."""
    context = ReleaseSignalContext(dev3_config(), ReleaseSignalName.PROVIDER, None)
    return provider_probe(context, repo_root=repo_root)


def provider_row(repo_root: Path) -> Any:
    """Return the ``provider`` row of a dev3 sweep over *repo_root*."""
    readiness = compute_readiness(
        dev3_config(),
        probes=build_receipt_probes(repo_root),
        computed_at=NOW,
    )
    return readiness.row(ReleaseSignalName.PROVIDER)


def certification(document: dict[str, Any]) -> dict[str, Any]:
    """Return the single certification record of *document*."""
    record: dict[str, Any] = document["certifications"][0]["certification"]
    return record


# --- the committed export: the PRX-050 bootstrap ---------------------


def test_the_committed_export_loads_as_a_validated_record() -> None:
    evidence = load_canary_evidence(REPO_ROOT)

    assert evidence is not None
    assert evidence.release_key == "REL-0.7.0.dev3"
    assert len(evidence.certifications) == 1


def test_the_committed_certification_carries_contiguous_probe_canary_certify() -> None:
    """The runner's promise, re-checked off the committed record."""
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None
    record = evidence.certifications[0]

    assert record.stage_sequence == ("probe", "canary", "certify")
    assert all(row.outcome == "passed" for row in record.certification.stage_history)
    assert record.certify_findings() == ()


def test_the_committed_export_advertises_a_tuple_it_certifies() -> None:
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None

    assert [claim.manifest_ref for claim in evidence.advertised] == [MANIFEST_REF]
    assert evidence.certification_for(MANIFEST_REF) is not None


def test_provider_probe_passes_over_this_checkout() -> None:
    outcome = probe_at(REPO_ROOT)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == (CERTIFICATION_URN,)


def test_the_dev3_readiness_cites_the_certification_by_urn() -> None:
    """PRX-050: the row an approver reads names the record, not the claim."""
    row = provider_row(REPO_ROOT)

    assert row.status is ReleaseSignalStatus.PASS
    assert row.failure_code is None
    assert CERTIFICATION_URN in row.evidence_refs


# --- an advertised tuple with no certify-stage record ----------------


def test_provider_probe_fails_when_the_advertised_tuple_has_no_record(tmp_path: Path) -> None:
    document = committed()
    document["certifications"] = []

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.NO_CERTIFICATION.value in outcome.remediation
    assert MANIFEST_REF in outcome.remediation


def test_the_row_reports_provider_claim_unproven_without_a_record(tmp_path: Path) -> None:
    document = committed()
    document["certifications"] = []

    row = provider_row(staged(tmp_path, document))

    assert row.status is ReleaseSignalStatus.FAIL
    assert row.failure_code is ReleaseSignalFailureCode.PROVIDER_CLAIM_UNPROVEN


def test_a_run_log_alone_does_not_certify_an_advertised_tuple(tmp_path: Path) -> None:
    """The near miss: something was produced and it is the wrong thing."""
    document = committed()
    document["certifications"] = []
    document["run_logs"] = [
        {
            "runtime_id": "claude-code",
            "manifest_ref": MANIFEST_REF,
            "ran_at": "2026-09-18T00:00:00+00:00",
            "exit_code": 0,
            "log_ref": "artifact://dev3-conformance/2026-09-18/claude-code-run",
        }
    ]

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.RUN_LOG_ONLY.value in outcome.remediation
    assert "never that the advertised contract holds" in outcome.remediation


def test_a_run_log_row_leaves_the_row_provider_claim_unproven(tmp_path: Path) -> None:
    document = committed()
    document["certifications"] = []
    document["run_logs"] = [
        {
            "runtime_id": "claude-code",
            "manifest_ref": MANIFEST_REF,
            "ran_at": "2026-09-18T00:00:00+00:00",
            "exit_code": 0,
            "log_ref": "artifact://dev3-conformance/2026-09-18/claude-code-run",
        }
    ]

    row = provider_row(staged(tmp_path, document))

    assert row.failure_code is ReleaseSignalFailureCode.PROVIDER_CLAIM_UNPROVEN


# --- the stage history is re-checked ---------------------------------


def test_provider_probe_fails_when_the_certify_stage_was_refused(tmp_path: Path) -> None:
    document = committed()
    record = certification(document)
    record["stage_history"][-1]["outcome"] = "refused"
    record["stage_history"][-1]["reason_code"] = "canary_in_progress"

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.NO_CERTIFY_STAGE.value in outcome.remediation


def test_provider_probe_fails_when_the_history_carries_no_canary(tmp_path: Path) -> None:
    """A certify on a probe alone cites a canary that never ran."""
    document = committed()
    record = certification(document)
    record["stage_history"] = [record["stage_history"][0], record["stage_history"][-1]]

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.NO_CERTIFY_STAGE.value in outcome.remediation


def test_provider_probe_fails_when_the_certification_is_revoked(tmp_path: Path) -> None:
    document = committed()
    record = certification(document)
    record["overall_status"] = "revoked"
    record["install_trust"] = "quarantined"
    record["quarantine_trigger"] = "canary_failure"
    record["revoked_at"] = "2026-09-18T01:00:00+00:00"
    record["revocation_reason"] = "quarantined on canary_failure"

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.CERTIFICATION_NOT_VERIFIED.value in outcome.remediation
    assert "canary_failure" in outcome.remediation


def test_a_revoked_record_reports_both_axes_separately(tmp_path: Path) -> None:
    """Freshness and installation trust are repaired differently."""
    document = committed()
    record = certification(document)
    record["overall_status"] = "revoked"
    record["install_trust"] = "quarantined"
    record["quarantine_trigger"] = "protocol_drift"
    record["revoked_at"] = "2026-09-18T01:00:00+00:00"
    record["revocation_reason"] = "quarantined on protocol_drift"
    evidence = load_canary_evidence(staged(tmp_path, document))
    assert evidence is not None

    findings = provider_findings(evidence)

    assert len(findings) == 2
    assert {finding.gap for finding in findings} == {CanaryEvidenceGap.CERTIFICATION_NOT_VERIFIED}


def test_provider_probe_fails_when_a_required_capability_is_not_verified(tmp_path: Path) -> None:
    """``session_resume`` is certified ``unsupported``, so it cannot be required."""
    document = committed()
    document["advertised"][0]["required_capabilities"] = [
        "tool_use",
        "streaming",
        "session_resume",
    ]

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.CAPABILITY_UNCERTIFIED.value in outcome.remediation
    assert "'unsupported'" in outcome.remediation


def test_provider_probe_fails_on_a_capability_the_record_never_mentions(tmp_path: Path) -> None:
    document = committed()
    document["advertised"][0]["required_capabilities"] = ["tool_use", "plan_mode"]

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "absent from the certification" in outcome.remediation


def test_provider_probe_fails_when_the_certification_covers_another_build(tmp_path: Path) -> None:
    document = committed()
    document["advertised"][0]["manifest_digest"] = OTHER_DIGEST

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "certifies another build" in outcome.remediation


# --- absence, emptiness and unreadability ----------------------------


def test_provider_probe_is_unavailable_without_an_export(tmp_path: Path) -> None:
    """Nothing is advertised, so nothing is claimed."""
    outcome = probe_at(tmp_path)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert CANARY_EVIDENCE_FILENAME in outcome.remediation


def test_an_absent_export_still_names_provider_claim_unproven(tmp_path: Path) -> None:
    row = provider_row(tmp_path)

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert row.failure_code is ReleaseSignalFailureCode.PROVIDER_CLAIM_UNPROVEN


def test_provider_probe_is_unavailable_when_nothing_is_advertised(tmp_path: Path) -> None:
    document = committed()
    document["advertised"] = []

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "advertises no runtime tuple" in outcome.remediation


def test_provider_probe_fails_on_an_export_that_is_not_json(tmp_path: Path) -> None:
    outcome = probe_at(staged(tmp_path, "{not json"))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "does not read back" in outcome.remediation


def test_provider_probe_fails_on_an_export_carrying_an_unknown_key(tmp_path: Path) -> None:
    """An export nobody can validate is not an absent one."""
    document = committed()
    document["shipped"] = True

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "does not read back" in outcome.remediation


def test_provider_probe_fails_on_an_export_that_is_not_a_mapping(tmp_path: Path) -> None:
    outcome = probe_at(staged(tmp_path, "[]"))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "does not read back" in outcome.remediation


# --- more than one claim ---------------------------------------------


def test_a_second_uncertified_claim_is_named_and_the_certified_one_is_not(
    tmp_path: Path,
) -> None:
    document = committed()
    document["advertised"].append(
        {
            "runtime_id": "codex",
            "manifest_ref": "driver://codex/0.9.0",
            "manifest_digest": OTHER_DIGEST,
            "required_capabilities": ["tool_use"],
        }
    )

    outcome = probe_at(staged(tmp_path, document))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "driver://codex/0.9.0" in outcome.remediation
    assert outcome.remediation.count(CanaryEvidenceGap.NO_CERTIFICATION.value) == 1
    assert outcome.evidence_refs == (CERTIFICATION_URN,)


# --- the module's own boundaries -------------------------------------


def test_canary_evidence_path_refuses_a_root_that_is_not_a_path() -> None:
    with pytest.raises(TypeError, match="repo_root must be Path"):
        canary_evidence_path("/tmp")  # type: ignore[arg-type]


def test_load_canary_evidence_returns_none_for_an_absent_export(tmp_path: Path) -> None:
    assert load_canary_evidence(tmp_path) is None


def test_load_canary_evidence_refuses_text_that_is_not_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        load_canary_evidence(staged(tmp_path, "{"))


def test_load_canary_evidence_refuses_a_document_that_does_not_validate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not validate"):
        load_canary_evidence(staged(tmp_path, {"schema_version": "native-canary-evidence/v1"}))


def test_provider_evidence_refs_is_empty_without_a_certification(tmp_path: Path) -> None:
    document = committed()
    document["certifications"] = []
    evidence = load_canary_evidence(staged(tmp_path, document))
    assert evidence is not None

    assert provider_evidence_refs(evidence) == ()


def test_provider_findings_is_empty_when_nothing_is_advertised(tmp_path: Path) -> None:
    document = committed()
    document["advertised"] = []
    evidence = load_canary_evidence(staged(tmp_path, document))
    assert evidence is not None

    assert provider_findings(evidence) == ()


def test_summarise_findings_of_nothing_is_empty() -> None:
    assert summarise_findings(()) == ""


def test_certification_for_returns_none_for_a_manifest_nobody_certified() -> None:
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None

    assert evidence.certification_for("driver://nobody/0.0.1") is None
    assert evidence.run_logs_for("driver://nobody/0.0.1") == ()


# --- the production wiring -------------------------------------------


def test_build_receipt_probes_binds_the_provider_row(tmp_path: Path) -> None:
    assert ReleaseSignalName.PROVIDER in build_receipt_probes(tmp_path)


def test_the_release_chokepoint_composes_the_receipt_probes() -> None:
    """One composition: every operator surface sweeps through this function."""
    source = inspect.getsource(sweep_for_tag)

    assert "build_receipt_probes(repo_root)" in source
