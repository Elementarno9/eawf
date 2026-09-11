"""REL-037: a checkpoint is not built until its measurements are citable.

Three surfaces were probed before any of them was designed -- the
epoch-2 importer, the cross-provider dispatch surface and the daemon
under concurrent load -- and each probe is promoted as a
:class:`~eawf.kernel.spec.measured_contract.MeasuredContract`. Each leg
is then assigned to the rung that builds the surface it measures:
``dev2`` builds the importer, so it asserts over the importer contract;
``dev3`` is the first rung that dispatches natively in parallel and the
first with a projection seam over the daemon's RPC, so the other two
land there.

These tests pin that neither rung can be opened while one of its
contracts is unpromoted, and that the refusal names *each* missing
contract plus the command that promotes it -- a refusal naming "some
contract" would send the operator back to the same wall after every
partial fix, and a refusal naming only the first would do it once per
contract.

The negative fixture is per-contract on purpose: promoting one of the
dev3 pair and asserting the other is named is what proves the check
reads both rather than short-circuiting.
"""

from __future__ import annotations

import asyncio
import json
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
    IMPORTER_CORPUS_SCALE_BAND,
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
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import record_release
from eawf.workflow.release.train import V07_TRAIN
from tests._release_helpers import NOW, dev1_adoption, dev1_config, dev1_draft

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: Project code of the empty-repo fixture; artifact URNs are built from it.
SCOPE = "QR"

#: The checkpoints under admission.
DEV2 = "0.7.0.dev2"
DEV3 = "0.7.0.dev3"

#: The contracts each rung asserts over, in admission order.
CONTRACT_IDS = required_contract_ids(DEV2)
DEV3_CONTRACT_IDS = required_contract_ids(DEV3)

#: Identity minted for the record a successful create returns.
DEV2_UID = UUID(int=372)

#: The command the refusal must hand the operator.
PROMOTE_CMD = "eawf artifact promote-contract"

#: The corpus pin the importer contract was extracted from, which is the
#: same document the cutover rehearsal is judged against.
CORPUS_PIN = _REPO_ROOT / "tests" / "fixtures" / "migration" / "live-cutover" / "corpus-pin.json"


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


def create_dev3(state: State) -> Release:
    """Open the dev3 checkpoint against *state*."""
    return create_checkpoint_release(
        state,
        train=V07_TRAIN,
        version=DEV3,
        uid=DEV2_UID,
        membership_refs=("milestone://epoch2/native-canary",),
    )


# --- the admission table -----------------------------------------------------


def test_dev2_requires_the_importer_contract_alone() -> None:
    """dev2 builds the importer, so the importer is what it asserts over."""
    assert CONTRACT_IDS == ("MCT-26091101",)


def test_dev3_requires_the_cross_provider_and_daemon_contracts() -> None:
    """The two legs whose surfaces dev3 is the first rung to exercise."""
    assert DEV3_CONTRACT_IDS == ("MCT-26081302", "MCT-26081303")


def test_the_admission_table_draws_only_on_promotable_contracts() -> None:
    """Every id either rung requires is a contract that can be promoted."""
    assert set(CONTRACT_IDS) | set(DEV3_CONTRACT_IDS) <= set(PREFLIGHT_CONTRACTS)


def test_dev1_predates_the_rule_and_requires_nothing() -> None:
    """The rung that has no probes to cite is not blocked by the gate."""
    assert required_contract_ids("0.7.0.dev1") == ()
    assert assert_measured_contracts(state_with(()), "0.7.0.dev1") == ()


# --- the admitted case -------------------------------------------------------


def test_the_importer_contract_promoted_admits_the_checkpoint() -> None:
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


def test_the_importer_contract_resolves_by_artifact_urn() -> None:
    """The full URN, not only the bare id, lands on the promoted row."""
    state = state_with(CONTRACT_IDS)
    urn = f"urn:eawf:v1:artifact:{SCOPE}/{CONTRACT_IDS[0]}"

    row = resolve_contract_citation(state, urn)

    assert row.id == CONTRACT_IDS[0]
    assert row.metadata["contract_id"] == CONTRACT_IDS[0]


def test_the_importer_contract_is_measured_at_the_thousands_scale_band() -> None:
    """The importer asserts over the production corpus, in the thousands band."""
    rows = assert_measured_contracts(state_with(CONTRACT_IDS), DEV2)

    observed = rows[0].metadata["observed"]
    assert observed["scale_band"] == IMPORTER_CORPUS_SCALE_BAND == "thousands"
    assert rows[0].metadata["environment"]["scale_band"] == ScaleBand.PRODUCTION.value


def test_the_importer_contract_band_matches_the_committed_corpus_pin() -> None:
    """The band is the one the rehearsal itself is judged against."""
    pin = json.loads(CORPUS_PIN.read_text(encoding="utf-8"))
    rows = assert_measured_contracts(state_with(CONTRACT_IDS), DEV2)

    observed = rows[0].metadata["observed"]
    assert observed["scale_band"] == pin["declared_band"]
    assert observed["corpus_rows"] == pin["observed_rows"]
    assert observed["observed_at_revision"] == pin["observed_at_revision"]


