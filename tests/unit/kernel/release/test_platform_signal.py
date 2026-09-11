"""Tests for the platform signal and what the front-door gate reads.

The platform row is the one signal whose evidence can be forged by
accident: a journey run against a stub looks exactly like a journey that
ran, and the difference only surfaces in a user's install. So the module
pins:

1. The row computes at ``dev1`` from the checkpoint's claims -- the
   authored configuration advertises the Linux real-host CI receipt, and
   the row passes carrying that receipt.
2. An empty claim set is ``unavailable`` with remediation, never
   ``pass``. A checkpoint that advertises nothing has proven nothing,
   and a green row there would satisfy a gate by being vacuous.
3. A claim whose receipt did not come from a real host fails, naming the
   platform.
4. ``front_door_journey`` binds the install-smoke journey proof and
   never the platform row -- and no gate of the profile binds that row,
   so a green platform can never stand in for an install nobody
   attempted.
"""

from __future__ import annotations

import pytest

from eawf.kernel.release.gate_binding import GateEvidenceKind
from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseConfigError,
    ReleaseConfigRejection,
    ReleaseGateName,
)
from eawf.workflow.release.signal_probes import DEFAULT_RELEASE_PROBES, platform_probe
from eawf.workflow.release.train import gate_bindings_for
from eawf.workflow.verify.release_readiness import compute_readiness
from tests._release_helpers import NOW, dev1_config

_LINUX_RECEIPT = "ci://eawf/.github/workflows/ci.yaml#linux-jail"


def _claim(**overrides: object) -> dict[str, object]:
    """Return the authored Linux claim with *overrides* applied."""
    claim: dict[str, object] = {
        "platform_id": "linux-x86_64",
        "receipt_ref": _LINUX_RECEIPT,
        "real_host": True,
    }
    claim.update(overrides)
    return claim


def _outcome(config: ReleaseConfig) -> ReleaseSignalOutcome:
    """Run the platform probe against *config*."""
    return platform_probe(ReleaseSignalContext(config, ReleaseSignalName.PLATFORM, None))


# --- the dev1 claim --------------------------------------------------


def test_dev1_advertises_exactly_the_linux_real_host_claim(config: ReleaseConfig) -> None:
    assert len(config.platform_claims) == 1
    claim = config.platform_claims[0]
    assert claim.platform_id == "linux-x86_64"
    assert claim.receipt_ref == _LINUX_RECEIPT
    assert claim.real_host is True


def test_the_platform_row_computes_at_dev1_from_the_linux_receipt() -> None:
    row = compute_readiness(dev1_config(), computed_at=NOW).row(ReleaseSignalName.PLATFORM)
    assert row.status is ReleaseSignalStatus.PASS
    assert row.evidence_refs == (_LINUX_RECEIPT,)
    assert row.failure_code is None


def test_the_platform_producer_is_registered_by_default() -> None:
    assert DEFAULT_RELEASE_PROBES[ReleaseSignalName.PLATFORM] is platform_probe


def test_an_injected_probe_still_wins_over_the_default() -> None:
    def stub(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL, remediation="pinned by the caller"
        )

    readiness = compute_readiness(
        dev1_config(),
        probes={ReleaseSignalName.PLATFORM: stub},
        computed_at=NOW,
    )
    assert readiness.row(ReleaseSignalName.PLATFORM).remediation == "pinned by the caller"


# --- an empty claim set ----------------------------------------------


def test_no_claim_reports_unavailable_with_remediation() -> None:
    outcome = _outcome(dev1_config(platform_claims=[]))
    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "advertises no platform" in outcome.remediation


def test_no_claim_leaves_the_sweep_row_unavailable_not_passing() -> None:
    row = compute_readiness(dev1_config(platform_claims=[]), computed_at=NOW).row(
        ReleaseSignalName.PLATFORM
    )
    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert row.remediation


# --- an unproven claim -----------------------------------------------


def test_a_claim_without_a_real_host_receipt_fails_naming_the_platform() -> None:
    outcome = _outcome(dev1_config(platform_claims=[_claim(real_host=False)]))
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "linux-x86_64" in outcome.remediation


def test_one_unproven_claim_among_several_fails_the_row() -> None:
    outcome = _outcome(
        dev1_config(
            platform_claims=[
                _claim(),
                _claim(platform_id="darwin-arm64", real_host=False),
            ]
        )
    )
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "darwin-arm64" in outcome.remediation


def test_two_real_host_claims_pass_carrying_both_receipts() -> None:
    outcome = _outcome(
        dev1_config(
            platform_claims=[
                _claim(),
                _claim(platform_id="darwin-arm64", receipt_ref="ci://eawf/macos"),
            ]
        )
    )
    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == (_LINUX_RECEIPT, "ci://eawf/macos")


# --- claim validation ------------------------------------------------


def test_the_loader_refuses_a_duplicated_platform_claim() -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        dev1_config(platform_claims=[_claim(), _claim(receipt_ref="ci://eawf/other")])
    assert excinfo.value.code is ReleaseConfigRejection.DUPLICATE_PLATFORM_CLAIM


@pytest.mark.parametrize("platform_id", ["", "Linux", "9linux", "a" * 33])
def test_the_loader_refuses_a_malformed_platform_id(platform_id: str) -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        dev1_config(platform_claims=[_claim(platform_id=platform_id)])
    assert excinfo.value.code is ReleaseConfigRejection.SCHEMA_INVALID


def test_the_loader_refuses_a_claim_with_a_blank_receipt() -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        dev1_config(platform_claims=[_claim(receipt_ref="")])
    assert excinfo.value.code is ReleaseConfigRejection.SCHEMA_INVALID


def test_the_loader_refuses_an_unknown_claim_field() -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        dev1_config(platform_claims=[_claim(runner="ubuntu-24.04")])
    assert excinfo.value.code is ReleaseConfigRejection.SCHEMA_INVALID


def test_a_single_claim_is_the_boundary_that_still_computes() -> None:
    outcome = _outcome(dev1_config(platform_claims=[_claim()]))
    assert outcome.status is ReleaseSignalStatus.PASS


# --- what the front-door gate reads ----------------------------------


def test_front_door_journey_binds_the_install_smoke_journey_proof() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[ReleaseGateName.FRONT_DOOR_JOURNEY]
    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.proof is not None
    assert binding.proof.command_id == "front_door_install_smoke"
    assert binding.signal is None


def test_no_dev1_gate_binds_the_platform_row() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    assert all(binding.signal is not ReleaseSignalName.PLATFORM for binding in bindings.values())


def test_the_platform_row_is_not_in_the_derived_required_set(config: ReleaseConfig) -> None:
    readiness = compute_readiness(config, computed_at=NOW)
    assert ReleaseSignalName.PLATFORM not in readiness.required_signals
