"""The committed ``canonical_sequence`` keeps step with every committed write.

A write that lands a line in a committed ledger changes a tracked file, so
the committed document advances its sequence with it; a write that touches
no committed file leaves the committed document byte-identical. Callers
that read the committed sequence then send the value the daemon expects.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY, read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import STATUS_PROJECTION_COLLECTIONS, Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append, commit_row_write
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    provision,
    root_context,
)

_AT = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A freshly provisioned epoch-2 canary."""
    return provision(tmp_path / "repo")


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The canary's native context, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _line(collection: Epoch2Collection, key: str) -> LedgerRecord:
    return LedgerRecord(
        collection=collection,
        record_key=key,
        status="accepted",
        recorded_at=AT,
        payload={"payload_kind": "probe"},
    )


def _committed(path: Path) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(path.read_bytes())
    return decoded


def test_ledger_append_advances_the_committed_sequence(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    path = document_path(canary)

    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _line(Epoch2Collection.RUN, "HLO-1"))
    untouched = path.read_bytes()
    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _line(Epoch2Collection.RUN, "HLO-2"))
        commit_row_write(
            session,
            collection=Epoch2Collection.RUN,
            record_key="RUN-00000001",
            row={"key": "RUN-00000001", "status": "RUNNING"},
            event_name="run.started",
            event_fields={},
            compaction=None,
            now=AT,
        )
    # Local-only writes moved the live sequence and left the tracked file alone.
    assert path.read_bytes() == untouched
    assert read_document(path)[CANONICAL_SEQUENCE_KEY] == 3
    assert _committed(path).get(CANONICAL_SEQUENCE_KEY, 0) < 3

    with context.session([MILESTONE_URN]) as session:
        envelope = commit_ledger_append(session, _line(Epoch2Collection.DECISION, "DEC-0001"))

    live = read_document(path)[CANONICAL_SEQUENCE_KEY]
    assert live == envelope.payload["canonical_sequence"] == 4
    assert _committed(path)[CANONICAL_SEQUENCE_KEY] == live


def _tree(tmp_path: Path, *, sequence: int | None) -> Path:
    state_path = tmp_path / "state.json"
    document: dict[str, Any] = {"schema_version": 1, "milestone": {"MLS-0001": {"status": "x"}}}
    if sequence is not None:
        document[CANONICAL_SEQUENCE_KEY] = sequence
    write_document(state_path, document)
    return state_path


def test_status_only_write_keeps_the_committed_bytes(tmp_path: Path) -> None:
    state_path = _tree(tmp_path, sequence=5)
    before = state_path.read_bytes()

    document = read_document(state_path) | {CANONICAL_SEQUENCE_KEY: 6}
    write_document(state_path, document)

    assert state_path.read_bytes() == before
    assert read_document(state_path)[CANONICAL_SEQUENCE_KEY] == 6


@pytest.mark.parametrize("collection", sorted(STATUS_PROJECTION_COLLECTIONS))
def test_local_ledger_append_keeps_the_committed_bytes(
    tmp_path: Path, collection: Epoch2Collection
) -> None:
    state_path = _tree(tmp_path, sequence=5)
    before = state_path.read_bytes()

    document = read_document(state_path) | {CANONICAL_SEQUENCE_KEY: 6}
    write_document(state_path, document, appends_to=collection)

    assert state_path.read_bytes() == before
    assert read_document(state_path)[CANONICAL_SEQUENCE_KEY] == 6


def test_committed_ledger_append_advances_by_exactly_one(tmp_path: Path) -> None:
    state_path = _tree(tmp_path, sequence=5)

    document = read_document(state_path) | {CANONICAL_SEQUENCE_KEY: 6}
    write_document(state_path, document, appends_to=Epoch2Collection.DECISION)

    assert _committed(state_path)[CANONICAL_SEQUENCE_KEY] == 6
    assert read_document(state_path)[CANONICAL_SEQUENCE_KEY] == 6


def test_committed_ledger_append_catches_up_a_lagging_copy(tmp_path: Path) -> None:
    state_path = _tree(tmp_path, sequence=1)
    write_document(state_path, read_document(state_path) | {CANONICAL_SEQUENCE_KEY: 40})
    assert _committed(state_path)[CANONICAL_SEQUENCE_KEY] == 1

    write_document(
        state_path,
        read_document(state_path) | {CANONICAL_SEQUENCE_KEY: 41},
        appends_to=Epoch2Collection.TASK,
    )

    assert _committed(state_path)[CANONICAL_SEQUENCE_KEY] == 41


def test_committed_ledger_append_without_a_sequence_adds_none(tmp_path: Path) -> None:
    state_path = _tree(tmp_path, sequence=None)
    before = state_path.read_bytes()

    write_document(state_path, read_document(state_path), appends_to=Epoch2Collection.DECISION)

    assert state_path.read_bytes() == before
    assert CANONICAL_SEQUENCE_KEY not in _committed(state_path)


def test_first_write_of_a_tree_commits_the_sequence(tmp_path: Path) -> None:
    state_path = _tree(tmp_path, sequence=0)

    assert _committed(state_path)[CANONICAL_SEQUENCE_KEY] == 0


def test_unencodable_document_raises_type_error(tmp_path: Path) -> None:
    state_path = _tree(tmp_path, sequence=5)
    before = state_path.read_bytes()

    with pytest.raises(TypeError):
        write_document(
            state_path,
            read_document(state_path) | {"milestone": {"MLS-0001": {"at": _AT}}},
            appends_to=Epoch2Collection.DECISION,
        )

    assert state_path.read_bytes() == before