def test_the_importer_contract_boundary_cites_the_production_corpus() -> None:
    """A non-empty boundary naming what was measured and where it stops."""
    rows = assert_measured_contracts(state_with(CONTRACT_IDS), DEV2)

    boundary = rows[0].metadata["boundary"]
    assert boundary.strip()
    assert "production corpus" in boundary
    assert "thousands band" in boundary


# --- the negative fixture: each single absence --------------------------------


@pytest.mark.parametrize("absent", CONTRACT_IDS)
def test_a_single_absent_contract_refuses_and_names_it(absent: str) -> None:
    """An unpromoted contract refuses the checkpoint, naming the missing one."""
    promoted = [contract_id for contract_id in CONTRACT_IDS if contract_id != absent]

    with pytest.raises(UserError) as excinfo:
        create_dev2(state_with(promoted))

    message = str(excinfo.value)
    assert excinfo.value.kind == "measured_contract_missing"
    assert absent in message
    assert CONTRACT_LABELS[absent] in message
    assert f"{PROMOTE_CMD} {absent}" in message


@pytest.mark.parametrize("absent", DEV3_CONTRACT_IDS)
def test_a_refusal_names_only_the_absent_contract(absent: str) -> None:
    """The contract that is promoted stays out of the operator's way."""
    promoted = [contract_id for contract_id in DEV3_CONTRACT_IDS if contract_id != absent]

    with pytest.raises(UserError) as excinfo:
        create_dev3(state_with(promoted))

    assert not any(contract_id in str(excinfo.value) for contract_id in promoted)


def test_no_contracts_promoted_refuses_naming_the_required_one() -> None:
    """The empty state fails closed and names what dev2 needs."""
    with pytest.raises(UserError) as excinfo:
        create_dev2(state_with(()))

    assert excinfo.value.kind == "measured_contract_missing"
    assert CONTRACT_IDS[0] in str(excinfo.value)


# --- dev3 admission: the refusal names every missing contract -----------------


def test_dev3_refusal_names_each_missing_contract() -> None:
    """Both dev3 legs are named in one refusal, not one per round-trip."""
    with pytest.raises(UserError) as excinfo:
        create_dev3(state_with(()))

    message = str(excinfo.value)
    assert excinfo.value.kind == "measured_contract_missing"
    for contract_id in DEV3_CONTRACT_IDS:
        assert contract_id in message
        assert CONTRACT_LABELS[contract_id] in message
        assert f"{PROMOTE_CMD} {contract_id}" in message


def test_dev3_refusal_names_the_surfaces_in_the_operators_vocabulary() -> None:
    """The labels are cross-provider conformance and daemon RPC under dispatch."""
    with pytest.raises(UserError) as excinfo:
        create_dev3(state_with(()))

    message = str(excinfo.value)
    assert "cross-provider conformance" in message
    assert "daemon RPC under concurrent dispatch" in message


def test_dev3_refusal_counts_the_contracts_it_is_missing() -> None:
    """The count tells the operator how many fixes are ahead of them."""
    with pytest.raises(UserError) as excinfo:
        create_dev3(state_with(()))

    assert f"{len(DEV3_CONTRACT_IDS)} of {len(DEV3_CONTRACT_IDS)}" in str(excinfo.value)


def test_dev3_is_not_blocked_on_the_importer_contract() -> None:
    """The importer leg is dev2's assertion; dev3 does not re-demand it."""
    assert CONTRACT_IDS[0] not in DEV3_CONTRACT_IDS

    with pytest.raises(UserError) as excinfo:
        create_dev3(state_with(()))

    assert CONTRACT_IDS[0] not in str(excinfo.value)


def test_dev3_opens_once_both_contracts_are_promoted() -> None:
    """With the pair citable the rung opens as a DRAFT record."""
    record = create_dev3(state_with(DEV3_CONTRACT_IDS))

    assert record.key == "REL-0.7.0.dev3"
    assert record.status is ReleaseStatus.DRAFT


def test_admission_cites_ids_because_the_spike_path_stays_refused() -> None:
    """Promotion does not make the gitignored spike tree citable."""
    spike_path = PREFLIGHT_CONTRACTS[DEV3_CONTRACT_IDS[0]].observed_at_ref
    promoted = state_with(DEV3_CONTRACT_IDS)
    assert spike_path.startswith(LOCAL_SPIKE_ROOT)

    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(promoted, spike_path)

    assert excinfo.value.kind == "plan_reference_missing"
    assert all(contract_id in promoted.artifacts for contract_id in DEV3_CONTRACT_IDS)


def test_the_importer_contract_cites_a_committed_observation() -> None:
    """The re-measured importer leg points at a tree a reviewer can open."""
    contract = PREFLIGHT_CONTRACTS[CONTRACT_IDS[0]]

    assert not contract.observed_at_ref.startswith(LOCAL_SPIKE_ROOT)
    assert (_REPO_ROOT / contract.observed_at_ref).is_file()


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
    """Write *state* under *tmp_path* and return a context bound to it.

    The burned dev1 record is written alongside it because ``dev2``
    succeeds ``dev1``, and a rung whose predecessor is unrecorded is
    refused before its measurements are ever consulted. Staging it keeps
    these cases about admission rather than about succession.
    """
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, state)
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, _operation = burn_release(adopted, dev1_config(), None)
    record_release(state_path, burned, recorded_at=NOW, summary=f"burn {burned.key}")
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
