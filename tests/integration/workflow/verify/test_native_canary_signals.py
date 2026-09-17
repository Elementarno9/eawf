"""What the three ``native_canary`` gates read once their producers land.

``dev3`` is the first rung with epoch-2 authority, and the three gates it
adds are the three claims only a native canary can settle. W34 declared
the gate set; this module pins what each of them now *reads*.

**Membership is an acceptance, not a mention.** The checkpoint declares
its acceptance bundles in its own configuration, so a declared reference
that resolves to nothing, to a Milestone that has not COMPLETED, or to
one recorded outside a declared canary leaves the row red with
``membership_unaccepted``. Each of those is a bundle the checkpoint
claimed and cannot show accepted, and they are separate findings because
they are separate repairs.

**Canary isolation is a digest pair.** A rehearsal that reports "nothing
happened outside the canary" is reporting its own opinion. Two digests
over the production root are a fact anyone can recompute, so the reading
passes on an equal recorded pair and on nothing else: an unequal pair is
a failure, and an unrecorded one is an absence.

**Every gate name resolves to exactly one row.** The whole point of the
binding table is that no gate passes by reading nothing, so the sweep is
checked for one gate row per profile gate, and every row-backed gate is
checked to land on a signal the sweep actually computed.

The last group is the ``REL-037`` side of the same checkpoint: both dev3
contract legs resolve to promotable contracts, which is what lets the
rung be opened at all.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.release.gate_binding import (
    NATIVE_CANARY_ADDED_GATES,
    GateEvidenceKind,
    profile_gates,
)
from eawf.kernel.release.signals import (
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName, load_release_config
from eawf.workflow.evidence.measured_contract import PREFLIGHT_CONTRACTS
from eawf.workflow.evidence.provider_certification import (
    CanaryEvidenceGap,
    canary_evidence_path,
    load_canary_evidence,
    membership_evidence_refs,
    membership_findings,
)
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.signal_probes import build_receipt_probes, membership_probe
from eawf.workflow.release.train import (
    DEV3_RELEASE_CONFIG_YAML,
    V07_TRAIN,
    gate_bindings_for,
)
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalContext,
    compute_readiness,
)
from tests._release_helpers import all_passing, dev1_config

pytestmark = pytest.mark.integration

#: This checkout, whose committed export is the real evidence.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The profile under test.
PROFILE = ReleaseGateProfile.NATIVE_CANARY

#: Instant every sweep in this module is computed at.
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The acceptance bundle a dev3 configuration names.
MEMBERSHIP_REF = "milestone://epoch2/native-canary"

#: A second bundle, used to prove a refusal names only what is missing.
SECOND_REF = "milestone://epoch2/native-canary-2"

#: The canary the committed export declares.
CANARY_CODE = "W63CANARY"

#: A canary code no export declares.
OTHER_CODE = "ELSEWHERE"

#: The bundle digest the fixture Milestone seals.
BUNDLE_DIGEST = f"sha256:{'c' * 64}"

#: The evidence reference a resolved fixture bundle is cited by.
ACCEPTED_REF = f"milestone:{CANARY_CODE}:MS-0001:{BUNDLE_DIGEST}"

#: The two REL-037 legs whose surfaces dev3 is the first rung to build.
DEV3_CONTRACT_IDS = ("MCT-26081302", "MCT-26081303")


def committed() -> dict[str, Any]:
    """Return a mutable copy of this checkout's committed export."""
    document: dict[str, Any] = json.loads(
        canary_evidence_path(REPO_ROOT).read_text(encoding="utf-8")
    )
    return copy.deepcopy(document)


def staged(tmp_path: Path, document: dict[str, Any]) -> Path:
    """Write *document* as the export of a checkout at *tmp_path*."""
    path = canary_evidence_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return tmp_path


def milestone(**overrides: Any) -> dict[str, Any]:
    """Return a COMPLETED Milestone record in the declared canary."""
    record: dict[str, Any] = {
        "reference": MEMBERSHIP_REF,
        "project_code": CANARY_CODE,
        "milestone_id": "MS-0001",
        "status": "COMPLETED",
        "bundle_digest": BUNDLE_DIGEST,
        "recorded_at": "2026-09-18T00:00:00+00:00",
    }
    record.update(overrides)
    return record


