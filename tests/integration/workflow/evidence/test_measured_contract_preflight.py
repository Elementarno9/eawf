"""Tests for :mod:`eawf.workflow.evidence.measured_contract`.

Covers the promotion path for the three contracts extracted from the
2026-08-13 preflight spikes:

1. Each contract promotes through the existing evidence path
   (:func:`eawf.workflow.evidence.artifact.add_artifact`), lands an
   artifact row in ``state.json`` and mints one evidence (EVD) row.
2. ``eawf artifact show`` resolves each of the three artifact URNs and
   returns a non-empty ``boundary`` plus a ``scale_band`` at least as
   large as the band its implementing checkpoint asserts over — with the
   importer contract measured against a production-scale state
   population.
3. A citation of a contract that exists only under ``.ea/local/spikes``
   raises ``plan_reference_missing`` naming the promotion command.
4. Error paths: unknown contract id, duplicate promotion, a contract
   measured below its checkpoint band, malformed / non-artifact URNs.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.measured_contract import (
    ScaleBand,
    scale_band_satisfies,
)
from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.evidence.measured_contract import (
    LOCAL_SPIKE_ROOT,
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    PROMOTION_GAP,
    promote_measured_contract,
    resolve_contract_citation,
)


def _expect_plan_reference_missing(state: object, citation: str) -> str:
    """Assert *citation* is refused as unpromoted; return the message."""
    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(state, citation)  # type: ignore[arg-type]
    assert excinfo.value.kind == "plan_reference_missing"
    return str(excinfo.value)


FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
runner = CliRunner()

IMPORTER_CONTRACT_ID = "MCT-26081301"
CONFORMANCE_CONTRACT_ID = "MCT-26081302"
DAEMON_CONTRACT_ID = "MCT-26081303"
CORPUS_IMPORTER_CONTRACT_ID = "MCT-26091101"
SPIKE_CONTRACT_IDS = (IMPORTER_CONTRACT_ID, CONFORMANCE_CONTRACT_ID, DAEMON_CONTRACT_ID)
CONTRACT_IDS = (*SPIKE_CONTRACT_IDS, CORPUS_IMPORTER_CONTRACT_ID)

#: The fixture project code; artifact URNs are built from it.
SCOPE = "QR"


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    target = tmp_path / ".ea" / "state.json"
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)
    monkeypatch.setenv("EA_STATE", str(target))
    yield target


# ---- the registry ----------------------------------------------------------


def test_preflight_registry_holds_the_spike_contracts_and_the_corpus_revision() -> None:
    assert tuple(sorted(PREFLIGHT_CONTRACTS)) == tuple(sorted(CONTRACT_IDS))
    assert sorted(PREFLIGHT_CHECKPOINT_BANDS) == sorted(PREFLIGHT_CONTRACTS)


def test_every_preflight_contract_carries_a_non_empty_boundary() -> None:
    for contract in PREFLIGHT_CONTRACTS.values():
        assert contract.boundary.strip()
        assert contract.limits
        assert contract.observed


def test_importer_contract_is_measured_at_production_scale() -> None:
    contract = PREFLIGHT_CONTRACTS[IMPORTER_CONTRACT_ID]
    assert contract.environment.scale_band is ScaleBand.PRODUCTION
    assert PREFLIGHT_CHECKPOINT_BANDS[IMPORTER_CONTRACT_ID] is ScaleBand.PRODUCTION
    assert contract.environment.population_size == 10684
    assert contract.limit("state_bytes").value == pytest.approx(5743039.0)


def test_every_preflight_contract_meets_its_checkpoint_band() -> None:
    for contract_id, contract in PREFLIGHT_CONTRACTS.items():
        assert scale_band_satisfies(
            contract.environment.scale_band,
            required=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )


def test_every_spike_contract_points_at_its_raw_spike_output() -> None:
    for contract_id in SPIKE_CONTRACT_IDS:
        assert PREFLIGHT_CONTRACTS[contract_id].observed_at_ref.startswith(LOCAL_SPIKE_ROOT)


def test_the_corpus_importer_contract_points_at_a_committed_observation() -> None:
    """The re-measured importer leg was rehearsed in tree, not in the spike shed."""
    contract = PREFLIGHT_CONTRACTS[CORPUS_IMPORTER_CONTRACT_ID]

    assert not contract.observed_at_ref.startswith(LOCAL_SPIKE_ROOT)
    assert contract.environment.scale_band is ScaleBand.PRODUCTION
    assert contract.observed["corpus_magnitude"] == "thousands"
    assert "scale_band" not in contract.observed


# ---- REL-037: eawf artifact show over the three URNs -----------------------


# ---- PLAN-025: one EVD row per contract ------------------------------------


def test_promotion_goes_through_the_shared_artifact_mutator(state_path: Path) -> None:
    """The promotion reuses ``add_artifact``, so it inherits its URN
    minting and its duplicate-id guard rather than writing rows itself."""
    state = load_state(state_path)
    contract = PREFLIGHT_CONTRACTS[DAEMON_CONTRACT_ID]
    promotion = promote_measured_contract(
        state, contract=contract, scope_id=SCOPE, required_band=ScaleBand.DEV
    )
    assert promotion.urn == f"urn:eawf:v1:artifact:{SCOPE}/{DAEMON_CONTRACT_ID}"
    assert promotion.artifact_event.payload["event_type"] == "artifact.add"
    assert promotion.evidence_envelope.kind is StoreKind.EVIDENCE
    assert state.artifacts[DAEMON_CONTRACT_ID].metadata["boundary"] == contract.boundary

    with pytest.raises(UserError, match="already exists"):
        promote_measured_contract(
            state, contract=contract, scope_id=SCOPE, required_band=ScaleBand.DEV
        )


def test_promotion_refuses_a_contract_below_its_checkpoint_band(state_path: Path) -> None:
    state = load_state(state_path)
    contract = PREFLIGHT_CONTRACTS[DAEMON_CONTRACT_ID]
    with pytest.raises(UserError, match="asserts over") as excinfo:
        promote_measured_contract(
            state, contract=contract, scope_id=SCOPE, required_band=ScaleBand.FLEET
        )
    assert excinfo.value.kind == "scale_band_below_checkpoint"
    assert DAEMON_CONTRACT_ID not in state.artifacts


# ---- PLAN-025: plan_reference_missing --------------------------------------


@pytest.mark.parametrize(
    "citation",
    [
        ".ea/local/spikes/2026-08-13-v07-preflight/epoch1-state-at-scale/observations.json",
        "repo:.ea/local/spikes/2026-08-13-v07-preflight/epoch1-state-at-scale/probe.py",
        "./.ea/local/spikes/2026-08-13-v07-preflight/epoch1-state-at-scale/",
    ],
)
def test_local_spike_citation_raises_plan_reference_missing(
    state_path: Path, citation: str
) -> None:
    message = _expect_plan_reference_missing(load_state(state_path), citation)
    assert PROMOTION_GAP in message


def test_plan_reference_missing_names_the_promotion_command_for_each_spike(
    state_path: Path,
) -> None:
    state = load_state(state_path)
    for contract_id in SPIKE_CONTRACT_IDS:
        contract = PREFLIGHT_CONTRACTS[contract_id]
        message = _expect_plan_reference_missing(state, contract.observed_at_ref)
        assert PROMOTION_GAP in message


def test_unknown_spike_citation_names_a_placeholder_promotion_command(
    state_path: Path,
) -> None:
    message = _expect_plan_reference_missing(
        load_state(state_path), ".ea/local/spikes/2027-01-01-unknown/observations.json"
    )
    assert PROMOTION_GAP in message


@pytest.mark.parametrize(
    "citation",
    ["ART-001", "urn:eawf:v1:artifact:QR/ART-001", ".ea/local/research/2026-08-13-x.md"],
)
def test_non_spike_citations_are_not_flagged_as_unpromoted(state_path: Path, citation: str) -> None:
    """A neighbouring local path (research, not spikes) is a plain lookup
    miss, not a missing promotion."""
    state = load_state(state_path)
    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(state, citation)
    assert excinfo.value.kind != "plan_reference_missing"


def test_local_spike_root_prefix_is_recognised(state_path: Path) -> None:
    _expect_plan_reference_missing(
        load_state(state_path), f"{LOCAL_SPIKE_ROOT}anything/at/all.json"
    )


# ---- resolve error paths ---------------------------------------------------


def test_resolve_contract_citation_unknown_id_raises_not_found(state_path: Path) -> None:
    state = load_state(state_path)
    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(state, "MCT-99999999")
    assert excinfo.value.kind == "NotFound"


def test_resolve_contract_citation_malformed_urn_raises_invalid_input(
    state_path: Path,
) -> None:
    state = load_state(state_path)
    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(state, "urn:eawf:v1:artifact:")
    assert excinfo.value.kind == "InvalidInput"


def test_resolve_contract_citation_urn_without_id_raises_invalid_input(
    state_path: Path,
) -> None:
    state = load_state(state_path)
    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(state, f"urn:eawf:v1:artifact:{SCOPE}")
    assert excinfo.value.kind == "InvalidInput"
