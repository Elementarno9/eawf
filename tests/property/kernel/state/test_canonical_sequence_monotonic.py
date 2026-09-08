"""``canonical_sequence`` stays a workspace-global total order under every path.

The four paths the ordering has to survive are interleaved commits from
several entity kinds, a crash and recovery, a rolled-back transaction,
and an imported corpus allocated in source order. Each is a way for a
counter to hand out a number twice or out of order, and each would show
up as a History route that renders plausibly and is wrong.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eawf.kernel.state.canonical_sequence import (
    CanonicalSequenceAllocator,
    CanonicalSequenceError,
    CanonicalSequenceRejection,
    MonotonicAllocator,
)

WORKSPACE = "WSP-DEFAULT"

# Stand-ins for the entity kinds whose commits interleave in one
# workspace; the counter must not care which one asked.
ENTITY_KINDS = ["task", "run", "batch", "milestone", "receipt"]

kinds = st.lists(st.sampled_from(ENTITY_KINDS), min_size=1, max_size=40)
marks = st.integers(min_value=0, max_value=10_000)


@given(committed=kinds)
def test_transaction_allocates_a_strict_order_across_entity_kinds(committed: list[str]) -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    stamps: list[tuple[str, int]] = []
    for kind in committed:
        with allocator.transaction() as txn:
            stamps.append((kind, txn.allocate()))
    sequences = [sequence for _, sequence in stamps]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert allocator.high_water_mark == sequences[-1]


@given(committed=kinds)
def test_transaction_never_reuses_a_sequence_across_two_entity_kinds(
    committed: list[str],
) -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    by_kind: dict[str, list[int]] = {kind: [] for kind in ENTITY_KINDS}
    for kind in committed:
        with allocator.transaction() as txn:
            by_kind[kind].append(txn.allocate())
    every_sequence = [sequence for stamps in by_kind.values() for sequence in stamps]
    assert len(set(every_sequence)) == len(every_sequence)


@given(outcomes=st.lists(st.booleans(), min_size=1, max_size=30))
def test_transaction_burns_a_rolled_back_sequence(outcomes: list[bool]) -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    committed: list[int] = []
    rolled_back: list[int] = []
    for commits in outcomes:
        if commits:
            with allocator.transaction() as txn:
                committed.append(txn.allocate())
            continue
        with pytest.raises(RuntimeError), allocator.transaction() as txn:
            rolled_back.append(txn.allocate())
            raise RuntimeError("commit aborted")
    assert committed == sorted(committed)
    assert not set(committed) & set(rolled_back)
    assert allocator.high_water_mark == (committed[-1] if committed else 0)


@given(mark=marks, followers=st.integers(min_value=1, max_value=20))
def test_recover_resumes_above_the_durable_high_water_mark(mark: int, followers: int) -> None:
    allocator = CanonicalSequenceAllocator.recover(workspace_key=WORKSPACE, high_water_mark=mark)
    assert allocator.high_water_mark == mark
    with allocator.transaction() as txn:
        resumed = txn.allocate_run(followers)
    assert resumed[0] == mark + 1
    assert list(resumed) == sorted(resumed)
    assert allocator.high_water_mark == mark + followers


@given(counts=st.lists(st.integers(min_value=1, max_value=25), min_size=1, max_size=8))
def test_allocate_run_stamps_an_imported_corpus_in_source_order(counts: list[int]) -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    imported: list[int] = []
    for count in counts:
        with allocator.transaction() as txn:
            imported.extend(txn.allocate_run(count))
    assert imported == list(range(1, sum(counts) + 1))
    assert allocator.high_water_mark == sum(counts)


@given(mark=marks, offset=st.integers(min_value=0, max_value=500))
def test_assert_appendable_rejects_at_or_below_the_high_water_mark(mark: int, offset: int) -> None:
    allocator = CanonicalSequenceAllocator.recover(
        workspace_key=WORKSPACE, high_water_mark=mark + 1
    )
    candidate = max(1, mark + 1 - offset)
    with pytest.raises(CanonicalSequenceError) as excinfo:
        allocator.assert_appendable(candidate)
    assert excinfo.value.code is CanonicalSequenceRejection.SEQUENCE_NOT_MONOTONIC


@given(mark=marks, offset=st.integers(min_value=1, max_value=500))
def test_assert_appendable_admits_a_sequence_above_the_high_water_mark(
    mark: int, offset: int
) -> None:
    allocator = CanonicalSequenceAllocator.recover(workspace_key=WORKSPACE, high_water_mark=mark)
    assert allocator.assert_appendable(mark + offset) is None


@given(first=kinds, second=kinds)
def test_two_workspaces_order_independently(first: list[str], second: list[str]) -> None:
    one = CanonicalSequenceAllocator(workspace_key="WSP-ONE")
    two = CanonicalSequenceAllocator(workspace_key="WSP-TWO")
    for _ in first:
        with one.transaction() as txn:
            txn.allocate()
    for _ in second:
        with two.transaction() as txn:
            txn.allocate()
    assert one.high_water_mark == len(first)
    assert two.high_water_mark == len(second)


def test_transaction_allocates_the_first_sequence_from_a_fresh_workspace() -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    assert allocator.high_water_mark == 0
    with allocator.transaction() as txn:
        assert txn.allocate() == 1


def test_transaction_commits_nothing_when_it_allocates_nothing() -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    with allocator.transaction():
        pass
    assert allocator.high_water_mark == 0


def test_assert_appendable_rejects_a_non_positive_sequence() -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    with pytest.raises(ValueError, match="canonical_sequence must be positive"):
        allocator.assert_appendable(0)


def test_allocate_run_rejects_an_empty_run() -> None:
    allocator = CanonicalSequenceAllocator(workspace_key=WORKSPACE)
    with pytest.raises(ValueError, match="count must be positive"), allocator.transaction() as txn:
        txn.allocate_run(0)


def test_canonical_sequence_allocator_rejects_an_empty_workspace_key() -> None:
    with pytest.raises(ValueError, match="workspace_key must be non-empty"):
        CanonicalSequenceAllocator(workspace_key="")


def test_canonical_sequence_allocator_rejects_a_negative_high_water_mark() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        CanonicalSequenceAllocator(workspace_key=WORKSPACE, high_water_mark=-1)


def test_monotonic_allocator_aborts_the_whole_transaction_when_it_saturates() -> None:
    allocator = MonotonicAllocator(name="bounded", committed=1, limit=2)
    with pytest.raises(CanonicalSequenceError) as excinfo, allocator.transaction() as txn:
        txn.allocate_run(2)
    assert excinfo.value.code is CanonicalSequenceRejection.SEQUENCE_SPACE_SATURATED
    assert allocator.committed == 1


def test_monotonic_allocator_rejects_a_limit_below_the_first_ordinal() -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        MonotonicAllocator(name="bounded", limit=0)


def test_monotonic_allocator_rejects_a_committed_mark_above_its_limit() -> None:
    with pytest.raises(ValueError, match="exceeds limit"):
        MonotonicAllocator(name="bounded", committed=3, limit=2)


def test_monotonic_allocator_exposes_its_name_and_limit() -> None:
    allocator = MonotonicAllocator(name="bounded", limit=9)
    assert allocator.name == "bounded"
    assert allocator.limit == 9