def with_milestones(*records: dict[str, Any]) -> dict[str, Any]:
    """Return the committed export carrying exactly *records*."""
    document = committed()
    document["milestones"] = list(records)
    return document


def dev3_config(*, membership_refs: tuple[str, ...] = (MEMBERSHIP_REF,)) -> ReleaseConfig:
    """Return the rendered dev3 configuration carrying *membership_refs*."""
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV3_RELEASE_CONFIG_YAML))["release"]
    body["membership_refs"] = list(membership_refs)
    return load_release_config({"release": body}, train=V07_TRAIN)


def probe_at(repo_root: Path, *, config: ReleaseConfig | None = None) -> Any:
    """Return the ``membership`` outcome for a checkout at *repo_root*."""
    context = ReleaseSignalContext(config or dev3_config(), ReleaseSignalName.MEMBERSHIP, None)
    return membership_probe(context, repo_root=repo_root)


def readiness_at(repo_root: Path, *, config: ReleaseConfig | None = None) -> Any:
    """Return a dev3 sweep in which only the checkout-backed rows are real.

    Every other signal is pinned passing, so a red row and the first-red
    denial are attributable to the rows under test rather than to the CI
    receipts a scratch checkout has never carried.
    """
    return compute_readiness(
        config or dev3_config(),
        probes=all_passing() | build_receipt_probes(repo_root),
        computed_at=NOW,
    )


# --- membership: every declared bundle is accepted -------------------


def test_membership_passes_when_every_declared_ref_is_a_completed_milestone(
    tmp_path: Path,
) -> None:
    outcome = probe_at(staged(tmp_path, with_milestones(milestone())))

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == (ACCEPTED_REF,)


def test_membership_passes_for_every_declared_ref_at_once(tmp_path: Path) -> None:
    document = with_milestones(milestone(), milestone(reference=SECOND_REF, milestone_id="MS-0002"))

    outcome = probe_at(
        staged(tmp_path, document),
        config=dev3_config(membership_refs=(MEMBERSHIP_REF, SECOND_REF)),
    )

    assert outcome.status is ReleaseSignalStatus.PASS
    assert len(outcome.evidence_refs) == 2


def test_membership_fails_when_a_declared_ref_resolves_to_nothing(tmp_path: Path) -> None:
    outcome = probe_at(staged(tmp_path, with_milestones()))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.MEMBERSHIP_UNRESOLVED.value in outcome.remediation
    assert MEMBERSHIP_REF in outcome.remediation


def test_membership_fails_when_the_milestone_has_not_completed(tmp_path: Path) -> None:
    outcome = probe_at(staged(tmp_path, with_milestones(milestone(status="ACTIVE"))))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.MILESTONE_INCOMPLETE.value in outcome.remediation
    assert "ACTIVE" in outcome.remediation


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "ACCEPTANCE_REVIEW", "CANCELLED"])
def test_membership_admits_no_status_but_completed(tmp_path: Path, status: str) -> None:
    """Acceptance review is the near miss: sealed is not finished."""
    outcome = probe_at(staged(tmp_path, with_milestones(milestone(status=status))))

    assert outcome.status is ReleaseSignalStatus.FAIL


def test_membership_fails_when_the_milestone_ran_outside_a_declared_canary(
    tmp_path: Path,
) -> None:
    outcome = probe_at(staged(tmp_path, with_milestones(milestone(project_code=OTHER_CODE))))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert CanaryEvidenceGap.UNDECLARED_CANARY.value in outcome.remediation
    assert OTHER_CODE in outcome.remediation


def test_a_refusal_names_only_the_bundle_that_is_missing(tmp_path: Path) -> None:
    outcome = probe_at(
        staged(tmp_path, with_milestones(milestone())),
        config=dev3_config(membership_refs=(MEMBERSHIP_REF, SECOND_REF)),
    )

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert SECOND_REF in outcome.remediation
    assert outcome.remediation.count(CanaryEvidenceGap.MEMBERSHIP_UNRESOLVED.value) == 1


