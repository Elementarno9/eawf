"""The ``0.7.0`` stable checkpoint configuration and the gates it requires.

The stable rung is the last of the v0.7.0 train. Its configuration is
rendered from the ``stable`` profile, so its required list is the
profile's gate set: the whole suite under ``full_suite``, the packet run
read off the realization row, and the turn-cost producer over the live
corpus. The configurations the earlier rungs were cut against stay
byte-identical.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.release.checkpoint_template import DeferredRuntimeClaims
from eawf.kernel.release.gate_binding import GateEvidenceKind, profile_gates
from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import ReleaseChannel, ReleaseGateProfile
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseConfigError,
    ReleaseGateName,
    ReleaseRuntimeClaim,
    parse_release_config,
)
from eawf.observability.bench.turn_cost import LIVE_FIXTURE_ID
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.release_context import resolve_config
from eawf.workflow.release.train import (
    CHECKPOINT_CONFIGS,
    PROFILE_GATE_BINDINGS,
    V07_CONFIG_TEMPLATE,
    V07_TARGET_VERSION,
    gate_bindings_for,
)
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import derive_required_signals

pytestmark = pytest.mark.unit

_MEMBERSHIP = ("eawf://WSP-ABC/PRJ-ABC/REP-ABC/milestone/MLS-0001#MAB-0001-MLS-0001",)

#: The rungs configured before the stable one, in train order.
_EARLIER_VERSIONS = (
    "0.7.0.dev1",
    "0.7.0.dev2",
    "0.7.0.dev3",
    "0.7.0.dev4",
    "0.7.0.dev5",
    "0.7.0rc1",
    "0.7.0rc2",
    "0.7.0rc3",
    "0.7.0rc4",
)
_EARLIER_PROFILES = (
    ReleaseGateProfile.DEV1,
    ReleaseGateProfile.DEV2,
    ReleaseGateProfile.NATIVE_CANARY,
    ReleaseGateProfile.PRODUCT_CANARY,
    ReleaseGateProfile.FLAG_DAY,
    ReleaseGateProfile.CROSS_PROVIDER,
)

#: The digests of those rungs' configurations and binding tables as they
#: stood before the stable rung was added, so adding it cannot move a
#: document an earlier cut bound.
_EARLIER_CONFIGS_DIGEST = (
    "8b20b65e4f1c33ccc12b4e4d40d68b5a6d9647357a833d1fc5e352f7f62e21e4"  # pragma: allowlist secret
)
_EARLIER_BINDINGS_DIGEST = (
    "4ae7c2d53a3cf635ba204218ba3e60dcd65b4153cb6990fa6f88163eef10ccd7"  # pragma: allowlist secret
)


def _stable() -> ReleaseConfig:
    return resolve_config(V07_TARGET_VERSION, membership_refs=_MEMBERSHIP)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_stable_target_resolves_to_a_stable_profile_config() -> None:
    config = _stable()

    assert config.version == "0.7.0"
    assert config.channel is ReleaseChannel.STABLE
    assert config.gates.profile is ReleaseGateProfile.STABLE
    assert config.gates.required == profile_gates(ReleaseGateProfile.STABLE)


def test_the_stable_config_requires_membership_like_every_epoch_two_rung() -> None:
    with pytest.raises(DaemonValidationError, match="membership"):
        resolve_config(V07_TARGET_VERSION)


def test_a_version_past_the_train_still_has_no_config() -> None:
    with pytest.raises(DaemonValidationError, match=r"no release configuration for '0\.7\.1'"):
        resolve_config("0.7.1", membership_refs=_MEMBERSHIP)


def test_the_stable_config_requires_full_suite_packet_run_and_telemetry() -> None:
    required = set(_stable().gates.required)

    assert {
        ReleaseGateName.FULL_SUITE,
        ReleaseGateName.PACKET_RUN,
        ReleaseGateName.TELEMETRY_PRODUCER,
    } <= required
    assert ReleaseGateName.EPOCH1_STABILIZATION not in required
    assert ReleaseGateName.MIGRATION not in required


def test_every_stable_gate_has_exactly_one_binding() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.STABLE)

    assert tuple(bindings) == _stable().gates.required


def test_the_packet_run_gate_reads_the_realization_row() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.STABLE)[ReleaseGateName.PACKET_RUN]

    assert binding.kind is GateEvidenceKind.SIGNAL
    assert binding.signal is ReleaseSignalName.PERFECT_REALIZATION
    assert ReleaseSignalName.PERFECT_REALIZATION in derive_required_signals(_stable())


def test_no_rung_before_stable_requires_the_packet_run() -> None:
    for version in _EARLIER_VERSIONS:
        config = resolve_config(version, membership_refs=_membership_for(version))
        assert ReleaseGateName.PACKET_RUN not in config.gates.required
        assert ReleaseSignalName.PERFECT_REALIZATION not in derive_required_signals(config)


def _membership_for(version: str) -> tuple[str, ...]:
    # The epoch-1 rungs forbid membership refs; every later rung requires them.
    return () if version in {"0.7.0.dev1", "0.7.0.dev2"} else _MEMBERSHIP


def test_the_stable_telemetry_gate_runs_the_bench_on_the_live_fixture() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.STABLE)[ReleaseGateName.TELEMETRY_PRODUCER]

    assert binding.proof is not None
    argv = binding.proof.argv
    assert argv[:5] == ("uv", "run", "eawf", "bench", "turn-cost")
    assert argv[argv.index("--fixture") + 1] == LIVE_FIXTURE_ID


def test_the_stable_config_claims_claude_code_and_codex() -> None:
    needs = ("tool_use", "streaming")
    assert _stable().runtime_claims == (
        ReleaseRuntimeClaim(runtime_id="claude-code", required_capabilities=needs),
        ReleaseRuntimeClaim(runtime_id="codex", required_capabilities=needs),
    )


def test_no_earlier_config_declares_its_runtimes() -> None:
    for version in _EARLIER_VERSIONS:
        assert parse_release_config(CHECKPOINT_CONFIGS[version]).runtime_claims == ()
        assert "runtime_claims" not in CHECKPOINT_CONFIGS[version]


@pytest.mark.parametrize(
    ("profile", "count"),
    [(ReleaseGateProfile.CROSS_PROVIDER, 0), (ReleaseGateProfile.STABLE, 2)],
)
def test_runtime_claims_join_at_the_stable_profile(profile: ReleaseGateProfile, count: int) -> None:
    assert len(V07_CONFIG_TEMPLATE.runtime_claims_for(profile)) == count


def test_a_deferred_runtime_claim_set_is_never_empty() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        DeferredRuntimeClaims(from_profile=ReleaseGateProfile.STABLE, claims=())


@pytest.mark.parametrize(
    "claim",
    [
        {"runtime_id": "codex", "required_capabilities": []},
        {"runtime_id": "", "required_capabilities": ["tool_use"]},
        {"runtime_id": "x" * 65, "required_capabilities": ["tool_use"]},
        {"runtime_id": "codex", "required_capabilities": ["Tool Use"]},
        {"runtime_id": "codex"},
        {"runtime_id": "codex", "required_capabilities": ["tool_use"], "extra": 1},
        {"runtime_id": 7, "required_capabilities": ["tool_use"]},
    ],
)
def test_a_malformed_runtime_claim_is_refused(claim: dict[str, object]) -> None:
    with pytest.raises(ReleaseConfigError, match="schema"):
        parse_release_config(
            CHECKPOINT_CONFIGS[V07_TARGET_VERSION].replace(
                "  runtime_claims:", f"  runtime_claims:\n  - {json.dumps(claim)}", 1
            )
        )


def test_a_runtime_id_at_the_length_limit_is_accepted() -> None:
    claim = ReleaseRuntimeClaim(runtime_id="x" * 64, required_capabilities=("tool_use",))
    assert len(claim.runtime_id) == 64


def test_every_earlier_config_is_unchanged() -> None:
    text = "".join(f"{version}\n{CHECKPOINT_CONFIGS[version]}" for version in _EARLIER_VERSIONS)

    assert _sha256(text) == _EARLIER_CONFIGS_DIGEST


def test_every_earlier_binding_table_is_unchanged() -> None:
    text = "".join(
        f"{profile.value}\n{PROFILE_GATE_BINDINGS[profile]}" for profile in _EARLIER_PROFILES
    )

    assert _sha256(text) == _EARLIER_BINDINGS_DIGEST


def _changelog_status(tmp_path: Path, section: str, config: ReleaseConfig) -> ReleaseSignalStatus:
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{config.version}]\n\n{section}\n## [0.6.8]\n\n- old\n",
        encoding="utf-8",
    )
    inputs = TagPreflightInputs(
        repo_root=tmp_path,
        version=config.version,
        tag=f"v{config.version}",
        remote="origin",
        package_version=config.version,
    )
    probe = build_tag_probes(inputs)[ReleaseSignalName.CHANGELOG]
    outcome = probe(
        ReleaseSignalContext(
            config=config, signal=ReleaseSignalName.CHANGELOG, observed_revision=None
        )
    )
    return outcome.status


def test_the_stable_changelog_must_state_the_migration_path(tmp_path: Path) -> None:
    silent = "- Publish 0.7.0.\n"

    assert _changelog_status(tmp_path, silent, _stable()) is ReleaseSignalStatus.FAIL


def test_the_stable_changelog_passes_once_it_names_the_migration(tmp_path: Path) -> None:
    noted = "- Migration: a 0.6 install upgrades through 0.7.0rc4 first.\n"

    assert _changelog_status(tmp_path, noted, _stable()) is ReleaseSignalStatus.PASS


def test_a_candidate_changelog_still_passes_without_a_migration_note(tmp_path: Path) -> None:
    candidate = resolve_config("0.7.0rc4", membership_refs=_MEMBERSHIP)

    assert _changelog_status(tmp_path, "- A fix.\n", candidate) is ReleaseSignalStatus.PASS
