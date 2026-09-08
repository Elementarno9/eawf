"""Tests for the derived required-signal set and the moves it drives.

The contract under test is that the required subset is a *function* of
the authored surface, never a second list beside it. So the module pins:

1. The subset is exactly the rows the required gates bind, plus tree
   cleanliness under ``require_clean_tree``, ancestry under
   ``require_ancestor_of_remote``, and credentials when a required
   target declares a ``credential_handle`` -- each flag independently
   drops its row when turned off. No dev1 target declares one: all three
   authenticate by OIDC or the ambient workflow token, so the dev1
   derivation is six rows, not seven.
2. Two gates binding one row contribute it once, and a gate settled by a
   proof command contributes nothing.
3. A CANDIDATE moves to PREFLIGHT_FAILED on any derived row that is not
   passing, whichever row it is.
4. A CANDIDATE cannot reach APPROVED on such a sweep: the denial carries
   ``release_not_ready`` and names the first red *gate*, so the operator
   reads the check they must repair rather than the row beneath it.
"""

from __future__ import annotations

import pytest

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.release.waiver import ReleaseWaiver
from eawf.kernel.spec.release import ReleaseGateProfile, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName
from eawf.workflow.release.lifecycle import ReleaseDenialCode, ReleaseTransitionError
from eawf.workflow.release.preflight import approve_release, record_preflight_result
from eawf.workflow.release.train import gate_bindings_for
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    compute_readiness,
    derive_required_signals,
)
from tests.unit.kernel.release.conftest import (
    NOW,
    all_passing,
    dev1_config,
    fixed_probe,
    release_record,
)

_DEV1_REQUIRED = (
    ReleaseSignalName.VERSION_CONSISTENCY,
    ReleaseSignalName.CHANGELOG,
    ReleaseSignalName.ANCESTRY,
    ReleaseSignalName.TREE_CLEANLINESS,
    ReleaseSignalName.ARTIFACTS,
    ReleaseSignalName.DEPENDENCIES,
)


def _sweep(*, red: ReleaseSignalName | None = None, **kwargs: object) -> ReleaseReadiness:
    """Return a dev1 sweep that is green apart from *red*."""
    probes = all_passing()
    if red is not None:
        probes[red] = fixed_probe(ReleaseSignalStatus.FAIL)
    return compute_readiness(dev1_config(), probes=probes, computed_at=NOW, **kwargs)  # type: ignore[arg-type]


# --- the derivation --------------------------------------------------


def test_dev1_required_set_is_exactly_the_derived_six(config: ReleaseConfig) -> None:
    assert derive_required_signals(config) == _DEV1_REQUIRED


def test_required_set_is_in_signal_declaration_order(config: ReleaseConfig) -> None:
    required = derive_required_signals(config)
    assert list(required) == [name for name in ReleaseSignalName if name in set(required)]


def test_two_gates_on_one_row_contribute_it_once(config: ReleaseConfig) -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    sharing = [
        gate
        for gate in config.gates.required
        if bindings[gate].required_signal is ReleaseSignalName.DEPENDENCIES
    ]
    assert len(sharing) == 2
    required = derive_required_signals(config)
    assert required.count(ReleaseSignalName.DEPENDENCIES) == 1


def test_proof_command_gates_contribute_no_row(config: ReleaseConfig) -> None:
    required = set(derive_required_signals(config))
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    for gate in (
        ReleaseGateName.EPOCH1_STABILIZATION,
        ReleaseGateName.TELEMETRY_PRODUCER,
        ReleaseGateName.FRONT_DOOR_JOURNEY,
    ):
        assert bindings[gate].required_signal is None
    assert ReleaseSignalName.PLATFORM not in required
    assert ReleaseSignalName.PROVIDER not in required


@pytest.mark.parametrize(
    ("flag", "dropped"),
    [
        ("require_clean_tree", ReleaseSignalName.TREE_CLEANLINESS),
        ("require_ancestor_of_remote", ReleaseSignalName.ANCESTRY),
    ],
)
def test_a_flag_turned_off_drops_exactly_its_row(flag: str, dropped: ReleaseSignalName) -> None:
    required = set(derive_required_signals(dev1_config(**{flag: False})))
    assert dropped not in required
    assert set(_DEV1_REQUIRED) - {dropped} == required


def test_credentials_drop_when_no_target_is_required(config: ReleaseConfig) -> None:
    optional = [dict(target.model_dump(mode="json"), required=False) for target in config.targets]
    required = set(derive_required_signals(dev1_config(targets=optional)))
    assert ReleaseSignalName.CREDENTIALS not in required


def test_credentials_drop_when_no_required_target_declares_a_handle(
    config: ReleaseConfig,
) -> None:
    """A required target that holds no handle does not derive the row.

    PyPI trusted publishing and the GitHub release authenticate without
    holding anything the sweep could look for, so deriving "a required
    handle is available" from their mere presence asked a question with
    no answer and left the checkpoint permanently unready.
    """
    handleless = [
        dict(target.model_dump(mode="json"), credential_handle=None) for target in config.targets
    ]
    required = set(derive_required_signals(dev1_config(targets=handleless)))
    assert ReleaseSignalName.CREDENTIALS not in required


def test_credentials_derive_from_one_handle_bearing_required_target(
    config: ReleaseConfig,
) -> None:
    """One required target declaring a handle is enough to derive the row."""
    targets = [
        dict(target.model_dump(mode="json"), credential_handle=None) for target in config.targets
    ]
    targets[0] = dict(targets[0], required=True, credential_handle="SOME_TOKEN")
    required = set(derive_required_signals(dev1_config(targets=targets)))
    assert ReleaseSignalName.CREDENTIALS in required