def test_membership_fails_when_the_checkpoint_declares_bundles_and_nothing_backs_them(
    tmp_path: Path,
) -> None:
    """A declaration with no export behind it is unbacked, not unproduced."""
    outcome = probe_at(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "withdraw the references" in outcome.remediation


def test_membership_is_unavailable_when_the_checkpoint_declares_no_bundle() -> None:
    """Every rung before dev3 forbids bundles outright."""
    context = ReleaseSignalContext(dev1_config(), ReleaseSignalName.MEMBERSHIP, None)

    outcome = membership_probe(context, repo_root=REPO_ROOT)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "declares no membership_refs" in outcome.remediation


def test_membership_fails_on_an_export_that_does_not_read_back(tmp_path: Path) -> None:
    path = canary_evidence_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")

    outcome = probe_at(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "does not read back" in outcome.remediation


def test_the_membership_row_reports_membership_unaccepted(tmp_path: Path) -> None:
    row = readiness_at(staged(tmp_path, with_milestones())).row(ReleaseSignalName.MEMBERSHIP)

    assert row.status is ReleaseSignalStatus.FAIL
    assert row.failure_code is ReleaseSignalFailureCode.MEMBERSHIP_UNACCEPTED


def test_the_membership_row_cites_each_accepted_bundle(tmp_path: Path) -> None:
    row = readiness_at(staged(tmp_path, with_milestones(milestone()))).row(
        ReleaseSignalName.MEMBERSHIP
    )

    assert row.status is ReleaseSignalStatus.PASS
    assert row.evidence_refs == (ACCEPTED_REF,)


def test_this_checkout_has_accepted_no_canary_milestone() -> None:
    """The honest state: no Milestone has been driven inside a canary yet."""
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None

    assert evidence.milestones == ()
    assert membership_findings(evidence, (MEMBERSHIP_REF,))[0].gap is (
        CanaryEvidenceGap.MEMBERSHIP_UNRESOLVED
    )
    assert membership_evidence_refs(evidence, (MEMBERSHIP_REF,)) == ()


def test_membership_findings_of_no_declared_ref_is_empty() -> None:
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None

    assert membership_findings(evidence, ()) == ()


# --- canary isolation: an equal recorded digest pair ------------------


def test_the_committed_export_records_an_equal_production_root_pair() -> None:
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None
    record = evidence.isolation
    assert record is not None

    assert record.before_digest == record.after_digest
    assert record.untouched
    assert evidence.isolation_findings() == ()


def test_an_unequal_recorded_pair_is_a_touched_production_root(tmp_path: Path) -> None:
    document = committed()
    document["isolation"]["after_digest"] = f"sha256:{'d' * 64}"
    evidence = load_canary_evidence(staged(tmp_path, document))
    assert evidence is not None

    findings = evidence.isolation_findings()

    assert [finding.gap for finding in findings] == [CanaryEvidenceGap.PRODUCTION_ROOT_TOUCHED]
    assert not findings[0].is_absence
    assert evidence.isolation is not None
    assert not evidence.isolation.untouched


def test_an_unrecorded_pair_is_an_absence_rather_than_a_pass(tmp_path: Path) -> None:
    document = committed()
    document["isolation"] = None
    evidence = load_canary_evidence(staged(tmp_path, document))
    assert evidence is not None

    findings = evidence.isolation_findings()

    assert [finding.gap for finding in findings] == [CanaryEvidenceGap.ISOLATION_UNRECORDED]
    assert findings[0].is_absence


def test_the_isolation_record_names_the_depth_it_was_taken_at() -> None:
    """A narrower observation must not read as the full Milestone rehearsal."""
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None
    assert evidence.isolation is not None

    assert evidence.isolation.rehearsal_scope.strip()
    assert evidence.isolation.production_root_label.strip()


def test_canary_isolation_is_still_settled_by_its_proof_command() -> None:
    """The gate reads a run, not a row, so the sweep never greens it."""
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.CANARY_ISOLATION]

    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.required_signal is None


def test_the_canary_isolation_gate_row_is_unavailable_over_this_checkout() -> None:
    row = readiness_at(REPO_ROOT).gate_row(ReleaseGateName.CANARY_ISOLATION)

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert row.remediation.strip()


# --- every gate name binds exactly one row ---------------------------


def test_the_sweep_reports_one_gate_row_per_profile_gate() -> None:
    readiness = readiness_at(REPO_ROOT)

    assert tuple(row.gate for row in readiness.gates) == profile_gates(PROFILE)
    assert len(readiness.gates) == len({row.gate for row in readiness.gates})


@pytest.mark.parametrize("gate", profile_gates(ReleaseGateProfile.NATIVE_CANARY))
def test_every_native_canary_gate_resolves_to_exactly_one_row(gate: ReleaseGateName) -> None:
    readiness = readiness_at(REPO_ROOT)

    assert readiness.gate_row(gate).gate is gate
    assert sum(1 for row in readiness.gates if row.gate is gate) == 1


@pytest.mark.parametrize("gate", profile_gates(ReleaseGateProfile.NATIVE_CANARY))
def test_every_row_backed_gate_lands_on_a_signal_the_sweep_computed(
    gate: ReleaseGateName,
) -> None:
    """A gate bound to a row nobody computes would pass by reading nothing."""
    binding = gate_bindings_for(PROFILE)[gate]
    if binding.required_signal is None:
        pytest.skip(f"{gate.value} is settled without a readiness row")
    readiness = readiness_at(REPO_ROOT)

    assert readiness.row(binding.required_signal).signal is binding.required_signal


def test_the_provider_gate_row_inherits_the_provider_signal_verdict() -> None:
    readiness = readiness_at(REPO_ROOT)

    assert readiness.gate_row(ReleaseGateName.PROVIDER).status is (
        readiness.row(ReleaseSignalName.PROVIDER).status
    )
    assert readiness.gate_row(ReleaseGateName.PROVIDER).evidence_ref == (
        ReleaseSignalName.PROVIDER.value
    )


def test_the_membership_gate_row_inherits_the_membership_signal_verdict(tmp_path: Path) -> None:
    readiness = readiness_at(staged(tmp_path, with_milestones(milestone())))

    assert readiness.gate_row(ReleaseGateName.MEMBERSHIP).status is ReleaseSignalStatus.PASS
    assert readiness.gate_row(ReleaseGateName.MEMBERSHIP).evidence_ref == (
        ReleaseSignalName.MEMBERSHIP.value
    )


def test_the_three_added_gates_are_required_by_the_dev3_configuration() -> None:
    readiness = readiness_at(REPO_ROOT)

    for gate in NATIVE_CANARY_ADDED_GATES:
        assert readiness.gate_row(gate).required


def test_the_dev3_required_signal_set_now_includes_provider_and_membership() -> None:
    readiness = readiness_at(REPO_ROOT)

    assert ReleaseSignalName.PROVIDER in readiness.required_signals
    assert ReleaseSignalName.MEMBERSHIP in readiness.required_signals


def test_a_sweep_with_an_unaccepted_bundle_names_membership_first(tmp_path: Path) -> None:
    """The denial an operator reads points at the row they have to repair."""
    readiness = readiness_at(staged(tmp_path, with_milestones()))

    assert readiness.first_red is ReleaseSignalName.MEMBERSHIP
    assert not readiness.ready


# --- the REL-037 dev3 legs -------------------------------------------


def test_both_dev3_contract_legs_are_required_at_the_rung() -> None:
    assert required_contract_ids("0.7.0.dev3") == DEV3_CONTRACT_IDS


def test_both_dev3_contract_legs_resolve_to_promotable_contracts() -> None:
    assert set(DEV3_CONTRACT_IDS) <= set(PREFLIGHT_CONTRACTS)
    for contract_id in DEV3_CONTRACT_IDS:
        assert PREFLIGHT_CONTRACTS[contract_id].boundary.strip()
