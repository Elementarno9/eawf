"""Attention lists the Runs and verdicts that need someone, read from their records.

A failed Run is listed while it is still the newest attempt of a Task the document holds
open, and a running Run while no stall stands over it; a Run whose Task moved on, or one
that finished, is history and is not listed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.attention import AttentionBucket, build_attention_view
from eawf.kernel.projection.compute import RUN_STATE_KIND, RouteProjection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.store.tiers import Epoch2Collection
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_child_runs import (
    call,
    compact,
    repository_row,
    run_row,
    urn,
)

pytestmark = pytest.mark.integration

READ: Final = "projection.attention.read"
TASK: Final = "EAWF-0042"
TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"


def _failed(key: str, created_at: str) -> dict[str, Any]:
    row = seed_row("run", "FAILED")
    row.update({"key": key, "urn": urn(key), "created_at": created_at})
    row["scope"] = {**row["scope"], "task_ref": TASK_URN}
    return row


def _tree(tmp_path: Path, *, runs: dict[str, dict[str, Any]], task: bool = True) -> Any:
    canary = provision(tmp_path / "repo", code="KIDS")
    seed(
        canary,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.RUN.value: runs,
            **({Epoch2Collection.TASK.value: {TASK: seed_row("task", "PLANNED")}} if task else {}),
        },
    )
    return canary


def _items(canary: Any, tmp_path: Path) -> dict[str, AttentionBucket]:
    projection = RouteProjection.model_validate(call(canary, tmp_path, READ))
    register = build_register_view(projection)
    listed = [r for r in register.rows if r.facts.get("kind") == RUN_STATE_KIND]
    assert all(r.collection is Epoch2Collection.RUN for r in listed)
    return {i.key: i.bucket for i in build_attention_view(register).items}


def test_a_failed_run_of_an_open_task_and_a_running_run_are_listed(tmp_path: Path) -> None:
    canary = _tree(tmp_path, runs={"RUN-00000020": run_row("RUN-00000020")})
    compact(canary, _failed("RUN-00000010", "2026-09-08T01:00:00Z"))

    assert _items(canary, tmp_path) == {
        "RUN-00000010": AttentionBucket.FAILED,
        "RUN-00000020": AttentionBucket.ACTIVE,
    }


def test_a_failed_run_a_later_attempt_followed_is_not_listed(tmp_path: Path) -> None:
    canary = _tree(tmp_path, runs={})
    compact(canary, _failed("RUN-00000010", "2026-09-08T01:00:00Z"))
    compact(canary, _failed("RUN-00000011", "2026-09-08T02:00:00Z"))

    assert _items(canary, tmp_path) == {"RUN-00000011": AttentionBucket.FAILED}


def test_a_failed_run_whose_task_left_the_document_is_history(tmp_path: Path) -> None:
    canary = _tree(tmp_path, runs={}, task=False)
    compact(canary, _failed("RUN-00000010", "2026-09-08T01:00:00Z"))

    assert _items(canary, tmp_path) == {}
