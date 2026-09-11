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

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.measured_contract import (
    MeasuredContract,
    ScaleBand,
    scale_band_satisfies,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.evidence.measured_contract import (
    LOCAL_SPIKE_ROOT,
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
    resolve_contract_citation,
)

#: The promotion command the ``plan_reference_missing`` error must name,
#: spelled out literally rather than re-derived from the code under test.
PROMOTE_CMD = "eawf artifact promote-contract"


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


def _promote_all(state_path: Path) -> None:
    """Promote all three contracts through the CLI against *state_path*."""
    for contract_id in CONTRACT_IDS:
        result = runner.invoke(app, ["--json", "artifact", "promote-contract", contract_id])
        assert result.exit_code == 0, result.stdout


def _evidence_rows(state_path: Path) -> list[dict[str, Any]]:
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


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
    assert contract.observed["scale_band"] == "thousands"


# ---- REL-037: eawf artifact show over the three URNs -----------------------


def test_artifact_show_resolves_each_promoted_contract_urn(state_path: Path) -> None:
    """The REL-037 acceptance: each of the three contracts is promoted, is
    resolvable by ArtifactUrn through ``eawf artifact show``, carries a
    non-empty boundary, and carries a MeasurementEnvironment whose
    scale_band is at least the band its implementing checkpoint asserts
    over — the importer one at production scale."""
    _promote_all(state_path)

    for contract_id in CONTRACT_IDS:
        urn = f"urn:eawf:v1:artifact:{SCOPE}/{contract_id}"
        shown = runner.invoke(app, ["--json", "artifact", "show", urn])
        assert shown.exit_code == 0, shown.stdout
        row = json.loads(shown.stdout)
        assert row["id"] == contract_id
        assert row["urn"] == urn
        metadata = row["metadata"]
        assert metadata["boundary"].strip()
        observed_band = ScaleBand(metadata["environment"]["scale_band"])
        assert scale_band_satisfies(observed_band, required=PREFLIGHT_CHECKPOINT_BANDS[contract_id])

    importer = runner.invoke(
        app, ["--json", "artifact", "show", f"urn:eawf:v1:artifact:{SCOPE}/{IMPORTER_CONTRACT_ID}"]
    )
    importer_env = json.loads(importer.stdout)["metadata"]["environment"]
    assert importer_env["scale_band"] == ScaleBand.PRODUCTION.value
    assert importer_env["population_size"] == 10684


def test_promoted_contracts_land_in_state_json(state_path: Path) -> None:
    _promote_all(state_path)
    body = json.loads(state_path.read_text(encoding="utf-8"))
    for contract_id in CONTRACT_IDS:
        row = body["artifacts"][contract_id]
        assert row["urn"] == f"urn:eawf:v1:artifact:{SCOPE}/{contract_id}"
        assert row["kind"] == "plan_spec"
        assert row["metadata"]["boundary"].strip()
        assert row["metadata"]["limits"]
    assert load_state(state_path).artifacts.keys() >= set(CONTRACT_IDS)


def test_artifact_show_resolves_a_promoted_contract_by_bare_id(state_path: Path) -> None:
    _promote_all(state_path)
    shown = runner.invoke(app, ["--json", "artifact", "show", DAEMON_CONTRACT_ID])
    assert shown.exit_code == 0, shown.stdout
    assert json.loads(shown.stdout)["id"] == DAEMON_CONTRACT_ID


# ---- PLAN-025: one EVD row per contract ------------------------------------


def test_promotion_mints_one_evidence_row_per_contract(state_path: Path) -> None:
    assert _evidence_rows(state_path) == []
    _promote_all(state_path)

    rows = _evidence_rows(state_path)
    assert len(rows) == len(CONTRACT_IDS)
    by_contract = {row["payload"]["metrics"]["contract_id"]: row for row in rows}
    assert sorted(by_contract) == sorted(CONTRACT_IDS)
    for contract_id, row in by_contract.items():
        assert row["kind"] == StoreKind.EVIDENCE.value
        payload = row["payload"]
        assert payload["evidence_kind"] == "deterministic"
        assert payload["status"] == "pass"
        assert payload["produced_by"] == "tool"
        assert contract_id in payload["refs"]
        assert f"urn:eawf:v1:artifact:{SCOPE}/{contract_id}" in payload["refs"]
        assert row["artifact_ids"] == [contract_id]


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
    assert f"{PROMOTE_CMD} {IMPORTER_CONTRACT_ID}" in message


def test_plan_reference_missing_names_the_promotion_command_for_each_spike(
    state_path: Path,
) -> None:
    state = load_state(state_path)
    for contract_id in SPIKE_CONTRACT_IDS:
        contract = PREFLIGHT_CONTRACTS[contract_id]
        message = _expect_plan_reference_missing(state, contract.observed_at_ref)
        assert f"{PROMOTE_CMD} {contract_id}" in message


def test_plan_reference_missing_fires_even_after_promotion(state_path: Path) -> None:
    """The local path never becomes citable: promotion mints a URN, and the
    raw spike path stays refused so a spec cannot cite the scratch tree."""
    _promote_all(state_path)
    contract = PREFLIGHT_CONTRACTS[IMPORTER_CONTRACT_ID]
    _expect_plan_reference_missing(load_state(state_path), contract.observed_at_ref)


def test_unknown_spike_citation_names_a_placeholder_promotion_command(
    state_path: Path,
) -> None:
    message = _expect_plan_reference_missing(
        load_state(state_path), ".ea/local/spikes/2027-01-01-unknown/observations.json"
    )
    assert f"{PROMOTE_CMD} <id>" in message


def test_artifact_show_of_a_spike_path_exits_with_plan_reference_missing(
    state_path: Path,
) -> None:
    citation = ".ea/local/spikes/2026-08-13-v07-preflight/daemon-rpc-parallel/observations.jsonl"
    result = runner.invoke(app, ["--json", "artifact", "show", citation])
    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["data"]["kind"] == "plan_reference_missing"
    assert f"{PROMOTE_CMD} {DAEMON_CONTRACT_ID}" in body["message"]


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


def test_promote_contract_cli_rejects_unknown_contract_id(state_path: Path) -> None:
    result = runner.invoke(app, ["--json", "artifact", "promote-contract", "MCT-00000000"])
    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["data"]["kind"] == "NotFound"
    assert IMPORTER_CONTRACT_ID in body["message"]


def test_promote_contract_cli_is_idempotent_guarded(state_path: Path) -> None:
    first = runner.invoke(app, ["--json", "artifact", "promote-contract", DAEMON_CONTRACT_ID])
    assert first.exit_code == 0, first.stdout
    second = runner.invoke(app, ["--json", "artifact", "promote-contract", DAEMON_CONTRACT_ID])
    assert second.exit_code != 0
    assert len(_evidence_rows(state_path)) == 1


def test_promote_contract_cli_honours_explicit_scope_id(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--json",
            "artifact",
            "promote-contract",
            CONFORMANCE_CONTRACT_ID,
            "--scope-id",
            "EAWF",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["urn"] == f"urn:eawf:v1:artifact:EAWF/{CONFORMANCE_CONTRACT_ID}"


def test_contract_metadata_round_trips_through_state_json(state_path: Path) -> None:
    _promote_all(state_path)
    body = json.loads(state_path.read_text(encoding="utf-8"))
    for contract_id in CONTRACT_IDS:
        stored = body["artifacts"][contract_id]["metadata"]
        source: MeasuredContract = PREFLIGHT_CONTRACTS[contract_id]
        assert stored["contract_id"] == source.contract_id
        assert stored["surface"] == source.surface
        assert stored["probe_command"] == source.probe_command
        assert stored["observed"] == dict(source.observed)
        assert stored["boundary"] == source.boundary
        assert stored["observed_at_ref"] == source.observed_at_ref
        assert len(stored["limits"]) == len(source.limits)