def test_a_single_required_gate_derives_a_single_row() -> None:
    required = derive_required_signals(
        dev1_config(
            require_clean_tree=False,
            require_ancestor_of_remote=False,
            gates={"profile": "dev1", "required": ["changelog_entry"]},
        )
    )
    assert required == (ReleaseSignalName.CHANGELOG,)


def test_a_proof_only_gate_list_derives_no_gate_bound_row() -> None:
    required = derive_required_signals(
        dev1_config(
            require_clean_tree=False,
            require_ancestor_of_remote=False,
            gates={"profile": "dev1", "required": ["front_door_journey"]},
        )
    )
    assert required == ()


# --- CANDIDATE -> PREFLIGHT_FAILED -----------------------------------


@pytest.mark.parametrize("red", _DEV1_REQUIRED)
def test_any_derived_row_going_red_fails_preflight(red: ReleaseSignalName) -> None:
    outcome = record_preflight_result(release_record(), _sweep(red=red))
    assert outcome.status is ReleaseStatus.PREFLIGHT_FAILED


def test_a_row_outside_the_derived_set_does_not_fail_preflight() -> None:
    sweep = _sweep(red=ReleaseSignalName.MIGRATION)
    assert sweep.ready is True
    assert record_preflight_result(release_record(), sweep).status is ReleaseStatus.CANDIDATE


def test_a_green_sweep_leaves_the_candidate_where_it_is() -> None:
    candidate = release_record()
    assert record_preflight_result(candidate, _sweep()) is candidate


def test_preflight_refuses_a_sweep_computed_for_another_release() -> None:
    other = release_record(key="REL-0.7.0.dev2", version="0.7.0.dev2")
    with pytest.raises(ValueError, match="cannot be applied to release"):
        record_preflight_result(other, _sweep())


# --- CANDIDATE -> APPROVED -------------------------------------------


def test_approval_of_a_red_sweep_names_the_first_red_gate() -> None:
    sweep = _sweep(red=ReleaseSignalName.CHANGELOG)
    assert sweep.first_red_gate is ReleaseGateName.CHANGELOG_ENTRY
    with pytest.raises(ReleaseTransitionError) as excinfo:
        approve_release(
            release_record(),
            sweep,
            approval_ref="receipt://approval/0029",
            approved_at=NOW,
        )
    assert excinfo.value.code is ReleaseDenialCode.RELEASE_NOT_READY
    assert "release_not_ready" in str(excinfo.value)
    assert "changelog_entry" in str(excinfo.value)


def test_the_named_gate_is_the_earliest_red_in_profile_order() -> None:
    probes = all_passing()
    probes[ReleaseSignalName.ARTIFACTS] = fixed_probe(ReleaseSignalStatus.FAIL)
    probes[ReleaseSignalName.CHANGELOG] = fixed_probe(ReleaseSignalStatus.FAIL)
    sweep = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    assert sweep.first_red_gate is ReleaseGateName.CHANGELOG_ENTRY


def test_a_component_gate_going_red_names_the_component_gate() -> None:
    sweep = _sweep(red=ReleaseSignalName.DEPENDENCIES)
    assert sweep.first_red_gate is ReleaseGateName.DEPENDENCY_INVENTORY
    with pytest.raises(ReleaseTransitionError) as excinfo:
        approve_release(
            release_record(),
            sweep,
            approval_ref="receipt://approval/0029",
            approved_at=NOW,
        )
    assert "dependencies.inventory" in str(excinfo.value)


def test_a_waiver_only_denial_names_the_waivers_not_a_gate() -> None:
    sweep = _sweep(waivers=(ReleaseWaiver(scope="P31-I01-W36", reason="no runtime"),))
    assert sweep.first_red_gate is None
    with pytest.raises(ReleaseTransitionError) as excinfo:
        approve_release(
            release_record(),
            sweep,
            approval_ref="receipt://approval/0029",
            approved_at=NOW,
        )
    assert "every gate is green" in str(excinfo.value)
    assert "unexplained" in str(excinfo.value)


def test_a_green_sweep_approves_and_binds_the_receipt() -> None:
    approved = approve_release(
        release_record(),
        _sweep(),
        approval_ref="receipt://approval/0029",
        approved_at=NOW,
    )
    assert approved.status is ReleaseStatus.APPROVED
    assert approved.approval_ref == "receipt://approval/0029"


def test_approval_from_a_non_candidate_keeps_the_illegal_transition_code() -> None:
    with pytest.raises(ReleaseTransitionError) as excinfo:
        approve_release(
            release_record(status=ReleaseStatus.PREFLIGHT_FAILED),
            _sweep(),
            approval_ref="receipt://approval/0029",
            approved_at=NOW,
        )
    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


def test_approval_refuses_a_naive_timestamp() -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="must be timezone-aware"):
        approve_release(
            release_record(),
            _sweep(),
            approval_ref="receipt://approval/0029",
            approved_at=datetime(2026, 9, 4, 12, 0),
        )


def test_gate_row_lookup_refuses_an_absent_gate() -> None:
    sweep = _sweep()
    sweep_without = sweep.model_copy(update={"gates": sweep.gates[1:]})
    with pytest.raises(KeyError, match="version_consistency"):
        sweep_without.gate_row(ReleaseGateName.VERSION_CONSISTENCY)
