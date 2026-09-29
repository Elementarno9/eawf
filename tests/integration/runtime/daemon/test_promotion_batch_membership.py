"""A promotion into a Batch the tree holds no live row for is refused.

A promotion lists its Task on the Batch it names in the same commit, and
refuses a Batch that has moved past ``ACTIVE``. A Batch that closed is
compacted out of the document into its ledger, and a row imported from
epoch 1 is not a native Batch, so neither leaves a live row to judge. A
promotion naming one of them, or a Batch that never existed, is refused
before anything is written rather than placing the Task where no Batch
will ever merge it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path, seed_row
from tests.integration.runtime.daemon.test_domain_delivery_verbs import _canary, _promote

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _draft(batches: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a draft Task beside *batches*, which is the whole Batch collection."""
    return {"task": {"EAWF-0042": seed_row("task", "DRAFT")}, "batch": batches}


@pytest.mark.parametrize(
    "batches",
    [
        pytest.param({}, id="absent-or-compacted"),
        pytest.param(
            {"BAT-0007": {"payload": {"id": "BAT-0007"}, "status": "ACTIVE"}}, id="not-native"
        ),
    ],
)
def test_r06_promotion_into_a_batch_with_no_live_row_is_refused(
    tmp_path: Path, batches: dict[str, Any]
) -> None:
    canary = _canary(tmp_path, _draft(batches))
    before = document_path(canary).read_bytes()

    answer = _promote(canary, tmp_path)

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.TRANSITION_GUARD_FAILED.value
    assert row["guard"] == "promotion_contract_complete"
    assert "no live batch BAT-0007" in row["message"]
    assert document_path(canary).read_bytes() == before


def test_r06_promotion_into_a_live_active_batch_still_lists_the_task(tmp_path: Path) -> None:
    """The positive control: a live ACTIVE row takes the Task."""
    canary = _canary(tmp_path, _draft({"BAT-0007": seed_row("batch", "ACTIVE")}))

    answer = _promote(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
