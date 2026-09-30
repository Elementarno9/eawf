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
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.measured_contract import ScaleBand
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


runner = CliRunner()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a private ``state.json`` the CLI resolves through ``EA_STATE``."""
    target = tmp_path / ".ea" / "state.json"
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, target)
    monkeypatch.setenv("EA_STATE", str(target))
    yield target


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


# ---- refusals --------------------------------------------------------------


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
