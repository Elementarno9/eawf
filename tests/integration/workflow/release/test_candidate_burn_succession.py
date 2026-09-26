"""A burned CANDIDATE is finished with, so its successor rung may open.

Succession reads the status machine's terminal set, so once an adopted
candidate is burned to ``partially_released`` the next rung's
``release.create`` is admitted and its stored record names the burned key
as the version it supersedes. A candidate that was never burned -- with
or without an adoption -- can still move, and the successor is refused
``predecessor_live``. Every step runs through the daemon release verbs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.release import Release, ReleaseStatus, release_key
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import burn, create
from eawf.runtime.daemon.methods.release_disposition import adopt
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.records import read_release_record, record_release
from eawf.workflow.verify import checkpoint_succession
from tests._release_helpers import dev1_adoption, release_record

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: Project code of the empty-repo fixture.
SCOPE = "QR"

DEV1_KEY = release_key("0.7.0.dev1")
DEV2 = "0.7.0.dev2"
DEV2_KEY = release_key(DEV2)


def dev2_admitted_state() -> State:
    """Return the fixture state with every contract dev2 requires promoted."""
    state = load_state(_EMPTY_STATE)
    for contract_id in required_contract_ids(DEV2):
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id=SCOPE,
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )
    return state


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return a context whose store holds the pinned dev1 candidate."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, dev2_admitted_state())
    record_release(state_path, release_record(), recorded_at=datetime.now(UTC), summary="seed")
    return MethodContext(
        started_at="2026-09-26T00:00:00+00:00",
        pid=3636,
        protocol_version="1",
        version=DEV2,
        state_path=state_path,
    )


def stored(ctx: MethodContext, key: str) -> Release:
    """Return the record the collection currently holds for *key*."""
    record = read_release_record(Path(str(ctx.state_path)), key)
    assert record is not None
    return record


def adopt_dev1(ctx: MethodContext) -> Release:
    """Adopt the stored dev1 candidate through ``release.adopt``."""
    pinned = stored(ctx, DEV1_KEY)
    reply = asyncio.run(
        adopt(
            ctx,
            {
                "release": pinned.model_dump(mode="json"),
                "expected_revision": pinned.revision,
                "adoption": dev1_adoption().model_dump(mode="json"),
            },
        )
    )
    return Release.model_validate(reply["release"])


def burn_dev1(ctx: MethodContext, adopted: Release) -> dict[str, Any]:
    """Burn the adopted dev1 candidate through ``release.burn``."""
    return asyncio.run(
        burn(
            ctx,
            {
                "release": adopted.model_dump(mode="json"),
                "expected_revision": adopted.revision,
                "idempotency_key": "candidate-burn-0.7.0.dev1",
                "reason": "published to every target from the candidate; the version is spent",
            },
        )
    )


def test_the_adopted_candidate_burns_through_the_daemon(ctx: MethodContext) -> None:
    """Adopt and burn leave a stored record at partially_released."""
    adopted = adopt_dev1(ctx)
    assert adopted.status is ReleaseStatus.CANDIDATE

    reply = burn_dev1(ctx, adopted)

    assert reply["operation_ref"] is None
    assert reply["release"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value
    assert stored(ctx, DEV1_KEY).status is ReleaseStatus.PARTIALLY_RELEASED


def test_create_opens_the_successor_of_a_burned_candidate(ctx: MethodContext) -> None:
    """The successor is admitted and its stored record names the burned key."""
    burn_dev1(ctx, adopt_dev1(ctx))

    reply = asyncio.run(create(ctx, {"version": DEV2}))

    assert reply["supersedes_release_ref"] == DEV1_KEY
    assert reply["release"]["supersedes_release_ref"] == DEV1_KEY
    draft = stored(ctx, DEV2_KEY)
    assert draft.status is ReleaseStatus.DRAFT
    assert draft.supersedes_release_ref == DEV1_KEY


def test_create_reds_when_succession_ignores_partially_released(
    ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof: a check blind to the burn refuses the successor."""
    burn_dev1(ctx, adopt_dev1(ctx))
    monkeypatch.setattr(
        checkpoint_succession,
        "SUCCEEDABLE_STATUSES",
        checkpoint_succession.SUCCEEDABLE_STATUSES - {ReleaseStatus.PARTIALLY_RELEASED.value},
    )

    with pytest.raises(DaemonValidationError, match="predecessor_live"):
        asyncio.run(create(ctx, {"version": DEV2}))


def test_create_refuses_the_successor_of_a_candidate_left_unburned(ctx: MethodContext) -> None:
    """A pinned candidate can still move, so its successor is refused."""
    with pytest.raises(DaemonValidationError, match="predecessor_live"):
        asyncio.run(create(ctx, {"version": DEV2}))

    assert read_release_record(Path(str(ctx.state_path)), DEV2_KEY) is None


def test_create_refuses_the_successor_of_an_adopted_but_unburned_candidate(
    ctx: MethodContext,
) -> None:
    """Adoption alone does not end the checkpoint; the burn does."""
    adopt_dev1(ctx)

    with pytest.raises(DaemonValidationError, match="predecessor_live"):
        asyncio.run(create(ctx, {"version": DEV2}))
