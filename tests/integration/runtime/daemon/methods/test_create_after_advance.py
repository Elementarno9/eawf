"""``release.create`` is the only way into the next checkpoint's record.

The train advance no longer opens a DRAFT, so ``release create`` is where
every checkpoint record starts, and it has to know whether the rung below
really is finished with. For a predecessor that shipped (``baked`` or
``released``) that now includes the recorded train advance past it: the
advance is where the shipped rung's gate receipts were re-validated, and
creating its successor without one would skip that check.

An abandoned predecessor owes no advance, because it can never have one.
``0.7.0.dev1`` was burned to ``partially_released``, and ``dev2`` still
opens over it. These tests pin both sides, plus the full walk: dev3 is
refused before dev2 advances and admitted after the advance RPC records
the move.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus
from eawf.kernel.spec.release_config import load_release_config
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import create, show
from eawf.runtime.daemon.methods.release_receipts import advance
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.advance import CheckpointGateReceipt, TrainAdvanceRecord
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import read_release_records, record_release
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.release.train_store import (
    record_checkpoint_receipt,
    record_train_advance,
    train_advances_path,
)
from eawf.workflow.verify.checkpoint_succession import (
    CheckpointSuccessionError,
    SuccessionDenialCode,
    assert_predecessor_terminal,
)
from tests._release_helpers import dev1_adoption, dev1_config, dev1_draft

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[5]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: Project code of the empty-repo fixture.
SCOPE = "QR"

DEV1_KEY = "REL-0.7.0.dev1"
DEV2_KEY = "REL-0.7.0.dev2"
DEV3_KEY = "REL-0.7.0.dev3"
DEV2 = "0.7.0.dev2"
DEV3 = "0.7.0.dev3"
SOURCE_SHA = "a" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"

#: The bundle every dev3 open carries, since dev3 requires membership.
DEV3_CREATE = {"version": DEV3, "membership_refs": ["milestone://epoch2/native-canary"]}


def state_with(versions: Sequence[str]) -> State:
    """Return the fixture state with every contract *versions* require promoted."""
    state = load_state(_EMPTY_STATE)
    for version in versions:
        for contract_id in required_contract_ids(version):
            promote_measured_contract(
                state,
                contract=PREFLIGHT_CONTRACTS[contract_id],
                scope_id=SCOPE,
                required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
            )
    return state


def burned_dev1() -> Release:
    """Return dev1 as its burn left it: partially released."""
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, _operation = burn_release(adopted, dev1_config(), None)
    return burned


def dev2_at(status: ReleaseStatus) -> Release:
    """Return the pinned dev2 record at *status*."""
    return Release(
        uid=UUID(int=53),
        key=DEV2_KEY,
        version=DEV2,
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=status,
        approval_ref="receipt://approval/dev2",
        source_sha=SOURCE_SHA,
        source_tree_sha="b" * 40,
        manifest_ref="artifact://release/dev2-manifest",
        manifest_digest=MANIFEST_DIGEST,
        revision=9,
    )


def context(
    tmp_path: Path,
    *,
    state: State,
    records: Sequence[Release],
) -> MethodContext:
    """Write *state* and *records* under *tmp_path* and bind a context to them."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, state)
    for record in records:
        record_release(state_path, record, recorded_at=datetime.now(UTC), summary="seed")
    return MethodContext(
        started_at="2026-09-17T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version=DEV3,
        state_path=state_path,
    )


def store_dev2_receipts(ctx: MethodContext) -> None:
    """Store one fresh receipt per required dev2 gate."""
    now = datetime.now(UTC)
    config = load_release_config(checkpoint_config_yaml(DEV2), train=V07_TRAIN)
    for gate in config.gates.required:
        record_checkpoint_receipt(
            Path(str(ctx.state_path)),
            CheckpointGateReceipt(
                gate=gate,
                release_key=DEV2_KEY,
                source_sha=SOURCE_SHA,
                manifest_digest=MANIFEST_DIGEST,
                issued_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(hours=1),
                receipt_ref=f"checkpoint-receipt://{DEV2_KEY}/{gate.value}/seed",
            ),
            recorded_at=now,
            summary="seed",
        )


