"""One name per concept, and the migration that moves a promoted row onto it.

``scale_band`` used to mean two unrelated things. On
:class:`~eawf.kernel.spec.measured_contract.MeasurementEnvironment` it
classes the *environment* a probe ran in -- toy, dev, production, fleet.
Inside the importer contract's ``observed`` block it named the *order of
magnitude of the corpus* the probe ran over -- units through
tens-of-thousands. The importer contract carried the key under both
meanings at once, which is how a reader ends up comparing a corpus size
against an environment class and getting an answer.

The corpus meaning is now ``corpus_magnitude`` everywhere it is written:
in the typed contract, in the constant the release gate reads, in the
rehearsal's own enum, and in the committed corpus pin. ``scale_band``
keeps the environment meaning and nothing else.

Renaming a key in code does not move the row already registered in
``state.json``: a promoted artifact is a snapshot taken on promotion day,
and the duplicate-id guard means it cannot simply be promoted again. The
second half of this module drives the migration path that does move it --
``eawf artifact promote-contract <id> --refresh-metadata`` -- over a row
seeded into exactly the stale shape the live one is in.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.measured_contract import ScaleBand
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.evidence.measured_contract import (
    IMPORTER_CORPUS_MAGNITUDE,
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
    refresh_contract_metadata,
)
from tests.integration.kernel.migration._live_corpus import (
    PIN_FILENAME,
    CorpusMagnitude,
    LiveCorpusPin,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: The empty-repo fixture every CLI case is driven against, and the
#: project code its artifact URNs are built from.
FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"
SCOPE = "QR"

#: The committed pin the importer contract was extracted from.
CORPUS_PIN = _REPO_ROOT / "tests" / "fixtures" / "migration" / "live-cutover" / PIN_FILENAME

#: The contract that carried the name twice, and a second one used to
#: prove a refresh touches only the row it names.
CORPUS_CONTRACT_ID = "MCT-26091101"
DAEMON_CONTRACT_ID = "MCT-26081303"

#: The promotion command a refusal has to hand the operator.
PROMOTE_CMD = "eawf artifact promote-contract"

#: The ``population`` prose as it read before the split, when it called a
#: corpus size a scale band next to an environment field of the same name.
STALE_POPULATION_PHRASE = "6,212 rows in the thousands scale band"
CURRENT_POPULATION_PHRASE = "6,212 rows, a thousands corpus magnitude"

runner = CliRunner()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a private ``state.json`` the CLI resolves through ``EA_STATE``."""
    target = tmp_path / ".ea" / "state.json"
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)
    monkeypatch.setenv("EA_STATE", str(target))
    yield target


def _read_body(state_path: Path) -> dict[str, Any]:
    """Return the raw ``state.json`` payload."""
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_body(state_path: Path, body: dict[str, Any]) -> None:
    """Write *body* back over ``state.json``."""
    state_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _event_rows(state_path: Path) -> list[dict[str, Any]]:
    """Return every envelope in the event store, oldest first."""
    path = store_path(state_path, StoreKind.EVENT)
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _evidence_rows(state_path: Path) -> list[dict[str, Any]]:
    """Return every envelope in the evidence store, oldest first."""
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _promote(state_path: Path, contract_id: str) -> None:
    """Promote *contract_id* through the CLI against *state_path*."""
    result = runner.invoke(app, ["--json", "artifact", "promote-contract", contract_id])
    assert result.exit_code == 0, result.stdout


