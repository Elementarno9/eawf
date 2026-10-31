"""The ``0.7.0rc2`` rung renders under ``cross_provider`` with every gate bound.

``rc2`` is the second release candidate and the first rung to advertise
Windows beside Linux, so its profile adds one gate to the twenty-three
``flag_day`` gates: the ``platform`` row, which passes only when every
advertised claim carries a real-host receipt. These tests pin the gate
set, the rendered configuration with its four targets and two claims,
the binding table behind it, the committed canary export the membership
rung resolves its bundle against, and the gate reddening when a claim
loses its real-host receipt.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest
import yaml
from packaging.version import Version
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.release.checkpoint_template import (
    CONFIG_ROOT_KEY,
    DeferredPlatformClaims,
    render_checkpoint_config,
    with_membership_refs,
)
from eawf.kernel.release.gate_binding import (
    CROSS_PROVIDER_ADDED_GATES,
    GateBindingError,
    GateBindingRejection,
    GateEvidenceKind,
    load_gate_bindings,
    profile_gates,
)
from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseGateName,
    load_release_config,
    parse_release_config,
)
from eawf.workflow.evidence.provider_certification import load_canary_evidence
from eawf.workflow.release.admission import checkpoint_release_config
from eawf.workflow.release.train import (
    CHECKPOINT_CONFIGS,
    CROSS_PROVIDER_GATE_BINDINGS_YAML,
    RC1_RELEASE_CONFIG_YAML,
    RC2_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
    gate_bindings_for,
)
from eawf.workflow.verify.release_readiness import compute_readiness

pytestmark = pytest.mark.integration

#: This checkout, whose committed export is the real evidence.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

RC2_VERSION: Final = "0.7.0rc2"
RC2_KEY: Final = f"REL-{RC2_VERSION}"
PROFILE: Final = ReleaseGateProfile.CROSS_PROVIDER

LINUX_RECEIPT: Final = "ci://eawf/.github/workflows/ci.yaml#linux-jail"
WINDOWS_RECEIPT: Final = "ci://eawf/.github/workflows/ci.yaml#windows"
WINDOWS_PLATFORM: Final = "windows-x86_64"

#: A fixed sweep instant, so the readiness rows do not depend on the clock.
NOW: Final = datetime(2026, 10, 8, tzinfo=UTC)


def binding_rows() -> list[dict[str, Any]]:
    """Return the authored cross-provider binding rows as mutable dicts."""
    decoded: dict[str, Any] = yaml.safe_load(CROSS_PROVIDER_GATE_BINDINGS_YAML)
    rows: list[dict[str, Any]] = decoded["bindings"]
    return rows


def rc2_config() -> ReleaseConfig:
    """Return the rc2 configuration as tag time resolves it."""
    return checkpoint_release_config(RC2_VERSION, repo_root=REPO_ROOT)


def rc2_config_with_claims(claims: list[dict[str, object]]) -> ReleaseConfig:
    """Return the rc2 configuration advertising *claims* instead of its own."""
    document = with_membership_refs(
        RC2_RELEASE_CONFIG_YAML, membership_refs=rc2_config().membership_refs
    )
    body = dict(cast(Mapping[str, Any], document[CONFIG_ROOT_KEY]))
    body["platform_claims"] = claims
    return load_release_config({CONFIG_ROOT_KEY: body}, train=V07_TRAIN)


def _passing(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
    """Stand in for every row but ``platform``, so only the claim decides."""
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS)


#: Every probe but the platform one pinned green; the platform row runs
#: the registered producer against the configuration's real claims.
OTHER_ROWS_PASS: Final[dict[ReleaseSignalName, ReleaseSignalProbe]] = {
    signal: _passing for signal in ReleaseSignalName if signal is not ReleaseSignalName.PLATFORM
}


def platform_gate_status(config: ReleaseConfig) -> tuple[ReleaseSignalStatus, bool]:
    """Return the platform gate's verdict and the sweep's readiness for *config*."""
    readiness = compute_readiness(config, probes=OTHER_ROWS_PASS, computed_at=NOW)
    (row,) = [row for row in readiness.gates if row.gate is ReleaseGateName.PLATFORM]
    assert row.required
    return row.status, readiness.ready


def test_cross_provider_is_the_flag_day_gates_plus_platform() -> None:
    gates = profile_gates(PROFILE)

    assert gates == (*profile_gates(ReleaseGateProfile.FLAG_DAY), *CROSS_PROVIDER_ADDED_GATES)
    assert CROSS_PROVIDER_ADDED_GATES == (ReleaseGateName.PLATFORM,)
    assert len(gates) == 24


def test_rc2_config_is_rendered_from_the_train_template() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version(RC2_VERSION),
        template=V07_CONFIG_TEMPLATE,
    )

    assert rendered == RC2_RELEASE_CONFIG_YAML
    assert checkpoint_config_yaml(RC2_VERSION) == RC2_RELEASE_CONFIG_YAML


def test_rc2_config_requires_the_twenty_four_cross_provider_gates() -> None:
    config = parse_release_config(RC2_RELEASE_CONFIG_YAML)

    assert config.version == RC2_VERSION
    assert config.authority_epoch == 2
    assert config.gates.profile is PROFILE
    assert config.gates.required == profile_gates(PROFILE)


def test_rc2_config_declares_the_four_rc1_targets() -> None:
    rc2 = parse_release_config(RC2_RELEASE_CONFIG_YAML)
    rc1 = parse_release_config(RC1_RELEASE_CONFIG_YAML)

    assert [t.target_id for t in rc2.targets] == ["pypi", "npm", "github", "plugins-dist"]
    assert rc2.targets == rc1.targets


def test_rc2_claims_windows_beside_the_linux_claim_rc1_carried() -> None:
    rc2 = parse_release_config(RC2_RELEASE_CONFIG_YAML)
    rc1 = parse_release_config(RC1_RELEASE_CONFIG_YAML)

    assert rc2.platform_claims[: len(rc1.platform_claims)] == rc1.platform_claims
    assert [(c.platform_id, c.receipt_ref, c.real_host) for c in rc2.platform_claims] == [
        ("linux-x86_64", LINUX_RECEIPT, True),
        (WINDOWS_PLATFORM, WINDOWS_RECEIPT, True),
    ]


def test_the_windows_claim_names_a_job_the_ci_workflow_declares() -> None:
    workflow: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    )
    job = WINDOWS_RECEIPT.rsplit("#", 1)[1]

    assert workflow["jobs"][job]["runs-on"].startswith("windows-")


@pytest.mark.parametrize(
    "version",
    sorted(
        version
        for version in CHECKPOINT_CONFIGS
        if V07_TRAIN.checkpoints.index(V07_TRAIN.checkpoint_for_version(version))
        < V07_TRAIN.checkpoints.index(V07_TRAIN.checkpoint_for_version(RC2_VERSION))
    ),
)
def test_no_earlier_rung_gains_the_windows_claim(version: str) -> None:
    """The deferred claim leaves every already-configured rung as it rendered."""
    claims = parse_release_config(CHECKPOINT_CONFIGS[version]).platform_claims

    assert WINDOWS_PLATFORM not in {claim.platform_id for claim in claims}


@pytest.mark.parametrize(
    ("profile", "carried"),
    [
        (ReleaseGateProfile.FLAG_DAY, False),
        (ReleaseGateProfile.CROSS_PROVIDER, True),
        (ReleaseGateProfile.STABLE, True),
    ],
)
def test_the_windows_claim_joins_at_cross_provider(
    profile: ReleaseGateProfile, carried: bool
) -> None:
    """The off-by-one boundary: the rung before the join keeps one claim."""
    claims = V07_CONFIG_TEMPLATE.platform_claims_for(profile)

    assert (WINDOWS_PLATFORM in {claim["platform_id"] for claim in claims}) is carried
    assert claims[0]["platform_id"] == "linux-x86_64"


def test_deferred_platform_claims_refuse_an_empty_claim_list() -> None:
    with pytest.raises(ValidationError):
        DeferredPlatformClaims(from_profile=PROFILE, claims=())


def test_deferred_platform_claims_refuse_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        DeferredPlatformClaims.model_validate(
            {"from_profile": PROFILE.value, "claims": [{}], "runner": "windows-latest"}
        )


def test_deferred_platform_claims_refuse_an_unknown_profile() -> None:
    with pytest.raises(ValidationError):
        DeferredPlatformClaims.model_validate({"from_profile": "rc9", "claims": [{}]})


def test_every_rc2_gate_has_a_binding() -> None:
    config = parse_release_config(RC2_RELEASE_CONFIG_YAML)

    assert tuple(gate_bindings_for(config.gates.profile)) == config.gates.required


def test_the_platform_gate_reads_the_platform_row() -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.PLATFORM]

    assert binding.kind is GateEvidenceKind.SIGNAL
    assert binding.signal is ReleaseSignalName.PLATFORM


def test_the_platform_row_is_required_at_rc2() -> None:
    readiness = compute_readiness(rc2_config(), probes=OTHER_ROWS_PASS, computed_at=NOW)

    assert ReleaseSignalName.PLATFORM in readiness.required_signals


def test_the_platform_gate_passes_on_both_real_host_receipts() -> None:
    readiness = compute_readiness(rc2_config(), probes=OTHER_ROWS_PASS, computed_at=NOW)

    row = readiness.row(ReleaseSignalName.PLATFORM)
    assert row.status is ReleaseSignalStatus.PASS
    assert row.evidence_refs == (LINUX_RECEIPT, WINDOWS_RECEIPT)
    assert platform_gate_status(rc2_config()) == (ReleaseSignalStatus.PASS, True)


def test_the_platform_gate_fails_when_the_windows_receipt_is_not_real_host() -> None:
    """Gate-fire proof: a Windows claim off a stub reddens the gate and the sweep."""
    claims: list[dict[str, object]] = [
        {"platform_id": "linux-x86_64", "receipt_ref": LINUX_RECEIPT, "real_host": True},
        {"platform_id": WINDOWS_PLATFORM, "receipt_ref": WINDOWS_RECEIPT, "real_host": False},
    ]

    status, ready = platform_gate_status(rc2_config_with_claims(claims))

    assert status is ReleaseSignalStatus.FAIL
    assert ready is False


def test_the_platform_gate_is_unavailable_when_every_claim_is_withdrawn() -> None:
    """The empty boundary: no claim proves nothing, so the gate cannot pass."""
    status, ready = platform_gate_status(rc2_config_with_claims([]))

    assert status is ReleaseSignalStatus.UNAVAILABLE
    assert ready is False


def test_the_platform_gate_left_unbound_is_refused() -> None:
    """Dropping the cross-provider binding reds the load."""
    rows = [row for row in binding_rows() if row["gate"] != ReleaseGateName.PLATFORM.value]

    with pytest.raises(GateBindingError) as caught:
        load_gate_bindings({"bindings": rows}, profile=PROFILE)

    assert caught.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    assert ReleaseGateName.PLATFORM.value in str(caught.value)


def test_the_platform_binding_is_refused_under_flag_day() -> None:
    """The earlier profile does not admit the gate cross-provider adds."""
    with pytest.raises(GateBindingError) as caught:
        load_gate_bindings({"bindings": binding_rows()}, profile=ReleaseGateProfile.FLAG_DAY)

    assert caught.value.code is GateBindingRejection.UNBOUND_GATE_OUTSIDE_PROFILE


def test_rc2_resolves_its_membership_off_the_committed_export() -> None:
    evidence = load_canary_evidence(REPO_ROOT, RC2_KEY)
    config = rc2_config()

    assert evidence is not None
    assert evidence.release_key == RC2_KEY
    assert len(config.membership_refs) == 1
    accepted = evidence.milestone_for(config.membership_refs[0])
    assert accepted is not None
    assert accepted.status.value == "COMPLETED"


def test_the_package_version_has_reached_rc2() -> None:
    assert Version(__version__) >= Version(RC2_VERSION)