def advance_row(closed_key: str, opened_key: str) -> TrainAdvanceRecord:
    """Return a recorded advance from *closed_key* onto *opened_key*."""
    return TrainAdvanceRecord(
        train_id=V07_TRAIN.train_id,
        closed_key=closed_key,
        closed_revision=9,
        opened_key=opened_key,
        receipt_refs=("checkpoint-receipt://seed",),
        advanced_at=datetime.now(UTC),
        train_revision=1,
    )


def refused_create(ctx: MethodContext, params: dict[str, object]) -> str:
    """Return the refusal a create that must not open anything answers."""
    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(create(ctx, params))
    return str(excinfo.value)


# --- the walk ----------------------------------------------------------------


def test_create_refuses_dev3_before_the_dev2_advance(tmp_path: Path) -> None:
    """A baked dev2 nobody advanced past does not admit dev3."""
    ctx = context(
        tmp_path, state=state_with([DEV3]), records=[burned_dev1(), dev2_at(ReleaseStatus.BAKED)]
    )

    message = refused_create(ctx, DEV3_CREATE)

    assert SuccessionDenialCode.PREDECESSOR_NOT_ADVANCED.value in message
    assert f"eawf release advance {DEV2_KEY}" in message
    assert DEV3_KEY not in read_release_records(Path(str(ctx.state_path)))


def test_create_admits_dev3_after_the_advance_rpc_records_the_move(tmp_path: Path) -> None:
    """The same create is admitted once release.advance_train has run."""
    ctx = context(
        tmp_path, state=state_with([DEV3]), records=[burned_dev1(), dev2_at(ReleaseStatus.BAKED)]
    )
    store_dev2_receipts(ctx)
    refused_create(ctx, DEV3_CREATE)
    asyncio.run(advance(ctx, {"release_key": DEV2_KEY}))

    result = asyncio.run(create(ctx, DEV3_CREATE))

    assert result["release"]["key"] == DEV3_KEY
    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    assert result["supersedes_release_ref"] == DEV2_KEY
    assert result["measured_contracts"] == list(required_contract_ids(DEV3))
    shown = asyncio.run(show(ctx, {}))
    assert shown["current_checkpoint_index"] == 2
    assert shown["record"]["status"] == ReleaseStatus.DRAFT.value


def test_create_still_runs_admission_after_the_advance(tmp_path: Path) -> None:
    """The advance clears succession, never measurement: dev3 without contracts is refused."""
    ctx = context(
        tmp_path, state=state_with([]), records=[burned_dev1(), dev2_at(ReleaseStatus.BAKED)]
    )
    record_train_advance(
        Path(str(ctx.state_path)),
        advance_row(DEV2_KEY, DEV3_KEY),
        recorded_at=datetime.now(UTC),
        summary="seed",
    )

    message = refused_create(ctx, DEV3_CREATE)

    assert "measured_contract_missing" in message
    assert DEV3_KEY not in read_release_records(Path(str(ctx.state_path)))


# --- the abandoned predecessor ----------------------------------------------


def test_create_opens_dev2_over_the_burned_dev1_without_an_advance(tmp_path: Path) -> None:
    """dev1 was burned to partially_released and never advanced; dev2 opens anyway."""
    ctx = context(tmp_path, state=state_with([DEV2]), records=[burned_dev1()])

    result = asyncio.run(create(ctx, {"version": DEV2}))

    assert result["release"]["key"] == DEV2_KEY
    assert result["supersedes_release_ref"] == DEV1_KEY
    assert not train_advances_path(Path(str(ctx.state_path))).exists()