def _pre_split_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return *metadata* as the promoted row reads before the migration.

    The stale shape is reconstructed from the current one rather than
    pasted in, so the fixture cannot drift away from the contract it is
    supposed to be the earlier revision of.
    """
    stale = deepcopy(metadata)
    observed = dict(stale["observed"])
    observed["scale_band"] = observed.pop("corpus_magnitude")
    stale["observed"] = observed
    environment = dict(stale["environment"])
    environment["population"] = environment["population"].replace(
        CURRENT_POPULATION_PHRASE, STALE_POPULATION_PHRASE
    )
    stale["environment"] = environment
    return stale


@pytest.fixture
def stale_row(state_path: Path) -> Path:
    """Promote the corpus contract, then age its row back to the old shape."""
    _promote(state_path, CORPUS_CONTRACT_ID)
    body = _read_body(state_path)
    row = body["artifacts"][CORPUS_CONTRACT_ID]
    row["metadata"] = _pre_split_metadata(row["metadata"])
    _write_body(state_path, body)
    return state_path


# ---- one name per concept --------------------------------------------------


def test_no_contract_records_a_corpus_size_under_the_environment_name() -> None:
    """``scale_band`` survives only as the environment class it names."""
    for contract in PREFLIGHT_CONTRACTS.values():
        assert "scale_band" not in contract.observed
        assert isinstance(contract.environment.scale_band, ScaleBand)


def test_the_importer_contract_separates_its_corpus_size_from_its_environment() -> None:
    """Both facts are recorded, under names that cannot be mistaken."""
    contract = PREFLIGHT_CONTRACTS[CORPUS_CONTRACT_ID]

    assert contract.observed["corpus_magnitude"] == IMPORTER_CORPUS_MAGNITUDE == "thousands"
    assert contract.environment.scale_band is ScaleBand.PRODUCTION
    assert PREFLIGHT_CHECKPOINT_BANDS[CORPUS_CONTRACT_ID] is ScaleBand.PRODUCTION


def test_the_committed_corpus_pin_declares_a_magnitude_not_a_band() -> None:
    """The pin the rehearsal is judged against uses the corpus name too."""
    pin = json.loads(CORPUS_PIN.read_text(encoding="utf-8"))

    assert "declared_band" not in pin
    assert pin["declared_magnitude"] == IMPORTER_CORPUS_MAGNITUDE
    assert LiveCorpusPin.load(CORPUS_PIN).declared_magnitude is CorpusMagnitude.THOUSANDS


def test_a_pin_carrying_the_old_key_no_longer_validates() -> None:
    """The rename is enforced by the model, not left to a reader's eye.

    ``extra="forbid"`` is what makes the old key a hard error rather than
    a silently ignored leftover beside a missing required field.
    """
    pin = json.loads(CORPUS_PIN.read_text(encoding="utf-8"))
    pin["declared_band"] = pin.pop("declared_magnitude")

    with pytest.raises(ValueError, match="declared_band"):
        LiveCorpusPin.model_validate(pin)


def test_the_two_vocabularies_share_no_member_value() -> None:
    """A corpus magnitude can never be read as an environment class.

    The values are disjoint, so a consumer that mixed the two up gets a
    lookup failure rather than a plausible-looking wrong answer.
    """
    magnitudes = {member.value for member in CorpusMagnitude}
    bands = {member.value for member in ScaleBand}

    assert magnitudes & bands == set()


# ---- the migration: --refresh-metadata -------------------------------------


def test_refresh_metadata_moves_a_stale_row_onto_the_current_shape(stale_row: Path) -> None:
    """The registered row stops carrying the name under both meanings."""
    before = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]["metadata"]
    assert before["observed"]["scale_band"] == "thousands"

    result = runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    assert result.exit_code == 0, result.stdout
    after = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]["metadata"]
    assert after["observed"]["corpus_magnitude"] == "thousands"
    assert "scale_band" not in after["observed"]
    assert after["environment"]["scale_band"] == ScaleBand.PRODUCTION.value
    assert CURRENT_POPULATION_PHRASE in after["environment"]["population"]


def test_refresh_metadata_rewrites_the_row_from_the_in_code_contract(stale_row: Path) -> None:
    """Every metadata key lands as the typed contract renders it."""
    runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    contract = PREFLIGHT_CONTRACTS[CORPUS_CONTRACT_ID]
    stored = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]["metadata"]
    assert stored["observed"] == dict(contract.observed)
    assert stored["boundary"] == contract.boundary
    assert stored["observed_at_ref"] == contract.observed_at_ref
    assert stored["environment"] == contract.environment.model_dump(mode="json")


def test_refresh_metadata_names_the_keys_it_rewrote(stale_row: Path) -> None:
    """The operator is told what moved, so a no-op is visibly a no-op."""
    result = runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    payload = json.loads(result.stdout)
    assert payload["changed_keys"] == ["environment", "observed"]
    assert payload["urn"] == f"urn:eawf:v1:artifact:{SCOPE}/{CORPUS_CONTRACT_ID}"


def test_refresh_metadata_keeps_the_row_identity_fixed(stale_row: Path) -> None:
    """A refresh rewrites the measurement, never the row's address."""
    before = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]

    runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    after = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]
    for field in ("id", "kind", "uri", "urn", "created_at", "sha256", "size_bytes"):
        assert after[field] == before[field]


def test_refresh_metadata_appends_one_artifact_event_and_no_evidence_row(
    stale_row: Path,
) -> None:
    """The measurement is restated, not re-attested, so no EVD row is minted."""
    events_before = len(_event_rows(stale_row))
    evidence_before = len(_evidence_rows(stale_row))

    runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    events = _event_rows(stale_row)
    assert len(events) == events_before + 1
    assert len(_evidence_rows(stale_row)) == evidence_before
    appended = events[-1]
    assert appended["payload"]["event_type"] == "artifact.update"
    assert appended["payload"]["command"] == "artifact promote-contract --refresh-metadata"
    assert appended["artifact_ids"] == [CORPUS_CONTRACT_ID]
    assert appended["scope_id"] == SCOPE


