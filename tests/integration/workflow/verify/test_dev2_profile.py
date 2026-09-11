"""The twelve ``dev2`` gates, each bound to exactly one piece of evidence.

A gate profile is decorative until every name in it resolves to
something a sweep computed. These tests pin three claims about the
``dev2`` profile.

**It is a superset.** ``dev2`` admits the eight gates ``dev1`` admitted
plus four more. A checkpoint that dropped a gate its predecessor passed
could not claim the train stabilizes monotonically, so the subset
relation is asserted rather than assumed from two literal lists.

**Every name resolves once.** Each of the twelve binds exactly one
evidence source and lands as exactly one row on a computed readiness
object -- not zero (a gate that reads nothing passes vacuously) and not
two (two rows for one gate can disagree).

**The configuration is rendered, not overlaid.** The ``dev2``
configuration comes from the train template and the rung. The tempting
alternative -- take the authored ``dev1`` file and overlay the version
and the channel onto it -- is exercised here and refused, because that
copy would carry ``dev1``'s profile and ``dev1``'s eight-gate required
list under the ``dev2`` version's name.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.release.checkpoint_template import (
    CheckpointConfigTemplate,
    render_checkpoint_config,
)
from eawf.kernel.release.gate_binding import (
    DEV1_GATES,
    DEV2_ADDED_GATES,
    PROFILE_GATES,
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
from eawf.kernel.release.waiver import ReleaseWaiver
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseConfigError,
    ReleaseConfigRejection,
    ReleaseGateName,
    load_release_config,
)
from eawf.workflow.release.train import (
    DEV1_RELEASE_CONFIG_YAML,
    DEV2_GATE_BINDINGS_YAML,
    DEV2_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
    gate_bindings_for,
)
from eawf.workflow.verify.release_readiness import (
    WaiverAcknowledgement,
    compute_readiness,
)

#: The checkpoint under test, and the rung the train declares for it.
DEV2_VERSION = "0.7.0.dev2"
DEV2_RUNG = V07_TRAIN.checkpoint_for_version(DEV2_VERSION)

#: Instant every sweep here is computed at.
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

#: The twelve names the profile admits, in declaration order.
DEV2_GATE_NAMES = PROFILE_GATES[ReleaseGateProfile.DEV2]


def dev2_config() -> ReleaseConfig:
    """Return the generated ``dev2`` configuration, loaded against the train."""
    return load_release_config(DEV2_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def dev1_body_overlaid(**overrides: Any) -> dict[str, Any]:
    """Return the authored ``dev1`` configuration body with *overrides* applied."""
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    body.update(overrides)
    return {"release": body}


def all_signals_passing() -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry in which every signal passes."""

    def passing(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        del context
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS)

    return dict.fromkeys(ReleaseSignalName, passing)


# --- the profile is a superset of dev1 ---------------------------------------


def test_dev2_profile_admits_the_dev1_eight_plus_four() -> None:
    """The twelve are the eight, in order, then the four additions."""
    assert (*DEV1_GATES, *DEV2_ADDED_GATES) == DEV2_GATE_NAMES
    assert len(DEV2_GATE_NAMES) == 12


def test_dev2_profile_drops_no_gate_the_predecessor_passed() -> None:
    """Monotonic stabilization: every dev1 gate survives into dev2."""
    assert set(DEV1_GATES) <= set(DEV2_GATE_NAMES)


def test_dev2_profile_adds_the_four_epoch2_protections() -> None:
    """The additions are migration, hosted runner, strictness and waivers."""
    assert DEV2_ADDED_GATES == (
        ReleaseGateName.MIGRATION,
        ReleaseGateName.HOSTED_GATE_RUNNER,
        ReleaseGateName.SCHEMA_STRICTNESS,
        ReleaseGateName.WAIVER_COUNT,
    )


# --- the loader accepts the generated configuration ---------------------------


