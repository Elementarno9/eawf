"""Driving the adopted ``0.7.0.dev1`` record to a terminal state.

The live record reached DRAFT and stopped. ``approve`` refused because
the branch had not merged, and that refusal is correct;
``PARTIALLY_RELEASED`` sat behind it, and ``CANCELLED`` was both
untruthful and unreachable. So the record had no terminal state it could
honestly reach, which is what this module closes over a fixture.

The drive is two operator verbs and no new terminal machinery.
``release.adopt`` writes the observed facts; ``release.burn`` -- the same
verb the recovery path uses, reached through the same
:func:`~eawf.workflow.release.publication.burn_release` -- ends the
checkpoint. What makes the second work on an adopted record is the
absence of an operation, not a substitute for one: nothing was ever
dispatched through this machinery, so no publication operation is
invented to abandon, and the assertion that none is written is part of
the claim rather than an aside.

The burned record is then distinguishable from the one the recovery path
produces by a typed field: it carries an ``adoption`` and no
``approval_ref``, and the record model forbids the other combination.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import burn, show
from eawf.runtime.daemon.methods.release_disposition import adopt
from eawf.surfaces.cli.commands.release import RELEASE_RPC_METHODS, release_app
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.advance import (
    ADVANCING_STATUSES,
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    assert_checkpoint_terminal,
    draft_release_for,
)
from eawf.workflow.release.ledger import ledger_path
from eawf.workflow.release.lifecycle import (
    RELEASE_TRANSITIONS,
    TERMINAL_RELEASE_STATUSES,
    next_release_statuses,
)
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import read_release_record, record_envelope_id
from eawf.workflow.release.train import V07_TRAIN
from tests._release_helpers import (
    ADOPTION_REASON,
    dev1_adoption,
    dev1_config,
    dev1_draft,
)

pytestmark = pytest.mark.integration

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"
DEV2_VERSION = "0.7.0.dev2"

#: The reason the burn is called with. It names the disposition rather
#: than the failure, because a terminal status carries no later
#: transition that could supply one.
BURN_REASON = (
    "the version is spent: published out of band to four targets, the pypi "
    "filename cannot be reused, and 0.7.0.dev2 supersedes it"
)

BURN_KEY = "adopted-burn-0.7.0.dev1"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a throwaway state root the drive may record into."""
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(path, load_state(_EMPTY_STATE))
    return path


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a method context bound to the recording state root."""
    return MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7746,
        protocol_version="1",
        version=DEV1_VERSION,
        state_path=state_path,
    )


def burn_params(record: Release, **overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.burn`` params for *record*."""
    params: dict[str, Any] = {
        "release": record.model_dump(mode="json"),
        "expected_revision": record.revision,
        "idempotency_key": BURN_KEY,
        "reason": BURN_REASON,
    }
    params.update(overrides)
    return params


