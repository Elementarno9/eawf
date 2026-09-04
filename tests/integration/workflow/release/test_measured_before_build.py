"""REL-037: dev2 is not built until all three measurements are citable.

The ``0.7.0.dev2`` checkpoint carries the epoch-2 importer, the
cross-provider dispatch surface and the daemon under concurrent load.
Each was probed before it was designed, and each probe is promoted as a
:class:`~eawf.kernel.spec.measured_contract.MeasuredContract`. These
tests pin that the checkpoint cannot be opened while any one of the
three is unpromoted, and that the refusal names *that* contract plus the
command that promotes it -- a refusal naming "some contract" would send
the operator back to the same wall after every partial fix.

The negative fixture is per-contract on purpose: promoting two of three
and asserting the third is named is what proves the check reads all
three rather than short-circuiting on the first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest

from eawf.kernel.spec.measured_contract import ScaleBand
from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import create
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    LOCAL_SPIKE_ROOT,
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
    resolve_contract_citation,
)
from eawf.workflow.release.admission import (
    CONTRACT_LABELS,
    assert_measured_contracts,
    create_checkpoint_release,
    required_contract_ids,
)
from eawf.workflow.release.train import V07_TRAIN

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: Project code of the empty-repo fixture; artifact URNs are built from it.
SCOPE = "QR"

#: The checkpoint under admission.
DEV2 = "0.7.0.dev2"

#: The three contracts dev2 asserts over, in admission order.
CONTRACT_IDS = required_contract_ids(DEV2)

#: Identity minted for the record a successful create returns.
DEV2_UID = UUID(int=372)

#: The command the refusal must hand the operator.
PROMOTE_CMD = "eawf artifact promote-contract"


def state_with(contract_ids: Sequence[str]) -> State:
    """Return the fixture state with exactly *contract_ids* promoted."""
    state = load_state(_EMPTY_STATE)
    for contract_id in contract_ids:
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id=SCOPE,
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )
    return state


def create_dev2(state: State) -> Release:
    """Open the dev2 checkpoint against *state*."""
    return create_checkpoint_release(state, train=V07_TRAIN, version=DEV2, uid=DEV2_UID)


# --- the admission table -----------------------------------------------------


def test_dev2_requires_the_three_preflight_contracts() -> None:
    """The admission table names exactly the three promoted contracts."""
    assert CONTRACT_IDS == ("MCT-26081301", "MCT-26081302", "MCT-26081303")
    assert set(CONTRACT_IDS) == set(PREFLIGHT_CONTRACTS)


def test_dev1_predates_the_rule_and_requires_nothing() -> None:
    """The rung that has no probes to cite is not blocked by the gate."""
    assert required_contract_ids("0.7.0.dev1") == ()
    assert assert_measured_contracts(state_with(()), "0.7.0.dev1") == ()


# --- the admitted case -------------------------------------------------------


def test_all_three_promoted_admits_the_checkpoint() -> None:
    """With every contract citable, dev2 opens as a DRAFT record."""
    record = create_dev2(state_with(CONTRACT_IDS))

    assert record.key == "REL-0.7.0.dev2"
    assert record.status is ReleaseStatus.DRAFT
    assert record.revision == 0


def test_admission_resolves_every_contract_to_its_artifact_row() -> None:
    """Each citation lands on a promoted row carrying the measured band."""
    rows = assert_measured_contracts(state_with(CONTRACT_IDS), DEV2)

    assert [row.id for row in rows] == list(CONTRACT_IDS)
    assert all(row.metadata["boundary"] for row in rows)


def test_the_importer_contract_is_measured_at_production_band() -> None:
    """The importer asserts over the real population, so it needs that band."""
    rows = assert_measured_contracts(state_with(CONTRACT_IDS), DEV2)

    environment = rows[0].metadata["environment"]
    assert environment["scale_band"] == ScaleBand.PRODUCTION.value


# --- the negative fixture: each single absence --------------------------------


@pytest.mark.parametrize("absent", CONTRACT_IDS)
def test_a_single_absent_contract_refuses_and_names_it(absent: str) -> None:
    """Promoting the other two still refuses, naming the missing one."""
    promoted = [contract_id for contract_id in CONTRACT_IDS if contract_id != absent]

    with pytest.raises(UserError) as excinfo:
        create_dev2(state_with(promoted))

    message = str(excinfo.value)
    assert excinfo.value.kind == "measured_contract_missing"
    assert absent in message
    assert CONTRACT_LABELS[absent] in message
    assert f"{PROMOTE_CMD} {absent}" in message


@pytest.mark.parametrize("absent", CONTRACT_IDS)
def test_a_refusal_names_only_the_absent_contract(absent: str) -> None:
    """The two that are promoted stay out of the operator's way."""
    promoted = [contract_id for contract_id in CONTRACT_IDS if contract_id != absent]

    with pytest.raises(UserError) as excinfo:
        create_dev2(state_with(promoted))

    assert not any(contract_id in str(excinfo.value) for contract_id in promoted)


