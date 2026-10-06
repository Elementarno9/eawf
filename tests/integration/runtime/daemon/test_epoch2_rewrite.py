"""A whole-document rewrite commits like any native write; a refused one writes nothing."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.changes import ChangeRecord, ChangeTier, change_log_path
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_rewrite import commit_document_rewrite
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.wal import WalStatus, list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

EVENT: Final = "migration.probe.applied"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one planned Milestone."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _changes(canary: CanaryProvision) -> list[ChangeRecord]:
    path = change_log_path(document_path(canary), ChangeTier.COMMITTED)
    if not path.is_file():
        return []
    return [ChangeRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def _retitled(canary: CanaryProvision, title: str) -> dict[str, Any]:
    document = copy.deepcopy(read_document(document_path(canary)))
    document["milestone"]["MLS-0030"]["title"] = title
    return document


def test_a_rewrite_writes_one_intent_one_row_and_its_changes(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = read_document(document_path(canary)).get(CANONICAL_SEQUENCE_KEY, 0)

    with context.session([MILESTONE_URN]) as session:
        envelope = commit_document_rewrite(
            session,
            new_document=_retitled(canary, "Renamed"),
            event_name=EVENT,
            event_fields={"rows": 1},
            now=AT,
        )

    stored = read_document(document_path(canary))
    assert stored["milestone"]["MLS-0030"]["title"] == "Renamed"
    assert stored[CANONICAL_SEQUENCE_KEY] == before + 1
    assert len(list_records(context.wal_dir, status=WalStatus.FSYNCED)) == 1
    assert list_records(context.wal_dir, status=WalStatus.PENDING) == []
    [row] = _firehose_rows(canary)
    assert row["id"] == envelope.id
    assert row["payload"]["name"] == EVENT
    assert row["payload"]["rows"] == 1
    assert row["payload"]["canonical_sequence"] == before + 1
    [change] = _changes(canary)
    assert (change.record_key, change.event_name) == ("MLS-0030", EVENT)


def test_a_rewrite_that_changes_no_row_still_draws_one_sequence(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    unchanged = read_document(document_path(canary))
    before = unchanged.get(CANONICAL_SEQUENCE_KEY, 0)

    with context.session([MILESTONE_URN]) as session:
        commit_document_rewrite(
            session, new_document=unchanged, event_name=EVENT, event_fields={}, now=AT
        )

    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == before + 1
    assert len(_firehose_rows(canary)) == 1
    assert _changes(canary) == []


def test_a_stale_sequence_in_the_rewrite_is_replaced_by_the_drawn_one(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    document = _retitled(canary, "Renamed")
    document[CANONICAL_SEQUENCE_KEY] = 0
    before = read_document(document_path(canary)).get(CANONICAL_SEQUENCE_KEY, 0)

    with context.session([MILESTONE_URN]) as session:
        commit_document_rewrite(
            session, new_document=document, event_name=EVENT, event_fields={}, now=AT
        )

    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == before + 1
    assert document[CANONICAL_SEQUENCE_KEY] == 0, "the caller's document is not mutated"


def test_a_leaky_rewrite_writes_nothing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = document_path(canary).read_bytes()

    with (
        context.session([MILESTONE_URN]) as session,
        pytest.raises(TransactionRefusedError) as caught,
    ):
        commit_document_rewrite(
            session,
            new_document=_retitled(canary, "ghp_" + "a" * 36),
            event_name=EVENT,
            event_fields={},
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []
    assert _firehose_rows(canary) == []


def test_a_rewrite_refuses_a_closed_session(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    with context.session([MILESTONE_URN]) as session:
        pass

    with pytest.raises(RuntimeError, match="closed"):
        commit_document_rewrite(
            session,
            new_document=_retitled(canary, "Renamed"),
            event_name=EVENT,
            event_fields={},
            now=AT,
        )

    assert list_records(context.wal_dir) == []
    assert _firehose_rows(canary) == []
