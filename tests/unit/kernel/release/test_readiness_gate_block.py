"""Tests for the readiness gate block and the waiver field beside it.

The contract under test is that the gate block is a *projection* of the
twelve signal rows, not a thirteenth through twentieth fact. So the
module pins:

1. A sweep carries eight gate rows beside the unchanged twelve signal
   rows, in profile order, each naming the one evidence source it reads.
2. A gate bound to a row or a component inherits that row's verdict, so
   two gates on one row report it twice rather than disagreeing; a gate
   bound to a proof command reports ``unavailable`` naming the command.
3. A counted waiver carries scope, reason and protected principal. A
   count above zero with any waiver missing one of the three is red and
   not acknowledgeable; an all-explained count is reported for
   acknowledgement instead.
4. ``eawf release preflight --json`` emits both blocks, because the CLI
   serializes the readiness object whole -- so the model dump carrying
   both is exactly what the operator sees.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.release.gate_binding import GateEvidenceKind
from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.release.waiver import (
    ReleaseWaiver,
    WaiverDisposition,
    classify_waivers,
)
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness
from tests.unit.kernel.release.conftest import (
    NOW,
    all_passing,
    dev1_config,
    fixed_probe,
)

_EXPLAINED = ReleaseWaiver(
    scope="P31-I01-W36",
    reason="the executor ran inline with no captured runtime",
    protected_principal="every wave close records a runtime gate receipt",
)


def _sweep(**kwargs: object) -> ReleaseReadiness:
    """Return a green dev1 sweep with *kwargs* forwarded to the computation."""
    return compute_readiness(
        dev1_config(),
        probes=all_passing(),
        computed_at=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


# --- the two blocks --------------------------------------------------


def test_a_sweep_carries_eight_gate_rows_beside_twelve_signal_rows() -> None:
    sweep = _sweep()
    assert len(sweep.signals) == 12
    assert len(sweep.gates) == 8
    assert [row.signal for row in sweep.signals] == list(ReleaseSignalName)


def test_gate_rows_follow_the_profile_declaration_order() -> None:
    assert [row.gate for row in _sweep().gates] == [
        ReleaseGateName.VERSION_CONSISTENCY,
        ReleaseGateName.CHANGELOG_ENTRY,
        ReleaseGateName.DEPENDENCY_INVENTORY,
        ReleaseGateName.ARTIFACT_REPRODUCIBILITY,
        ReleaseGateName.SECURITY_REVIEW,
        ReleaseGateName.EPOCH1_STABILIZATION,
        ReleaseGateName.TELEMETRY_PRODUCER,
        ReleaseGateName.FRONT_DOOR_JOURNEY,
    ]


def test_every_gate_row_names_its_one_evidence_source() -> None:
    for row in _sweep().gates:
        assert row.evidence_ref
        assert row.required is True


def test_a_row_bound_gate_inherits_the_row_verdict() -> None:
    probes = all_passing()
    probes[ReleaseSignalName.CHANGELOG] = fixed_probe(ReleaseSignalStatus.FAIL)
    sweep = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    gate_row = sweep.gate_row(ReleaseGateName.CHANGELOG_ENTRY)
    assert gate_row.status is ReleaseSignalStatus.FAIL
    assert gate_row.remediation == sweep.row(ReleaseSignalName.CHANGELOG).remediation


def test_two_component_gates_report_their_shared_row_twice() -> None:
    probes = all_passing()
    probes[ReleaseSignalName.DEPENDENCIES] = fixed_probe(ReleaseSignalStatus.FAIL)
    sweep = compute_readiness(dev1_config(), probes=probes, computed_at=NOW)
    inventory = sweep.gate_row(ReleaseGateName.DEPENDENCY_INVENTORY)
    vulnerability = sweep.gate_row(ReleaseGateName.SECURITY_REVIEW)
    assert inventory.status is vulnerability.status is ReleaseSignalStatus.FAIL
    assert inventory.evidence_ref == "dependencies.inventory"
    assert vulnerability.evidence_ref == "dependencies.vulnerability"


@pytest.mark.parametrize(
    ("gate", "command_id"),
    [
        (ReleaseGateName.EPOCH1_STABILIZATION, "epoch1_stabilization_suite"),
        (ReleaseGateName.TELEMETRY_PRODUCER, "telemetry_turn_cost_record"),
        (ReleaseGateName.FRONT_DOOR_JOURNEY, "front_door_install_smoke"),
    ],
)
def test_a_proof_gate_reports_unavailable_naming_its_command(
    gate: ReleaseGateName, command_id: str
) -> None:
    row = _sweep().gate_row(gate)
    assert row.evidence_kind is GateEvidenceKind.PROOF_COMMAND
    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert command_id in row.remediation
    assert row.evidence_ref == f"proof:{command_id}"


def test_a_gate_outside_the_required_list_is_reported_but_not_required() -> None:
    sweep = compute_readiness(
        dev1_config(gates={"profile": "dev1", "required": ["changelog_entry"]}),
        probes=all_passing(),
        computed_at=NOW,
    )
    assert len(sweep.gates) == 8
    assert sweep.gate_row(ReleaseGateName.CHANGELOG_ENTRY).required is True
    assert sweep.gate_row(ReleaseGateName.SECURITY_REVIEW).required is False


# --- waivers ---------------------------------------------------------


def test_no_waiver_is_the_none_disposition() -> None:
    sweep = _sweep()
    assert sweep.waiver_count == 0
    assert sweep.waivers == ()
    assert sweep.waiver_disposition is WaiverDisposition.NONE
    assert sweep.ready is True


def test_an_explained_waiver_is_reported_for_acknowledgement() -> None:
    sweep = _sweep(waivers=(_EXPLAINED,))
    assert sweep.waiver_count == 1
    assert sweep.waiver_disposition is WaiverDisposition.AWAITING_ACKNOWLEDGEMENT
    assert sweep.waivers[0].scope == "P31-I01-W36"
    assert sweep.waivers[0].protected_principal
    assert sweep.ready is False


@pytest.mark.parametrize("blank", ["scope", "reason", "protected_principal"])
def test_a_waiver_missing_any_one_of_the_three_is_red(blank: str) -> None:
    waiver = _EXPLAINED.model_copy(update={blank: ""})
    sweep = _sweep(waivers=(waiver,))
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert waiver.missing_fields == (blank,)
    assert sweep.ready is False


def test_a_count_with_no_waiver_row_is_red() -> None:
    sweep = _sweep(waiver_count=1)
    assert sweep.waiver_count == 1
    assert sweep.waivers == ()
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED
    assert sweep.ready is False


def test_one_unexplained_waiver_reds_an_otherwise_explained_set() -> None:
    sweep = _sweep(waivers=(_EXPLAINED, _EXPLAINED.model_copy(update={"reason": ""})))
    assert sweep.waiver_count == 2
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED


def test_a_count_larger_than_the_rows_is_red_not_an_error() -> None:
    sweep = _sweep(waiver_count=3, waivers=(_EXPLAINED,))
    assert sweep.waiver_count == 3
    assert sweep.waiver_disposition is WaiverDisposition.UNEXPLAINED


def test_a_count_smaller_than_the_rows_is_refused() -> None:
    with pytest.raises(ValueError, match="smaller than"):
        _sweep(waiver_count=1, waivers=(_EXPLAINED, _EXPLAINED))


def test_a_negative_waiver_count_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        _sweep(waiver_count=-1)


def test_classify_waivers_refuses_more_rows_than_are_counted() -> None:
    with pytest.raises(ValueError, match="every row is a counted waiver"):
        classify_waivers((_EXPLAINED, _EXPLAINED), waiver_count=1)


def test_waiver_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ReleaseWaiver.model_validate({"scope": "P31", "granted_by": "nobody"})


# --- record invariants -----------------------------------------------


def test_readiness_refuses_a_disposition_that_disagrees_with_the_waivers() -> None:
    payload = _sweep(waivers=(_EXPLAINED,)).model_dump(mode="json")
    with pytest.raises(ValidationError, match="waiver_disposition"):
        ReleaseReadiness.model_validate(payload | {"waiver_disposition": "none"})


def test_readiness_refuses_a_ready_flag_that_ignores_a_waiver() -> None:
    payload = _sweep(waivers=(_EXPLAINED,)).model_dump(mode="json")
    with pytest.raises(ValidationError, match="ready=True disagrees"):
        ReleaseReadiness.model_validate(payload | {"ready": True})


def test_readiness_refuses_a_duplicated_gate_row() -> None:
    payload = _sweep().model_dump(mode="json")
    payload["gates"] = [*payload["gates"], payload["gates"][0]]
    with pytest.raises(ValidationError, match="reports a gate more than once"):
        ReleaseReadiness.model_validate(payload)


def test_a_red_gate_row_must_carry_remediation() -> None:
    payload = _sweep().model_dump(mode="json")
    payload["gates"][5]["remediation"] = ""
    with pytest.raises(ValidationError, match="must carry remediation"):
        ReleaseReadiness.model_validate(payload)


# --- what the CLI emits ----------------------------------------------


def test_preflight_json_emits_both_blocks() -> None:
    # `eawf release preflight --json` hands the readiness model dump
    # straight to the emitter, so the dump IS the emitted payload.
    payload = _sweep(waivers=(_EXPLAINED,)).model_dump(mode="json")
    assert len(payload["signals"]) == 12
    assert len(payload["gates"]) == 8
    assert payload["waiver_count"] == 1
    assert payload["waiver_disposition"] == "awaiting_acknowledgement"
    assert payload["waivers"][0]["protected_principal"]
    assert payload["gates"][0] == {
        "gate": "version_consistency",
        "evidence_kind": "signal",
        "evidence_ref": "version_consistency",
        "required": True,
        "status": "pass",
        "remediation": "",
    }


def test_the_emitted_payload_round_trips() -> None:
    sweep = _sweep(waivers=(_EXPLAINED,))
    assert ReleaseReadiness.model_validate(sweep.model_dump(mode="json")) == sweep