def test_no_contracts_promoted_refuses_on_the_first() -> None:
    """The empty state fails closed on the first required contract."""
    with pytest.raises(UserError) as excinfo:
        create_dev2(state_with(()))

    assert excinfo.value.kind == "measured_contract_missing"
    assert CONTRACT_IDS[0] in str(excinfo.value)


def test_admission_cites_ids_because_the_spike_path_stays_refused() -> None:
    """Promotion does not make the gitignored spike tree citable."""
    spike_path = PREFLIGHT_CONTRACTS[CONTRACT_IDS[0]].observed_at_ref
    promoted = state_with(CONTRACT_IDS)
    assert spike_path.startswith(LOCAL_SPIKE_ROOT)

    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(promoted, spike_path)

    assert excinfo.value.kind == "plan_reference_missing"
    assert all(contract_id in promoted.artifacts for contract_id in CONTRACT_IDS)


# --- error paths -------------------------------------------------------------


def test_a_version_the_train_does_not_declare_is_refused() -> None:
    """A checkpoint off the ladder cannot be created at all."""
    with pytest.raises(KeyError, match="declares no checkpoint"):
        create_checkpoint_release(
            state_with(CONTRACT_IDS), train=V07_TRAIN, version="9.9.9", uid=DEV2_UID
        )


def test_a_malformed_version_is_refused() -> None:
    """A string that is not a train version never reaches the admission table."""
    with pytest.raises(ValueError, match="not a train version"):
        create_checkpoint_release(
            state_with(CONTRACT_IDS), train=V07_TRAIN, version="0.7", uid=DEV2_UID
        )


# --- the RPC the operator actually calls -------------------------------------


def _context(tmp_path: Path, state: State) -> MethodContext:
    """Write *state* under *tmp_path* and return a context bound to it."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, state)
    return MethodContext(
        started_at="2026-09-04T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0.dev1",
        state_path=state_path,
    )


def test_release_create_rpc_opens_dev2_when_all_three_are_promoted(tmp_path: Path) -> None:
    """The verb an operator calls admits the checkpoint and echoes the ids."""
    ctx = _context(tmp_path, state_with(CONTRACT_IDS))

    result = asyncio.run(create(ctx, {"version": DEV2}))

    assert result["release"]["key"] == "REL-0.7.0.dev2"
    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    assert result["measured_contracts"] == list(CONTRACT_IDS)


@pytest.mark.parametrize("absent", CONTRACT_IDS)
def test_release_create_rpc_refuses_naming_the_absent_contract(tmp_path: Path, absent: str) -> None:
    """The refusal reaches the wire with its cause tag and the contract id."""
    promoted = [contract_id for contract_id in CONTRACT_IDS if contract_id != absent]
    ctx = _context(tmp_path, state_with(promoted))

    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(create(ctx, {"version": DEV2}))

    message = str(excinfo.value)
    assert "measured_contract_missing" in message
    assert absent in message


def test_release_create_rpc_refuses_without_an_on_disk_state() -> None:
    """A checkpoint admitted against unreadable state is admitted against nothing."""
    ctx = MethodContext(
        started_at="2026-09-04T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0.dev1",
    )

    with pytest.raises(DaemonValidationError, match="on-disk state root"):
        asyncio.run(create(ctx, {"version": DEV2}))
