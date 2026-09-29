"""AUTH-021: promotion of a backlog draft is an ordinary plan apply.

A plan that names an existing native ``DRAFT`` Task places it rather than
creating a second record: the apply takes the draft through the registry's
own promotion edge, so the one identifier survives from backlog idea to
planned work, and the Batch lists it. A key that names anything else -- a
Task already past the backlog, or a row imported from the previous epoch
-- stays a claimed key the apply refuses, writing nothing.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from eawf.platform.install.canary import CanaryProvision
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    legacy_task_row,
    seed,
    seed_row,
)
from tests.integration.workflow.planning.test_plan_revision_apply import (
    apply_plan,
    approve,
    canary_runtime_under_tmp,
    collection,
    planned_canary,
    submit,
)

__all__ = ["canary_runtime_under_tmp"]

pytestmark = pytest.mark.integration


def _approved_over(tmp_path: Path, task: dict[str, Any], *, code: str) -> CanaryProvision:
    """Return an approved canary whose document already holds *task*."""
    canary = planned_canary(tmp_path, code=code)
    seed(canary, {"task": {"EAWF-0042": task}})
    assert submit(canary, tmp_path)["status"] == "ok"
    assert approve(canary, tmp_path)["status"] == "ok"
    return canary


def test_auth_021_a_plan_naming_a_draft_promotes_it_in_place(tmp_path: Path) -> None:
    draft = seed_row("task", "DRAFT")
    canary = _approved_over(tmp_path, draft, code="PROMOTE")

    answer = apply_plan(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    task = collection(canary, "task")["EAWF-0042"]
    assert task["status"] == "PLANNED"
    assert (task["uid"], task["urn"], task["created_at"]) == (
        draft["uid"],
        TASK_URN,
        draft["created_at"],
    )
    assert task["revision"] == draft["revision"] + 1
    assert task["batch_ref"] == BATCH_URN
    assert task["due_scope"] == MILESTONE_URN
    assert task["intent"] == "build and publish the wheel"
    assert [item["id"] for item in task["criteria"]] == ["CR-01"]
    assert collection(canary, "batch")["BAT-0007"]["task_refs"] == [TASK_URN]


def test_auth_021_a_promoted_draft_keeps_the_due_scope_it_already_carried(
    tmp_path: Path,
) -> None:
    draft = {**seed_row("task", "DRAFT"), "due_scope": MILESTONE_URN}
    canary = _approved_over(tmp_path, draft, code="DUESCOPE")

    assert apply_plan(canary, tmp_path)["status"] == "ok"

    assert collection(canary, "task")["EAWF-0042"]["due_scope"] == MILESTONE_URN


def _legacy_draft() -> dict[str, Any]:
    row = copy.deepcopy(legacy_task_row())
    row.update(status="DRAFT", batch_ref=None, criteria=[], due_scope=None)
    return row


@pytest.mark.parametrize(
    ("task", "code"),
    [
        pytest.param(seed_row("task", "PLANNED"), "PLACED", id="already-placed"),
        pytest.param(seed_row("task", "DEFERRED"), "DEFERRED", id="deferred"),
        pytest.param(_legacy_draft(), "LEGACY", id="legacy-draft"),
    ],
)
def test_auth_021_a_plan_naming_a_task_it_cannot_promote_is_refused(
    tmp_path: Path, task: dict[str, Any], code: str
) -> None:
    canary = _approved_over(tmp_path, task, code=code)
    before = document_path(canary).read_bytes()

    answer = apply_plan(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert document_path(canary).read_bytes() == before
