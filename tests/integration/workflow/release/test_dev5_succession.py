"""``0.7.0.dev5`` opens only over a ``dev4`` that was burned, and names it.

``REL-0.7.0.dev4`` was published to every registry after its record was
pinned, so it stands at CANDIDATE with gates that can no longer pass. The
product-canary rung cannot open over a record that can still move: its
create is admitted only once dev4 has been adopted at CANDIDATE and burned
to ``partially_released``, and the dev5 record it stores names dev4 as the
version it supersedes. Every step runs through the daemon release verbs
over a scratch root that carries this checkout's committed dev5 canary
export and canary-window receipts, so the create is refused by succession
and by nothing else.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.release import Release, ReleaseStatus, release_key
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import burn, create
from eawf.runtime.daemon.methods.release_disposition import adopt
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.provider_certification import CANARY_EVIDENCE_DIRS
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.records import read_release_record, record_release
from tests._release_helpers import dev1_adoption, release_record

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

DEV4 = "0.7.0.dev4"
DEV5 = "0.7.0.dev5"
DEV4_KEY = release_key(DEV4)
DEV5_KEY = release_key(DEV5)

#: The COMPLETED canary Milestone bundle the committed dev5 export records.
MEMBERSHIP_REF = (
    "eawf://WSP-W37CANARY/PRJ-W37CANARY/REP-W37CANARY/milestone/MLS-0001#MAB-0001-MLS-0001"
)


def dev4_candidate() -> Release:
    """Return a pinned dev4 candidate, the shape the published record stands in."""
    return release_record(
        key=DEV4_KEY,
        version=DEV4,
        authority_epoch=2,
        membership_refs=(MEMBERSHIP_REF,),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return a context over a scratch root holding the pinned dev4 candidate."""
    assert required_contract_ids(DEV5) == ()
    evidence = Path(*CANARY_EVIDENCE_DIRS[DEV5_KEY])
    shutil.copytree(_REPO_ROOT / evidence, tmp_path / evidence)
    state_path = tmp_path / ".ea" / "state.json"
    atomic_write_state(state_path, load_state(_EMPTY_STATE))
    record_release(state_path, dev4_candidate(), recorded_at=datetime.now(UTC), summary="seed")
    return MethodContext(
        started_at="2026-09-27T00:00:00+00:00",
        pid=3637,
        protocol_version="1",
        version=DEV5,
        state_path=state_path,
    )


def stored(ctx: MethodContext, key: str) -> Release | None:
    """Return the record the collection currently holds for *key*."""
    return read_release_record(Path(str(ctx.state_path)), key)


def adopt_dev4(ctx: MethodContext) -> Release:
    """Adopt the stored dev4 candidate's four read-backs through ``release.adopt``."""
    pinned = stored(ctx, DEV4_KEY)
    assert pinned is not None
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


def burn_dev4(ctx: MethodContext, adopted: Release) -> dict[str, Any]:
    """Burn the adopted dev4 candidate through ``release.burn``."""
    return asyncio.run(
        burn(
            ctx,
            {
                "release": adopted.model_dump(mode="json"),
                "expected_revision": adopted.revision,
                "idempotency_key": "candidate-burn-0.7.0.dev4",
                "reason": "published to every target after the pin; the version is spent",
            },
        )
    )


def create_dev5(ctx: MethodContext) -> dict[str, Any]:
    """Open dev5 through ``release.create`` on the committed membership bundle."""
    return asyncio.run(create(ctx, {"version": DEV5, "membership_refs": [MEMBERSHIP_REF]}))


def test_dev4_burns_from_candidate_to_partially_released(ctx: MethodContext) -> None:
    adopted = adopt_dev4(ctx)
    assert adopted.status is ReleaseStatus.CANDIDATE

    reply = burn_dev4(ctx, adopted)

    assert reply["release"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value
    burned = stored(ctx, DEV4_KEY)
    assert burned is not None and burned.status is ReleaseStatus.PARTIALLY_RELEASED


def test_dev5_opens_over_the_burned_dev4_and_names_it(ctx: MethodContext) -> None:
    burn_dev4(ctx, adopt_dev4(ctx))

    reply = create_dev5(ctx)

    assert reply["supersedes_release_ref"] == DEV4_KEY
    draft = stored(ctx, DEV5_KEY)
    assert draft is not None
    assert draft.status is ReleaseStatus.DRAFT
    assert draft.supersedes_release_ref == DEV4_KEY
    assert draft.membership_refs == (MEMBERSHIP_REF,)


def test_dev5_is_refused_while_dev4_stands_at_candidate(ctx: MethodContext) -> None:
    """Gate-fire proof: an unburned dev4 keeps the product canary closed."""
    with pytest.raises(DaemonValidationError, match="predecessor_live"):
        create_dev5(ctx)

    assert stored(ctx, DEV5_KEY) is None


def test_dev5_is_refused_over_an_adopted_but_unburned_dev4(ctx: MethodContext) -> None:
    adopt_dev4(ctx)

    with pytest.raises(DaemonValidationError, match="predecessor_live"):
        create_dev5(ctx)

    assert stored(ctx, DEV5_KEY) is None


def test_dev5_is_refused_without_its_canary_window_receipts(ctx: MethodContext) -> None:
    """Over a burned dev4, the missing receipt file is what refuses the open."""
    burn_dev4(ctx, adopt_dev4(ctx))
    receipts = Path(str(ctx.state_path)).parent.parent.joinpath(
        *CANARY_EVIDENCE_DIRS[DEV5_KEY], "product-canary-receipts.json"
    )
    receipts.unlink()

    with pytest.raises(DaemonValidationError, match="canary_receipts_unbound"):
        create_dev5(ctx)

    assert stored(ctx, DEV5_KEY) is None
