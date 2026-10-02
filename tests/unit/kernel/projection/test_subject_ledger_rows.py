"""A route opened on a record reads that record and everything filed under it from the ledgers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import subject_ledger_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext

pytestmark = pytest.mark.unit

_URN = "eawf://WSP-X/PRJ-X/REP-X"


def _row(kind: str, key: str, **fields: Any) -> dict[str, Any]:
    return {
        "key": key,
        "urn": f"{_URN}/{kind}/{key}",
        "revision": 1,
        "status": "COMPLETED",
        **fields,
    }


def _task(key: str, batch: str) -> dict[str, Any]:
    return _row("task", key, batch_ref=f"{_URN}/batch/{batch}")


def _batch(key: str, milestone: str) -> dict[str, Any]:
    return _row("batch", key, milestone_ref=f"{_URN}/milestone/{milestone}")


_FILED = {
    Epoch2Collection.BATCH: {"BAT-0001": _batch("BAT-0001", "MLS-0001")},
    Epoch2Collection.TASK: {
        "EAWF-0001": _task("EAWF-0001", "BAT-0001"),
        "EAWF-0002": _task("EAWF-0002", "BAT-0001"),
        "EAWF-0003": _task("EAWF-0003", "BAT-0002"),
    },
}


def _keys(picked: dict[Epoch2Collection, tuple[Any, ...]]) -> dict[str, list[str]]:
    return {collection.value: [row["key"] for row in rows] for collection, rows in picked.items()}


def test_a_closed_batch_reads_back_with_its_closed_tasks() -> None:
    """The Batch and only the Tasks filed under it, not a sibling Batch's."""
    picked = subject_ledger_rows(
        route="batch.detail", subject="BAT-0001", document={}, filed=_FILED
    )

    assert _keys(picked) == {"batch": ["BAT-0001"], "task": ["EAWF-0001", "EAWF-0002"]}


def test_a_live_batch_lists_its_closed_tasks() -> None:
    """A parent still in the document is the subject all the same."""
    document = {"batch": {"BAT-0002": _batch("BAT-0002", "MLS-0001") | {"status": "ACTIVE"}}}

    picked = subject_ledger_rows(
        route="batch.detail", subject="BAT-0002", document=document, filed=_FILED
    )

    assert _keys(picked) == {"task": ["EAWF-0003"]}


def test_a_closed_task_reads_back_by_its_urn() -> None:
    """A caller that names the record by its URN reads the same row."""
    picked = subject_ledger_rows(
        route="task.detail", subject=f"{_URN}/task/EAWF-0002", document={}, filed=_FILED
    )

    assert _keys(picked) == {"task": ["EAWF-0002"]}


def test_a_milestone_reaches_tasks_only_through_its_batches() -> None:
    """Descendants are found down the chain, through a closed Batch, in the route's collections."""
    picked = subject_ledger_rows(route="milestone", subject="MLS-0001", document={}, filed=_FILED)

    assert _keys(picked) == {"batch": ["BAT-0001"]}


def test_a_document_row_is_never_returned_from_the_ledger() -> None:
    """The document's row is the current one, so the ledger's copy of that key is left out."""
    document = {"task": {"EAWF-0001": _task("EAWF-0001", "BAT-0001") | {"status": "ACTIVE"}}}

    picked = subject_ledger_rows(
        route="batch.detail", subject="BAT-0001", document=document, filed=_FILED
    )

    assert _keys(picked) == {"batch": ["BAT-0001"], "task": ["EAWF-0002"]}


def test_a_subject_nothing_names_reads_nothing() -> None:
    """The empty boundaries: an unknown key, no ledgers, and a route with no chain collection."""
    assert (
        subject_ledger_rows(route="batch.detail", subject="BAT-9999", document={}, filed=_FILED)
        == {}
    )
    assert (
        subject_ledger_rows(route="batch.detail", subject="BAT-0001", document={}, filed={}) == {}
    )
    assert subject_ledger_rows(route="trust", subject="BAT-0001", document={}, filed=_FILED) == {}
    assert (
        subject_ledger_rows(route="no.such.route", subject="BAT-0001", document={}, filed=_FILED)
        == {}
    )


@pytest.mark.parametrize("params", [{"key": ""}, {"key": 7}, {"subject": "BAT-0001"}])
def test_a_route_read_refuses_a_parameter_it_does_not_take(
    params: dict[str, Any], tmp_path: Path
) -> None:
    """An empty or mistyped key, or a parameter the read does not know, is refused by name."""
    ctx = MethodContext(
        started_at="2026-09-01T12:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=tmp_path / "wal",
    )

    with pytest.raises(DaemonValidationError, match="projection_unreadable"):
        asyncio.run(methods.dispatch("projection.batch.detail.read", ctx, params))
