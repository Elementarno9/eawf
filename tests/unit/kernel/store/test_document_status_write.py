"""A status-only document write never renders the committed document.

Every Run event commit rewrites the document, and the committed half of a
document of several megabytes took about 100 ms to render just to learn its
bytes had not changed. Only a write that changes a committed row or lands a
committed ledger line renders it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from eawf.kernel.store import compaction
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    provision,
    root_context,
)


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


@pytest.fixture
def renders(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the committed-document renders; the status projection's are not counted."""
    calls: list[int] = []
    real = compaction._render

    def counting(document: dict[str, object]) -> bytes:
        if "base" not in document:
            calls.append(len(document))
        return real(document)

    monkeypatch.setattr(compaction, "_render", counting)
    return calls


def _line(collection: Epoch2Collection, key: str) -> LedgerRecord:
    return LedgerRecord(
        collection=collection,
        record_key=key,
        status="accepted",
        recorded_at=AT,
        payload={"payload_kind": "probe"},
    )


def test_a_local_ledger_line_skips_the_committed_render(
    context: Epoch2RootContext, canary: CanaryProvision, renders: list[int]
) -> None:
    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _line(Epoch2Collection.RUN, "HLO-1"))
    before = document_path(canary).read_bytes()
    renders.clear()

    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _line(Epoch2Collection.RUN, "HLO-2"))

    assert renders == []
    assert document_path(canary).read_bytes() == before


def test_a_committed_ledger_line_still_renders_and_rewrites(
    context: Epoch2RootContext, canary: CanaryProvision, renders: list[int]
) -> None:
    before = document_path(canary).read_bytes()

    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _line(Epoch2Collection.RECEIPT, "call-1"))

    assert len(renders) == 1
    assert document_path(canary).read_bytes() != before


def test_a_first_write_renders_the_document(tmp_path: Path, renders: list[int]) -> None:
    state = tmp_path / "state.json"

    compaction.write_document(state, {"milestones": {}})

    assert len(renders) == 1
    assert compaction.read_document(state)["milestones"] == {}


@pytest.mark.parametrize(
    ("raw", "message"),
    [(b"[]", "not a JSON object"), (b"\xff{}", None), (b"", None), (b"{", None)],
)
def test_a_document_that_is_no_json_object_is_refused(
    tmp_path: Path, raw: bytes, message: str | None
) -> None:
    state = tmp_path / "state.json"
    state.write_bytes(raw)

    with pytest.raises(ValueError, match=message):
        compaction.read_document(state)
