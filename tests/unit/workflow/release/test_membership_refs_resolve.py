"""A membership reference is resolved when the checkpoint is opened.

A rung that requires membership used to be admitted on a non-empty
``membership_refs`` alone, so an invented reference passed ``release
create`` and was only caught by the preflight ``membership`` row -- with
a DRAFT record already on file claiming a bundle nobody accepted. These
tests pin that the open applies the preflight's resolution itself: every
reference must name a COMPLETED Milestone recorded in a declared canary
of the committed export, and a refused open writes no record.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eawf.kernel.spec.release import Release, ReleaseStatus
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import create
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.evidence.provider_certification import CanaryEvidence
from eawf.workflow.release.admission import (
    assert_membership_resolves,
    create_checkpoint_release,
    required_contract_ids,
)
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.advance import TrainAdvanceRecord
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import read_release_record, record_release
from eawf.workflow.release.train import V07_TRAIN
from eawf.workflow.release.train_store import record_train_advance
from tests._release_helpers import (
    DEV3_MEMBERSHIP_REF,
    NOW,
    accepted_canary_export,
    dev1_adoption,
    dev1_config,
    dev1_draft,
    release_record,
    stage_canary_export,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

DEV2 = "0.7.0.dev2"
DEV3 = "0.7.0.dev3"
DEV3_KEY = "REL-0.7.0.dev3"
UID = UUID(int=723)

#: The reference the accepted fixture Milestone is recorded under.
ACCEPTED_REF = DEV3_MEMBERSHIP_REF

#: A plausible reference no canary ever recorded.
FABRICATED_REF = "milestone://epoch2/invented"


def evidence(*records: dict[str, Any]) -> CanaryEvidence:
    """Return a validated export accepting *records* (the dev3 bundle by default)."""
    return CanaryEvidence.model_validate(accepted_canary_export(*records))


def no_milestones() -> CanaryEvidence:
    """Return a validated export in which no Milestone is recorded."""
    document = accepted_canary_export()
    document["milestones"] = []
    return CanaryEvidence.model_validate(document)


def dev3_state() -> State:
    """Return the fixture state with both dev3 contracts promoted."""
    state = load_state(_EMPTY_STATE)
    for contract_id in required_contract_ids(DEV3):
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id="QR",
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )
    return state


def open_dev3(refs: tuple[str, ...], canary: CanaryEvidence | None) -> Release:
    """Open dev3 carrying *refs* against *canary*."""
    return create_checkpoint_release(
        dev3_state(),
        train=V07_TRAIN,
        version=DEV3,
        uid=UID,
        membership_refs=refs,
        canary_evidence=canary,
    )


# --- the library admission ---------------------------------------------------


def test_create_checkpoint_release_refuses_a_fabricated_reference() -> None:
    with pytest.raises(UserError) as excinfo:
        open_dev3((FABRICATED_REF,), evidence())

    assert excinfo.value.kind == "membership_unresolved"
    assert FABRICATED_REF in str(excinfo.value)
    assert "membership_unresolved" in str(excinfo.value)


def test_create_checkpoint_release_admits_an_accepted_bundle() -> None:
    record = open_dev3((ACCEPTED_REF,), evidence())

    assert record.status is ReleaseStatus.DRAFT
    assert record.membership_refs == (ACCEPTED_REF,)


def test_create_checkpoint_release_refuses_every_reference_without_an_export() -> None:
    with pytest.raises(UserError) as excinfo:
        open_dev3((ACCEPTED_REF,), None)

    assert excinfo.value.kind == "membership_unresolved"
    assert "1 of 1" in str(excinfo.value)


def test_create_checkpoint_release_refuses_against_an_export_with_no_milestones() -> None:
    with pytest.raises(UserError, match="membership_unresolved"):
        open_dev3((ACCEPTED_REF,), no_milestones())


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "ACCEPTANCE_REVIEW", "CANCELLED"])
def test_create_checkpoint_release_refuses_an_unfinished_milestone(status: str) -> None:
    with pytest.raises(UserError) as excinfo:
        open_dev3((ACCEPTED_REF,), evidence({"status": status}))

    assert "milestone_incomplete" in str(excinfo.value)
    # The refusal names the status the admission actually requires.
    assert "COMPLETED Milestone" in str(excinfo.value)


def test_create_checkpoint_release_refuses_a_milestone_outside_a_declared_canary() -> None:
    with pytest.raises(UserError) as excinfo:
        open_dev3((ACCEPTED_REF,), evidence({"project_code": "ELSEWHERE"}))

    assert "undeclared_canary" in str(excinfo.value)


def test_create_checkpoint_release_names_only_the_unresolved_reference() -> None:
    with pytest.raises(UserError) as excinfo:
        open_dev3((ACCEPTED_REF, FABRICATED_REF), evidence())

    message = str(excinfo.value)
    assert "1 of 2" in message
    assert FABRICATED_REF in message
    assert f"{ACCEPTED_REF}:" not in message


def test_create_checkpoint_release_keeps_the_empty_membership_refusal() -> None:
    with pytest.raises(ValueError, match="requires non-empty membership_refs"):
        open_dev3((), evidence())


def test_create_checkpoint_release_skips_resolution_on_a_rung_without_membership() -> None:
    state = load_state(_EMPTY_STATE)
    for contract_id in required_contract_ids(DEV2):
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id="QR",
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )

    record = create_checkpoint_release(state, train=V07_TRAIN, version=DEV2, uid=UID)

    assert record.membership_refs == ()


def test_assert_membership_resolves_admits_no_references_without_an_export() -> None:
    assert_membership_resolves(None, ())


# --- the release.create verb -------------------------------------------------


def _context(tmp_path: Path, document: dict[str, Any] | str | None) -> MethodContext:
    """Return a context whose checkout is ready for a dev3 open.

    dev1 is burned, dev2 baked and advanced past, and both dev3 contracts
    are promoted, so the membership references are the only thing left
    for the open to refuse on. *document* is written as the canary
    export: a mapping as JSON, a string verbatim, ``None`` for no export.
    """
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, dev3_state())
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, _operation = burn_release(adopted, dev1_config(), None)
    record_release(state_path, burned, recorded_at=NOW, summary=f"burn {burned.key}")
    baked = release_record(
        key="REL-0.7.0.dev2",
        version=DEV2,
        status=ReleaseStatus.BAKED,
        approval_ref="receipt://approval/dev2",
    )
    record_release(state_path, baked, recorded_at=NOW, summary=f"bake {baked.key}")
    record_train_advance(
        state_path,
        TrainAdvanceRecord(
            train_id=V07_TRAIN.train_id,
            closed_key=baked.key,
            closed_revision=baked.revision,
            opened_key=DEV3_KEY,
            receipt_refs=("checkpoint-receipt://REL-0.7.0.dev2/migration/seed",),
            advanced_at=NOW,
            train_revision=1,
        ),
        recorded_at=NOW,
        summary="advance past REL-0.7.0.dev2",
    )
    if document is not None:
        stage_canary_export(tmp_path, document)
    return MethodContext(
        started_at="2026-09-04T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0.dev2",
        state_path=state_path,
    )


def test_release_create_refuses_a_fabricated_reference_and_writes_no_draft(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path, accepted_canary_export())

    with pytest.raises(DaemonValidationError, match="membership_unresolved"):
        asyncio.run(create(ctx, {"version": DEV3, "membership_refs": [FABRICATED_REF]}))

    assert read_release_record(Path(str(ctx.state_path)), DEV3_KEY) is None


def test_release_create_refuses_without_a_committed_export_and_writes_no_draft(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path, None)

    with pytest.raises(DaemonValidationError, match="membership_unresolved"):
        asyncio.run(create(ctx, {"version": DEV3, "membership_refs": [ACCEPTED_REF]}))

    assert read_release_record(Path(str(ctx.state_path)), DEV3_KEY) is None


def test_release_create_refuses_an_unreadable_export_and_writes_no_draft(tmp_path: Path) -> None:
    ctx = _context(tmp_path, "{not json")

    with pytest.raises(DaemonValidationError, match="membership_unresolved"):
        asyncio.run(create(ctx, {"version": DEV3, "membership_refs": [ACCEPTED_REF]}))

    assert read_release_record(Path(str(ctx.state_path)), DEV3_KEY) is None


def test_release_create_opens_dev3_on_an_accepted_bundle(tmp_path: Path) -> None:
    ctx = _context(tmp_path, accepted_canary_export())

    result = asyncio.run(create(ctx, {"version": DEV3, "membership_refs": [ACCEPTED_REF]}))

    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    stored = read_release_record(Path(str(ctx.state_path)), DEV3_KEY)
    assert stored is not None
    assert stored.membership_refs == (ACCEPTED_REF,)
