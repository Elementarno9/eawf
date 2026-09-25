"""The Attention route reads an opened acceptance question by its own urn.

Before this fix ``PendingAction`` carried no ``urn`` of its own: the row the
document held for an opened acceptance question serialised without one, and
the shared projection row validator refuses any row that states none. One
malformed row was enough to fail the whole route's read, not just that row,
so an operator opening ``/verify`` on a Milestone in review could not see
the Attention route at all -- this is the gap W38 hit live on the
accepted-canary tree.

The walk that opens the question is rebuilt here rather than a row seeded
by hand, which is what makes this a gate on the real write path and not on
a row this suite invented to look right.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final
from unittest import mock

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.workflow.release._canary_acceptance_walk import CanaryWalk, walk_canary

pytestmark = pytest.mark.integration

#: The one pending action the walk opens and seals.
ACTION_KEY: Final = "ACT-0001"

#: A protected approval's two required answers, offered at their floor.
_TWO_OPTIONS: Final = [
    {"option_id": "approve", "label": "Approve", "effect": "approve"},
    {"option_id": "decline", "label": "Decline", "effect": "decline"},
]


@pytest.fixture(scope="module")
def walked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CanaryWalk]:
    """Walk one canary Milestone to acceptance, once for the whole module."""
    base = tmp_path_factory.mktemp("attention-walk")
    scratch = base / "scratch"
    scratch.mkdir()
    with mock.patch.object(tempfile, "tempdir", str(scratch)):
        yield walk_canary(base / "repo", base / "runtime")


def _row(**overrides: Any) -> dict[str, Any]:
    """Return one minimal pending-action payload, with *overrides* applied."""
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).isoformat()
    payload: dict[str, Any] = {
        "id": ACTION_KEY,
        "kind": "operator_decision",
        "subject_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030",
        "question": "Which way?",
        "options": _TWO_OPTIONS,
        "idempotency_key": "req-0001",
        "status": "CREATED",
        "requested_by": {"principal_kind": "human", "principal_id": "OP-0001"},
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return payload


# ---------- the gate: the route reads the opened question by its urn ----------


def test_the_attention_route_addresses_the_opened_question_by_its_urn(
    walked: CanaryWalk,
) -> None:
    """Once the walk has opened and sealed the question, Attention still reads it."""
    document = read_document(document_path(walked.canary))

    projection = build_route_projection(
        route="attention",
        document=document,
        cursor=int(document.get(CANONICAL_SEQUENCE_KEY, 0)),
        scope_id="canary",
        generated_at=datetime.now(UTC),
    )

    assert [row.key for row in projection.rows] == [ACTION_KEY]
    row = projection.rows[0]
    assert row.urn == f"{walked.container}/pending-action/{ACTION_KEY}"
    assert row.status.value == "SEALED"


def test_a_row_carrying_its_own_urn_is_read_back_unchanged(walked: CanaryWalk) -> None:
    """A row a fresh write already addressed is not re-derived on the way back in."""
    document = read_document(document_path(walked.canary))
    stored = document_rows(document, Epoch2Collection.PENDING_ACTION)[ACTION_KEY]
    assert stored["urn"] == f"{walked.container}/pending-action/{ACTION_KEY}"

    action = PendingAction.model_validate(stored)

    assert str(action.urn) == stored["urn"]


# ---------- the floor: an unaddressable row still fails validation ----------


def test_a_pending_action_row_addressing_a_workspace_scoped_subject_still_fails_validation() -> (
    None
):
    """A well-formed subject in the wrong scope still leaves urn unaddressable.

    A pending action is repository-scoped; a Release is not. The subject
    parses fine on its own, but a repository-scoped urn cannot be built
    beside a workspace-scoped container, so the backfill declines and the
    row is refused for the field it could not fill -- not silently
    defaulted into an address that names the wrong repository.
    """
    with pytest.raises(ValidationError, match="urn"):
        PendingAction.model_validate(
            _row(subject_ref="eawf://WSP-MAIN/PRJ-EAWF/_/release/REL-0.7.0")
        )


def test_a_pending_action_row_with_an_unparseable_subject_ref_still_fails_validation() -> None:
    """A subject that is not a urn at all leaves nothing to derive from."""
    with pytest.raises(ValidationError, match="urn"):
        PendingAction.model_validate(_row(subject_ref="not a urn"))


def test_a_pending_action_row_missing_id_and_urn_still_fails_validation() -> None:
    """With no key to derive from either, the row is refused the same way."""
    payload = _row()
    del payload["id"]
    with pytest.raises(ValidationError, match="urn"):
        PendingAction.model_validate(payload)


# ---------- backfill agrees with a fresh write ----------


def test_the_backfilled_urn_matches_a_freshly_written_row() -> None:
    """A legacy row (no stored urn) reads at the same urn a fresh write would carry."""
    legacy = _row()
    fresh = _row(urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001")

    assert PendingAction.model_validate(legacy).urn == PendingAction.model_validate(fresh).urn