def test_dev2_profile_config_loads_against_the_train() -> None:
    """The rendered configuration is accepted by the strict loader."""
    config = dev2_config()

    assert config.release_key == "REL-0.7.0.dev2"
    assert config.version == DEV2_VERSION
    assert config.gates.profile is ReleaseGateProfile.DEV2
    assert config.authority_epoch == DEV2_RUNG.authority_epoch


def test_dev2_profile_config_requires_every_name_the_profile_admits() -> None:
    """The required list is the profile's gate set, not a second authored list."""
    assert dev2_config().gates.required == DEV2_GATE_NAMES


def test_dev2_profile_config_is_registered_for_the_version() -> None:
    """The generated text is what the checkpoint lookup hands a caller."""
    assert checkpoint_config_yaml(DEV2_VERSION) == DEV2_RELEASE_CONFIG_YAML


def test_dev2_profile_config_is_rendered_from_the_rung_and_the_template() -> None:
    """Re-rendering from the same inputs reproduces the registered text."""
    assert (
        render_checkpoint_config(rung=DEV2_RUNG, template=V07_CONFIG_TEMPLATE)
        == DEV2_RELEASE_CONFIG_YAML
    )


def test_dev2_profile_template_carries_no_version_to_overlay() -> None:
    """A template that could name a version would be a configuration."""
    assert "version" not in CheckpointConfigTemplate.model_fields
    assert "gates" not in CheckpointConfigTemplate.model_fields


# --- the overlay onto the dev1 instance is refused ---------------------------


def test_dev2_profile_refuses_a_version_and_channel_overlay_onto_dev1() -> None:
    """Overlaying dev2's version onto the dev1 file is refused, not accepted."""
    overlaid = dev1_body_overlaid(version=DEV2_VERSION, channel="dev")

    with pytest.raises(ReleaseConfigError) as excinfo:
        load_release_config(overlaid, train=V07_TRAIN)

    assert excinfo.value.code is ReleaseConfigRejection.UNDECLARED_GATE_PROFILE
    assert "dev1" in str(excinfo.value)
    assert "dev2" in str(excinfo.value)


def test_dev2_profile_overlay_would_have_carried_the_dev1_eight() -> None:
    """The overlay's required list is dev1's eight, which is why it is refused."""
    overlaid = dev1_body_overlaid(version=DEV2_VERSION, channel="dev")

    assert tuple(overlaid["release"]["gates"]["required"]) == tuple(
        gate.value for gate in DEV1_GATES
    )


def test_dev2_profile_refuses_an_overlay_that_also_renames_the_profile() -> None:
    """Renaming the profile without the gate list leaves a gate unbound."""
    body = dev1_body_overlaid(version=DEV2_VERSION, channel="dev")
    body["release"]["gates"] = {
        "profile": "dev2",
        "required": [gate.value for gate in DEV1_GATES],
    }

    config = load_release_config(body, train=V07_TRAIN)

    assert set(config.gates.required) < set(DEV2_GATE_NAMES)
    assert ReleaseGateName.MIGRATION not in config.gates.required


# --- every name resolves to exactly one binding and one row -------------------


def test_dev2_profile_binds_every_gate_exactly_once() -> None:
    """The authored table covers the twelve with no duplicate and no stray."""
    bindings = gate_bindings_for(ReleaseGateProfile.DEV2)

    assert tuple(bindings) == DEV2_GATE_NAMES


@pytest.mark.parametrize("gate", DEV2_GATE_NAMES, ids=lambda gate: gate.value)
def test_dev2_profile_gate_resolves_to_one_evidence_source(gate: ReleaseGateName) -> None:
    """Each name carries exactly one non-empty evidence reference."""
    binding = gate_bindings_for(ReleaseGateProfile.DEV2)[gate]

    assert binding.gate is gate
    assert binding.evidence_ref