def test_assert_predecessor_terminal_admits_a_cancelled_predecessor_without_an_advance() -> None:
    """Cancelled is abandonment too, so it owes no advance."""
    cancelled = dev2_at(ReleaseStatus.CANCELLED)

    rung = assert_predecessor_terminal(V07_TRAIN, version=DEV3, predecessor=cancelled)

    assert rung is not None
    assert rung.release_key == DEV2_KEY


# --- the shipped predecessor -------------------------------------------------


@pytest.mark.parametrize("status", [ReleaseStatus.BAKED, ReleaseStatus.RELEASED])
def test_assert_predecessor_terminal_refuses_a_shipped_rung_with_no_advance(
    status: ReleaseStatus,
) -> None:
    """Both shipped statuses owe the advance; the refusal names the rung."""
    with pytest.raises(CheckpointSuccessionError) as excinfo:
        assert_predecessor_terminal(V07_TRAIN, version=DEV3, predecessor=dev2_at(status))

    assert excinfo.value.code is SuccessionDenialCode.PREDECESSOR_NOT_ADVANCED
    assert excinfo.value.predecessor_key == DEV2_KEY
    assert status.value in str(excinfo.value)


@pytest.mark.parametrize("status", [ReleaseStatus.BAKED, ReleaseStatus.RELEASED])
def test_assert_predecessor_terminal_admits_a_shipped_rung_once_advanced(
    status: ReleaseStatus,
) -> None:
    """With the advance recorded, a shipped predecessor admits its successor."""
    rung = assert_predecessor_terminal(
        V07_TRAIN, version=DEV3, predecessor=dev2_at(status), advanced_keys={DEV2_KEY}
    )

    assert rung is not None
    assert rung.release_key == DEV2_KEY


def test_create_refuses_the_successor_of_a_released_rung_without_an_advance(
    tmp_path: Path,
) -> None:
    """Released is as shipped as baked, and owes the same advance."""
    ctx = context(
        tmp_path,
        state=state_with([DEV3]),
        records=[burned_dev1(), dev2_at(ReleaseStatus.RELEASED)],
    )

    assert SuccessionDenialCode.PREDECESSOR_NOT_ADVANCED.value in refused_create(ctx, DEV3_CREATE)


def test_create_ignores_an_advance_past_a_different_rung(tmp_path: Path) -> None:
    """Only an advance past the predecessor itself counts."""
    ctx = context(
        tmp_path, state=state_with([DEV3]), records=[burned_dev1(), dev2_at(ReleaseStatus.BAKED)]
    )
    record_train_advance(
        Path(str(ctx.state_path)),
        advance_row(DEV1_KEY, DEV2_KEY),
        recorded_at=datetime.now(UTC),
        summary="seed",
    )

    assert SuccessionDenialCode.PREDECESSOR_NOT_ADVANCED.value in refused_create(ctx, DEV3_CREATE)


def test_create_refuses_a_live_predecessor_whatever_the_advances_say(tmp_path: Path) -> None:
    """A dev2 still verifying is live; a stray advance row cannot finish it."""
    ctx = context(
        tmp_path,
        state=state_with([DEV3]),
        records=[burned_dev1(), dev2_at(ReleaseStatus.VERIFYING)],
    )
    record_train_advance(
        Path(str(ctx.state_path)),
        advance_row(DEV2_KEY, DEV3_KEY),
        recorded_at=datetime.now(UTC),
        summary="seed",
    )

    assert SuccessionDenialCode.PREDECESSOR_LIVE.value in refused_create(ctx, DEV3_CREATE)


def test_create_refuses_over_a_corrupt_advance_collection(tmp_path: Path) -> None:
    """An unreadable advance row is a refusal, not a missing advance."""
    ctx = context(
        tmp_path, state=state_with([DEV3]), records=[burned_dev1(), dev2_at(ReleaseStatus.BAKED)]
    )
    train_advances_path(Path(str(ctx.state_path))).write_text("{}\n", encoding="utf-8")

    assert "is not an envelope" in refused_create(ctx, DEV3_CREATE)