def test_refresh_metadata_of_an_already_current_row_changes_nothing(stale_row: Path) -> None:
    """The boundary case: a second refresh reports an empty key set.

    A migration verb that could not tell "already migrated" from "just
    migrated" would leave the operator re-running it forever, so the
    no-op is a distinguishable outcome rather than a silent success.
    """
    first = runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )
    assert first.exit_code == 0, first.stdout
    migrated = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]

    second = runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    assert second.exit_code == 0, second.stdout
    assert json.loads(second.stdout)["changed_keys"] == []
    assert _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID] == migrated


def test_refresh_metadata_reports_a_single_changed_key(state_path: Path) -> None:
    """One drifted key is reported alone, not rounded up to the whole row."""
    _promote(state_path, CORPUS_CONTRACT_ID)
    body = _read_body(state_path)
    body["artifacts"][CORPUS_CONTRACT_ID]["metadata"]["boundary"] = "stale boundary text"
    _write_body(state_path, body)

    result = runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["changed_keys"] == ["boundary"]


def test_refresh_metadata_touches_only_the_contract_it_names(stale_row: Path) -> None:
    """A neighbouring promoted row is left byte for byte as it was."""
    _promote(stale_row, DAEMON_CONTRACT_ID)
    neighbour = _read_body(stale_row)["artifacts"][DAEMON_CONTRACT_ID]

    runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    assert _read_body(stale_row)["artifacts"][DAEMON_CONTRACT_ID] == neighbour


# ---- refusals --------------------------------------------------------------


def test_refresh_metadata_of_an_unpromoted_contract_names_the_promotion_command(
    state_path: Path,
) -> None:
    """There is nothing to migrate until there is a row, and the error says so."""
    result = runner.invoke(
        app, ["--json", "artifact", "promote-contract", CORPUS_CONTRACT_ID, "--refresh-metadata"]
    )

    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["data"]["kind"] == "NotFound"
    assert f"{PROMOTE_CMD} {CORPUS_CONTRACT_ID}" in body["message"]
    assert CORPUS_CONTRACT_ID not in _read_body(state_path)["artifacts"]


def test_refresh_metadata_refuses_an_explicit_scope_id(stale_row: Path) -> None:
    """The registered row's urn already pins the scope, so a second one is a defect."""
    result = runner.invoke(
        app,
        [
            "--json",
            "artifact",
            "promote-contract",
            CORPUS_CONTRACT_ID,
            "--refresh-metadata",
            "--scope-id",
            "EAWF",
        ],
    )

    assert result.exit_code != 0
    assert json.loads(result.stdout)["data"]["kind"] == "InvalidInput"
    stored = _read_body(stale_row)["artifacts"][CORPUS_CONTRACT_ID]["metadata"]
    assert stored["observed"]["scale_band"] == "thousands"


def test_refresh_metadata_of_an_unknown_contract_id_is_refused(state_path: Path) -> None:
    """An id no contract carries never reaches the state transaction."""
    result = runner.invoke(
        app, ["--json", "artifact", "promote-contract", "MCT-00000000", "--refresh-metadata"]
    )

    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["data"]["kind"] == "NotFound"
    assert CORPUS_CONTRACT_ID in body["message"]


def test_refresh_contract_metadata_refuses_a_contract_below_its_checkpoint_band(
    state_path: Path,
) -> None:
    """A rewrite cannot lower a registered row under its checkpoint's band."""
    _promote(state_path, DAEMON_CONTRACT_ID)
    state = load_state(state_path)
    before = state.artifacts[DAEMON_CONTRACT_ID].metadata

    with pytest.raises(UserError, match="asserts over") as excinfo:
        refresh_contract_metadata(
            state,
            contract=PREFLIGHT_CONTRACTS[DAEMON_CONTRACT_ID],
            required_band=ScaleBand.FLEET,
        )

    assert excinfo.value.kind == "scale_band_below_checkpoint"
    assert state.artifacts[DAEMON_CONTRACT_ID].metadata == before


def test_refresh_contract_metadata_raises_not_found_before_it_mutates(state_path: Path) -> None:
    """The library refuses an unregistered contract without touching state."""
    state = load_state(state_path)
    promote_measured_contract(
        state,
        contract=PREFLIGHT_CONTRACTS[DAEMON_CONTRACT_ID],
        scope_id=SCOPE,
        required_band=PREFLIGHT_CHECKPOINT_BANDS[DAEMON_CONTRACT_ID],
    )
    registered = dict(state.artifacts)

    with pytest.raises(UserError) as excinfo:
        refresh_contract_metadata(
            state,
            contract=PREFLIGHT_CONTRACTS[CORPUS_CONTRACT_ID],
            required_band=PREFLIGHT_CHECKPOINT_BANDS[CORPUS_CONTRACT_ID],
        )

    assert excinfo.value.kind == "NotFound"
    assert state.artifacts == registered