@pytest.mark.parametrize("gate", DEV2_GATE_NAMES, ids=lambda gate: gate.value)
def test_dev2_profile_gate_lands_as_exactly_one_readiness_row(gate: ReleaseGateName) -> None:
    """A computed sweep reports the gate once, and reports it as required."""
    readiness = compute_readiness(dev2_config(), computed_at=NOW)

    matching = [row for row in readiness.gates if row.gate is gate]
    assert len(matching) == 1
    assert matching[0].required is True


def test_dev2_profile_sweep_reports_twelve_gate_rows() -> None:
    """The gate block is the profile's twelve names and nothing else."""
    readiness = compute_readiness(dev2_config(), computed_at=NOW)

    assert tuple(row.gate for row in readiness.gates) == DEV2_GATE_NAMES


def test_dev2_profile_migration_gate_binds_the_migration_row() -> None:
    """The new row-backed gate reads the whole migration signal."""
    binding = gate_bindings_for(ReleaseGateProfile.DEV2)[ReleaseGateName.MIGRATION]

    assert binding.kind is GateEvidenceKind.SIGNAL
    assert binding.required_signal is ReleaseSignalName.MIGRATION
    assert binding.evidence_ref == ReleaseSignalName.MIGRATION.value


def test_dev2_profile_migration_row_joins_the_derived_required_set() -> None:
    """Binding the row is what puts it in the set the sweep must green."""
    readiness = compute_readiness(dev2_config(), computed_at=NOW)

    assert ReleaseSignalName.MIGRATION in readiness.required_signals


@pytest.mark.parametrize(
    "gate",
    (ReleaseGateName.HOSTED_GATE_RUNNER, ReleaseGateName.SCHEMA_STRICTNESS),
    ids=lambda gate: gate.value,
)
def test_dev2_profile_proof_gate_carries_a_runnable_argv(gate: ReleaseGateName) -> None:
    """A proof gate names a command, and the command is pinnable to a revision."""
    binding = gate_bindings_for(ReleaseGateProfile.DEV2)[gate]

    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.required_signal is None
    resolved = binding.resolve_proof("a" * 40)
    assert resolved.argv[0] == "uv"
    assert Path(resolved.argv[3]).suffix == ".py"


def test_dev2_profile_waiver_gate_reads_the_waiver_block() -> None:
    """The twelfth gate binds no row: waivers are counted, not probed."""
    binding = gate_bindings_for(ReleaseGateProfile.DEV2)[ReleaseGateName.WAIVER_COUNT]

    assert binding.kind is GateEvidenceKind.WAIVER_BLOCK
    assert binding.required_signal is None
    assert binding.evidence_ref == "readiness:waivers"


def test_dev2_profile_waiver_gate_passes_when_nothing_is_waived() -> None:
    """No counted waiver is a clean waiver block, not an unavailable one."""
    readiness = compute_readiness(dev2_config(), computed_at=NOW)

    row = readiness.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert row.status is ReleaseSignalStatus.PASS


def test_dev2_profile_waiver_gate_reds_on_an_unexplained_waiver() -> None:
    """A waiver counted with no explanation reds the gate that reports it."""
    readiness = compute_readiness(
        dev2_config(), computed_at=NOW, waiver_count=1, waivers=(ReleaseWaiver(),)
    )

    row = readiness.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert row.status is ReleaseSignalStatus.FAIL
    assert "nothing to acknowledge" in row.remediation


def test_dev2_profile_waiver_gate_reds_while_an_explained_waiver_is_unacknowledged() -> None:
    """A fully explained waiver still blocks until the operator accepts the loss."""
    waiver = ReleaseWaiver(
        scope="P32-I01-W20",
        reason="the hosted runner receipt has not landed",
        protected_principal="gate evidence",
    )

    readiness = compute_readiness(dev2_config(), computed_at=NOW, waiver_count=1, waivers=(waiver,))

    row = readiness.gate_row(ReleaseGateName.WAIVER_COUNT)
    assert row.status is ReleaseSignalStatus.FAIL
    assert "P32-I01-W20/gate evidence" in row.remediation


