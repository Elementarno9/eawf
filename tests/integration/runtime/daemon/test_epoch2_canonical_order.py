"""One workspace-global order, allocated inside the committing transaction.

``revision`` is per-entity and says nothing across two records, so the only
number that can justify rendering rows of several entity kinds in one
strict order is ``canonical_sequence``. That makes two properties load
bearing: it never repeats, and it leaves no hole. A number handed out
before the commit that produced it would break both -- a refused mutation
would burn an ordinal and a concurrent pair could stamp the same one.

The allocation therefore happens inside the transaction, under the
document lock, against the high-water mark the document itself carries.
This suite drives that from three directions: a run of accepted mutations,
a run with refusals interleaved, and a fan of threads racing for the same
root.
"""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

ACTOR = "OP-0001"

#: How many Milestones the concurrent fan races over. Four is enough for a
#: thread to lose the document lock at least once on any machine, and small
#: enough that the whole fan finishes well inside the lock timeout.
RACERS = 4


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture(autouse=True)
def patient_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a thread that loses the document lock wait rather than fail."""
    monkeypatch.setenv("EA_LOCK_TIMEOUT", "30")


def _milestone_keys(count: int) -> list[str]:
    return [f"MLS-{index:04d}" for index in range(30, 30 + count)]


def _urn_for(key: str) -> str:
    return f"{MILESTONE_URN.rsplit('/', 1)[0]}/{key}"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one planned Milestone per racer."""
    provisioned = provision(tmp_path / "repo", code="ORDER")
    planned = seed_row("milestone", "PLANNED")
    seed(
        provisioned,
        {"milestone": {key: rekeyed(planned, key=key) for key in _milestone_keys(RACERS)}},
    )
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _activate(key: str) -> TransitionRequest:
    return TransitionRequest.model_validate(
        {
            "urn": _urn_for(key),
            "to_status": "ACTIVE",
            "expected_revision": 1,
            "idempotency_key": f"req-{key}",
            "actor": ACTOR,
        }
    )


def _firehose_sequences(canary: CanaryProvision) -> list[int]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [int(row["payload"]["canonical_sequence"]) for row in rows]


def test_sequential_mutations_are_contiguous_and_increasing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    allocated = [
        run_transaction(context=context, request=_activate(key), now=AT).receipt.canonical_sequence
        for key in _milestone_keys(RACERS)
    ]

    assert allocated == list(range(1, RACERS + 1))
    assert _firehose_sequences(canary) == allocated


def test_the_document_carries_the_committed_high_water_mark(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The mark survives a restart because it lives with the rows it orders."""
    keys = _milestone_keys(RACERS)
    run_transaction(context=context, request=_activate(keys[0]), now=AT)

    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == 1

    run_transaction(context=context, request=_activate(keys[1]), now=AT)

    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == 2


def test_the_first_mutation_of_a_fresh_workspace_allocates_one(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """A workspace that has committed nothing issues the first ordinal."""
    assert CANONICAL_SEQUENCE_KEY not in read_document(document_path(canary))

    committed = run_transaction(context=context, request=_activate("MLS-0030"), now=AT)

    assert committed.receipt.canonical_sequence == 1


def test_a_refused_mutation_leaves_no_hole(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """A burned ordinal would be a hole; the mark only moves on commit."""
    keys = _milestone_keys(RACERS)
    first = run_transaction(context=context, request=_activate(keys[0]), now=AT)

    with pytest.raises(TransactionRefusedError):
        run_transaction(
            context=context,
            request=TransitionRequest.model_validate(
                {
                    "urn": _urn_for(keys[1]),
                    "to_status": "COMPLETED",
                    "expected_revision": 1,
                    "idempotency_key": "req-refused",
                    "actor": ACTOR,
                }
            ),
            now=AT,
        )

    second = run_transaction(context=context, request=_activate(keys[1]), now=AT)

    assert (first.receipt.canonical_sequence, second.receipt.canonical_sequence) == (1, 2)
    assert _firehose_sequences(canary) == [1, 2]


def test_concurrent_mutations_share_one_strict_order(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    keys = _milestone_keys(RACERS)

    def commit(key: str) -> int:
        return run_transaction(
            context=context, request=_activate(key), now=AT
        ).receipt.canonical_sequence

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        allocated = list(pool.map(commit, keys))

    assert sorted(allocated) == list(range(1, RACERS + 1))
    assert sorted(_firehose_sequences(canary)) == sorted(allocated)
    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == RACERS


def test_concurrent_mutations_commit_every_record_once(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    keys = _milestone_keys(RACERS)

    def commit(key: str) -> None:
        run_transaction(context=context, request=_activate(key), now=AT)

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        list(pool.map(commit, keys))

    rows: dict[str, Any] = read_document(document_path(canary))["milestone"]
    assert sorted(rows) == keys
    assert {row["status"] for row in rows.values()} == {"ACTIVE"}
    assert {row["revision"] for row in rows.values()} == {2}