def drive(ctx: MethodContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adopt then burn a fresh dev1 draft, through the operator verbs.

    Returns:
        The adopt reply and the burn reply, in order.
    """
    adopted = asyncio.run(
        adopt(
            ctx,
            {
                "release": dev1_draft().model_dump(mode="json"),
                "expected_revision": 0,
                "adoption": dev1_adoption().model_dump(mode="json"),
            },
        )
    )
    burned = asyncio.run(burn(ctx, burn_params(Release.model_validate(adopted["release"]))))
    return adopted, burned


# --- the drive ---------------------------------------------------------


def test_the_adopted_record_reaches_a_terminal_state(ctx: MethodContext) -> None:
    """Two operator verbs take the record from DRAFT to the burn."""
    _adopted, burned = drive(ctx)

    record = Release.model_validate(burned["release"])
    assert record.status is ReleaseStatus.PARTIALLY_RELEASED
    assert record.status in TERMINAL_RELEASE_STATUSES
    assert next_release_statuses(record.status) == frozenset()
    assert RELEASE_TRANSITIONS[record.status] == frozenset()


def test_the_burned_record_still_carries_every_observed_fact(ctx: MethodContext) -> None:
    """The burn freezes the adoption rather than re-deriving it."""
    adopted, burned = drive(ctx)

    before = Release.model_validate(adopted["release"])
    after = Release.model_validate(burned["release"])
    assert after.adoption == before.adoption
    assert dict(after.target_statuses) == dict(before.target_statuses)
    assert after.target_statuses["npm"] is ReleaseTargetStatus.OBSERVED_SUCCESS
    assert after.target_statuses["pypi"] is ReleaseTargetStatus.OBSERVED_MISMATCH


def test_the_burned_record_claims_no_approval_and_no_operation(ctx: MethodContext) -> None:
    """A burned adoption is distinguishable from a burned release."""
    _adopted, burned = drive(ctx)

    record = Release.model_validate(burned["release"])
    assert record.approval_ref is None
    assert record.adoption is not None
    assert record.publication_operation_ref is None
    assert burned["operation_ref"] is None
    assert burned["operation"] is None


def test_the_burn_fabricates_no_publication_operation(ctx: MethodContext, state_path: Path) -> None:
    """No episode ran through this machinery, so none is written down."""
    drive(ctx)

    assert ledger_path(state_path).exists() is False


def test_show_answers_the_terminal_status_afterwards(ctx: MethodContext) -> None:
    """A reader asking about the checkpoint is told it is burned."""
    drive(ctx)

    answer = asyncio.run(show(ctx, {"version": DEV1_VERSION}))

    assert answer["record"] is not None
    assert answer["record"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value


def test_the_burn_records_its_reason(ctx: MethodContext, state_path: Path) -> None:
    """The terminal row says why the version stopped moving."""
    _adopted, burned = drive(ctx)

    assert burned["reason"] == BURN_REASON
    rows = (state_path.parent / "store" / "release_record.jsonl").read_text(encoding="utf-8")
    assert BURN_REASON in rows
    assert ADOPTION_REASON in rows


def test_the_collection_reads_back_the_burned_record(ctx: MethodContext, state_path: Path) -> None:
    """The terminal record is the one a later reader finds."""
    _adopted, burned = drive(ctx)

    settled = read_release_record(state_path, DEV1_KEY)
    assert settled is not None
    assert settled.status is ReleaseStatus.PARTIALLY_RELEASED
    assert settled.revision == 2
    assert record_envelope_id(settled) == burned["release_record_id"]


def test_a_repeated_burn_answers_the_recorded_record(ctx: MethodContext) -> None:
    """A replay reports the burned record, not the status it left."""
    adopted, _burned = drive(ctx)

    replay = asyncio.run(burn(ctx, burn_params(Release.model_validate(adopted["release"]))))

    assert replay["replayed"] is True
    assert replay["release"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value
    assert replay["release"]["revision"] == 2


# --- the burn stays the burn -------------------------------------------


def test_burn_without_an_operation_refuses_a_record_with_no_adoption() -> None:
    """The draft burn is the adoption route, not a way past approval."""
    with pytest.raises(ValueError, match="no publication operation and no adoption"):
        burn_release(dev1_draft(), dev1_config(), None)


def test_burn_refuses_a_draft_it_was_handed_no_adoption_for(ctx: MethodContext) -> None:
    """Through the verb, the same refusal reads as a validation failure."""
    with pytest.raises(DaemonValidationError, match="no publication operation is open"):
        asyncio.run(burn(ctx, burn_params(dev1_draft())))


def test_burn_returns_no_abandoned_operation_for_an_adopted_record() -> None:
    """There is nothing to abandon, so nothing is returned as abandoned."""
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())

    burned, abandoned = burn_release(adopted, dev1_config(), None)

    assert abandoned is None
    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
    assert burned.revision == adopted.revision + 1


def test_the_burned_record_cannot_be_reopened(ctx: MethodContext) -> None:
    """Correction is a new version, never a return to this one."""
    _adopted, burned = drive(ctx)

    record = Release.model_validate(burned["release"])
    for forbidden in (ReleaseStatus.DRAFT, ReleaseStatus.CANCELLED, ReleaseStatus.CANDIDATE):
        assert forbidden not in next_release_statuses(record.status)


# --- what the terminal record lets the next checkpoint do ---------------


def test_the_train_refuses_to_walk_past_the_adopted_burn(ctx: MethodContext) -> None:
    """A burned rung is terminal but never a shipped checkpoint."""
    _adopted, burned = drive(ctx)
    record = Release.model_validate(burned["release"])
    assert record.status not in ADVANCING_STATUSES

    with pytest.raises(TrainAdvanceError) as excinfo:
        assert_checkpoint_terminal(V07_TRAIN, record)

    assert excinfo.value.code is TrainAdvanceDenialCode.CHECKPOINT_NOT_TERMINAL


def test_the_next_checkpoint_can_supersede_the_burned_one(ctx: MethodContext) -> None:
    """The terminal record is the predecessor dev2 links back to."""
    _adopted, burned = drive(ctx)

    successor = draft_release_for(
        V07_TRAIN.checkpoint_for_version(DEV2_VERSION), uid=dev1_draft().uid
    ).model_copy(update={"supersedes_release_ref": burned["release"]["key"]})

    assert Release.model_validate(successor.model_dump(mode="json")).supersedes_release_ref == (
        DEV1_KEY
    )
    assert required_contract_ids(DEV2_VERSION) is not None


# --- the surface the operator reaches it through ------------------------


def test_both_disposition_verbs_are_reachable_from_the_cli() -> None:
    """A terminal move no operator can invoke is the defect, not the fix."""
    declared = {command.name for command in release_app.registered_commands}

    assert RELEASE_RPC_METHODS["adopt"] == "release.adopt"
    assert RELEASE_RPC_METHODS["cancel"] == "release.cancel"
    assert {"adopt", "cancel", "burn"} <= declared