def test_dev2_profile_waiver_gate_clears_once_acknowledged() -> None:
    """The acknowledgement is what turns the block green, and it names the loss."""
    waiver = ReleaseWaiver(
        scope="P32-I01-W20",
        reason="the hosted runner receipt has not landed",
        protected_principal="gate evidence",
    )
    acknowledgement = WaiverAcknowledgement(
        scope=waiver.scope,
        protected_principal=waiver.protected_principal,
        acknowledged_by="release-operator",
    )

    readiness = compute_readiness(
        dev2_config(),
        computed_at=NOW,
        waiver_count=1,
        waivers=(waiver,),
        acknowledgements=(acknowledgement,),
    )

    assert readiness.gate_row(ReleaseGateName.WAIVER_COUNT).status is ReleaseSignalStatus.PASS


def test_dev2_profile_waiver_gate_can_be_the_first_red_gate() -> None:
    """An uncleared waiver block is a legitimate answer to 'what do I fix first'."""
    readiness = compute_readiness(
        dev2_config(),
        computed_at=NOW,
        probes=all_signals_passing(),
        waiver_count=1,
        waivers=(ReleaseWaiver(),),
    )

    assert readiness.first_red_gate is ReleaseGateName.WAIVER_COUNT
    assert readiness.ready is False


# --- error paths on the binding loader ---------------------------------------


def test_dev2_profile_binding_table_refuses_a_duplicated_gate() -> None:
    """One gate reads one evidence source; a second binding is refused."""
    doubled = DEV2_GATE_BINDINGS_YAML + "  - gate: migration\n    kind: waiver_block\n"

    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(doubled, profile=ReleaseGateProfile.DEV2)

    assert excinfo.value.code is GateBindingRejection.DUPLICATE_BINDING


def test_dev2_profile_binding_table_refuses_an_unbound_gate() -> None:
    """Dropping a binding is refused: an unbound gate passes by reading nothing."""
    rows = yaml.safe_load(DEV2_GATE_BINDINGS_YAML)
    rows["bindings"] = [row for row in rows["bindings"] if row["gate"] != "migration"]

    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(rows, profile=ReleaseGateProfile.DEV2)

    assert excinfo.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    assert "migration" in str(excinfo.value)


def test_dev2_profile_binding_table_refuses_a_waiver_gate_carrying_a_signal() -> None:
    """A waiver-block binding that also names a row declares two sources."""
    rows = yaml.safe_load(DEV2_GATE_BINDINGS_YAML)
    for row in rows["bindings"]:
        if row["gate"] == "waiver_count":
            row["signal"] = "migration"

    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(rows, profile=ReleaseGateProfile.DEV2)

    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


def test_dev2_profile_binding_table_is_not_accepted_under_dev1() -> None:
    """The four additions are outside dev1, so the dev2 table is refused there."""
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(DEV2_GATE_BINDINGS_YAML, profile=ReleaseGateProfile.DEV1)

    assert excinfo.value.code is GateBindingRejection.UNBOUND_GATE_OUTSIDE_PROFILE


def test_dev2_profile_rendering_refuses_a_rung_whose_profile_is_unauthored() -> None:
    """A rung whose profile has no gate set cannot be configured yet."""
    unauthored = V07_TRAIN.checkpoint_for_version("0.7.0.dev3")

    with pytest.raises(GateBindingError) as excinfo:
        render_checkpoint_config(rung=unauthored, template=V07_CONFIG_TEMPLATE)

    assert excinfo.value.code is GateBindingRejection.UNDECLARED_PROFILE


def test_dev2_profile_gates_lookup_refuses_an_unauthored_profile() -> None:
    """profile_gates is the fail-fast boundary for an unauthored profile."""
    with pytest.raises(GateBindingError, match="no gate set is declared"):
        profile_gates(ReleaseGateProfile.STABLE)
