"""The live path: one principal's snooze of a pending action, and its assignment.

A disposable epoch-2 canary is seeded with a Milestone in acceptance review so the
acceptance-approval verb opens a real question; the snooze and assign verbs are then
called through the daemon's own dispatch and the Attention register read back through
``projection.attention.read`` exactly as a console reads it.

CON-125 and NTFY-023: a snooze hides the question from the principal who snoozed it and
from nobody else, moves no revision, and a snooze or assignment sent at a stale revision
is refused naming the revision the question stands at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.attention import attention_all, attention_mine
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods.delivery_approval import (
    DELIVERY_OPEN_APPROVAL_METHOD,
    DELIVERY_SEAL_APPROVAL_METHOD,
)
from eawf.runtime.daemon.methods.pending_action_disposition import (
    ACTION_ASSIGN_METHOD,
    ACTION_SNOOZE_METHOD,
)
from tests.integration.runtime.daemon.test_attention_audience_live import (
    ACTION,
    ASSIGNEE,
    OTHER,
    RECEIPT,
    _attention,
    _call,
    _open_params,
    tree,  # noqa: F401 - the canary fixture
)

LATER: Final = (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _snooze(actor: str, revision: int, key: str = "req-snooze-0001") -> dict[str, Any]:
    return {
        "urn": ACTION,
        "expected_revision": revision,
        "idempotency_key": key,
        "actor": actor,
        "snooze_until": LATER,
    }


def test_con_125_a_snooze_hides_the_question_from_its_principal_only(
    tree: CanaryProvision,  # noqa: F811
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    opened = _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=None))
    snoozed = _call(tree, runtime, ACTION_SNOOZE_METHOD, _snooze(ASSIGNEE, opened["revision"]))
    assert snoozed["revision"] == opened["revision"]
    assert "other principals still see it" in snoozed["reason"]
    register = _attention(tree, runtime)
    assert attention_mine(register, principal=ASSIGNEE).value == "0"
    assert attention_mine(register, principal=OTHER).value == "1"
    assert attention_all(register).value == "1"
    replay = _call(tree, runtime, ACTION_SNOOZE_METHOD, _snooze(ASSIGNEE, opened["revision"]))
    assert replay["canonical_sequence"] == snoozed["canonical_sequence"]


def test_ntfy_023_a_stale_snooze_is_refused_with_the_current_revision(
    tree: CanaryProvision,  # noqa: F811
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=None))
    with pytest.raises(Exception, match="revision_conflict") as refused:
        _call(tree, runtime, ACTION_SNOOZE_METHOD, _snooze(ASSIGNEE, 9))
    assert "revision 1" in str(refused.value)


def test_con_125_assign_moves_the_question_between_principals_counts(
    tree: CanaryProvision,  # noqa: F811
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    opened = _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=ASSIGNEE))
    assigned = _call(
        tree,
        runtime,
        ACTION_ASSIGN_METHOD,
        {
            "urn": ACTION,
            "expected_revision": opened["revision"],
            "idempotency_key": "req-assign-0001",
            "actor": ASSIGNEE,
            "assignee": OTHER,
        },
    )
    assert assigned["revision"] == opened["revision"] + 1
    register = _attention(tree, runtime)
    assert attention_mine(register, principal=ASSIGNEE).value == "0"
    assert attention_mine(register, principal=OTHER).value == "1"


def test_con_125_a_sealed_question_takes_no_snooze(
    tree: CanaryProvision,  # noqa: F811
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=None))
    sealed = _call(
        tree,
        runtime,
        DELIVERY_SEAL_APPROVAL_METHOD,
        {
            "urn": ACTION,
            "expected_revision": 1,
            "idempotency_key": "req-seal-0001",
            "actor": ASSIGNEE,
            "resolver": {"principal_kind": "human", "principal_id": ASSIGNEE},
            "option_id": "approve",
            "receipt_ref": RECEIPT,
        },
    )
    assert sealed["dispositions"][0]["acted_at"] is not None
    with pytest.raises(Exception, match="sealed"):
        _call(tree, runtime, ACTION_SNOOZE_METHOD, _snooze(OTHER, sealed["revision"], "k-2"))
